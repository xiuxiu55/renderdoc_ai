"""RenderDoc replay session management for the MCP server.

The RenderDoc ``ReplayController`` has thread affinity: every call must happen
on the same thread that opened the capture. To play nicely with the MCP server
(whose tool handlers may be dispatched from an anyio worker pool) we funnel all
RenderDoc interactions through a single dedicated worker thread.
"""

from __future__ import annotations

import os
import sys
import threading
import queue
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

# The renderdoc python module (renderdoc.pyd / renderdoc.so). Imported lazily so
# that the server process can start even before a capture is loaded, and so we
# can inject the module search path from an environment variable or tool call.
rd: Any = None


class RenderDocError(RuntimeError):
    """Raised for any RenderDoc-specific failure that should surface to the user."""


def _import_renderdoc(module_path: Optional[str]) -> Any:
    """Import the renderdoc module, wiring up the native library search path.

    ``module_path`` should point to the directory that contains ``renderdoc.pyd``
    (or ``renderdoc.so``) as well as the native ``renderdoc.dll`` /
    ``librenderdoc.so``. If ``None`` we fall back to ``RENDERDOC_MODULE_PATH``
    and finally the default import path.
    """
    global rd
    if rd is not None:
        return rd

    module_path = module_path or os.environ.get("RENDERDOC_MODULE_PATH")

    if module_path:
        module_path = os.path.abspath(module_path)
        if not os.path.isdir(module_path):
            raise RenderDocError(f"RenderDoc module path does not exist: {module_path}")
        # Development builds often put renderdoc.pyd under pymodules/ and the
        # native DLL next to the Development root — search both.
        search_dirs = [module_path]
        pymodules = os.path.join(module_path, "pymodules")
        if os.path.isdir(pymodules):
            search_dirs.insert(0, pymodules)
        parent = os.path.dirname(module_path)
        if os.path.basename(module_path).lower() == "pymodules" and os.path.isdir(parent):
            search_dirs.append(parent)
        for d in search_dirs:
            if d not in sys.path:
                sys.path.insert(0, d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            if sys.platform == "win32" and sys.version_info >= (3, 8):
                try:
                    os.add_dll_directory(d)
                except (OSError, AttributeError):
                    pass
        if sys.platform != "win32":
            os.environ["LD_LIBRARY_PATH"] = (
                os.pathsep.join(search_dirs)
                + os.pathsep
                + os.environ.get("LD_LIBRARY_PATH", "")
            )

    try:
        import renderdoc as _rd  # type: ignore
    except ImportError as exc:
        hint = (
            "Could not import the 'renderdoc' module. Build RenderDoc and either "
            "set the RENDERDOC_MODULE_PATH environment variable to the directory "
            "containing renderdoc.pyd/renderdoc.dll, or pass module_path to "
            "load_capture. Note: the Python version running this server must match "
            "the Python version RenderDoc was built against."
        )
        raise RenderDocError(f"{hint}\nOriginal error: {exc}") from exc

    rd = _rd
    return rd


class _Worker:
    """A single dedicated thread that executes callables submitted to it."""

    def __init__(self) -> None:
        self._tasks: "queue.Queue[tuple]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="renderdoc-replay", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            fn, args, kwargs, result_box, done = self._tasks.get()
            if fn is None:  # shutdown sentinel
                done.set()
                return
            try:
                result_box.append(("ok", fn(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - marshal every error back
                result_box.append(("err", exc))
            finally:
                done.set()

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        result_box: list = []
        done = threading.Event()
        self._tasks.put((fn, args, kwargs, result_box, done))
        done.wait()
        status, payload = result_box[0]
        if status == "err":
            raise payload
        return payload

    def stop(self) -> None:
        done = threading.Event()
        self._tasks.put((None, (), {}, [], done))
        done.wait()


class Session:
    """Holds the currently loaded capture and its replay controller."""

    def __init__(self) -> None:
        self._worker = _Worker()
        self._replay_initialised = False

        self.cap: Any = None
        self.controller: Any = None
        self.filename: Optional[str] = None

        # Flat cache: eventId -> ActionDescription for O(1) lookups.
        self._action_by_event: dict[int, Any] = {}
        self._current_event: int = 0

    # -- internal helpers -------------------------------------------------

    def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` on the dedicated replay thread and return its result."""
        return self._worker.submit(fn, *args, **kwargs)

    def _ensure_replay(self, module_path: Optional[str]) -> None:
        module = _import_renderdoc(module_path)
        if not self._replay_initialised:
            result = module.InitialiseReplay(module.GlobalEnvironment(), [])
            # Older builds return None from InitialiseReplay; only fail on an
            # explicit non-success result.
            if result is not None and result != module.ResultCode.Succeeded:
                raise RenderDocError(f"InitialiseReplay failed: {result}")
            self._replay_initialised = True

    def _rebuild_action_cache(self) -> None:
        self._action_by_event.clear()

        def walk(actions: Any) -> None:
            for a in actions:
                self._action_by_event[int(a.eventId)] = a
                if a.children:
                    walk(a.children)

        walk(self.controller.GetRootActions())

    # -- lifecycle --------------------------------------------------------

    def load(self, path: str, module_path: Optional[str] = None) -> dict:
        if not os.path.isfile(path):
            raise RenderDocError(f"Capture file not found: {path}")

        self.close()

        def _open() -> dict:
            self._ensure_replay(module_path)
            module = rd

            cap = module.OpenCaptureFile()
            result = cap.OpenFile(path, "", None)
            if result != module.ResultCode.Succeeded:
                raise RenderDocError(f"Couldn't open file '{path}': {result}")

            if not cap.LocalReplaySupport():
                cap.Shutdown()
                raise RenderDocError(
                    "Capture cannot be replayed locally on this platform/build."
                )

            result, controller = cap.OpenCapture(module.ReplayOptions(), None)
            if result != module.ResultCode.Succeeded:
                cap.Shutdown()
                raise RenderDocError(f"Couldn't initialise replay: {result}")

            self.cap = cap
            self.controller = controller
            self.filename = path
            self._rebuild_action_cache()
            self._current_event = 0

            props = controller.GetAPIProperties()
            return {
                "filename": path,
                "api": str(props.pipelineType),
                "localRenderer": str(props.localRenderer),
                "vendor": str(getattr(props, "vendor", "")),
                "degraded": bool(getattr(props, "degraded", False)),
                "rootActionCount": len(controller.GetRootActions()),
                "totalActions": len(self._action_by_event),
            }

        return self.run(_open)

    def close(self) -> bool:
        if self.controller is None and self.cap is None:
            return False

        def _shutdown() -> None:
            if self.controller is not None:
                try:
                    self.controller.Shutdown()
                except Exception:  # noqa: BLE001
                    pass
            if self.cap is not None:
                try:
                    self.cap.Shutdown()
                except Exception:  # noqa: BLE001
                    pass

        self.run(_shutdown)
        self.controller = None
        self.cap = None
        self.filename = None
        self._action_by_event.clear()
        self._current_event = 0
        return True

    def shutdown(self) -> None:
        self.close()
        if self._replay_initialised and rd is not None:
            try:
                self.run(rd.ShutdownReplay)
            except Exception:  # noqa: BLE001
                pass
            self._replay_initialised = False
        self._worker.stop()

    # -- accessors --------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self.controller is not None

    def require_controller(self) -> Any:
        if self.controller is None:
            raise RenderDocError("No capture is loaded. Call load_capture first.")
        return self.controller

    def action_for_event(self, event_id: int) -> Any:
        action = self._action_by_event.get(int(event_id))
        if action is None:
            raise RenderDocError(f"No action found for eventId {event_id}.")
        return action

    def all_actions(self) -> dict[int, Any]:
        return self._action_by_event

    def set_event(self, event_id: int, force: bool = False) -> None:
        controller = self.require_controller()
        # Validate the event id exists before moving the cursor.
        if int(event_id) not in self._action_by_event:
            raise RenderDocError(f"No action found for eventId {event_id}.")

        def _set() -> None:
            controller.SetFrameEvent(int(event_id), force)

        self.run(_set)
        self._current_event = int(event_id)

    @property
    def current_event(self) -> int:
        return self._current_event
