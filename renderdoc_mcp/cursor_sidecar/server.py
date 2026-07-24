"""HTTP server mimicking CodeBuddy --serve so the RenderDoc panel can chat
via Cursor Cloud Agents HTTP (api.cursor.com).

Compatible surfaces:
  GET  /api/v1/health
  POST /api/v1/runs
  GET  /api/v1/runs/{runId}/stream
  POST /api/v1/acp/connect
  POST /api/v1/acp          (JSON-RPC over SSE: initialize / session/*)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import json
import os
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


DEFAULT_MODEL = "composer-2.5"
DEFAULT_MODES = [
    {"id": "bypassPermissions", "name": "bypassPermissions",
     "description": "Non-interactive (sidecar default)"},
    {"id": "dontAsk", "name": "dontAsk", "description": "Read-oriented"},
]


def _jid() -> str:
    return str(uuid.uuid4())


def _clean_text(s: str) -> str:
    """Replace lone UTF-16 surrogates so UTF-8 encode / JSON never fail."""
    if not s:
        return ""
    return "".join(
        ch if not (0xD800 <= ord(ch) <= 0xDFFF) else "\ufffd"
        for ch in s
    )


def _sse(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _sse_event(event: str, obj: dict) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False))).encode(
        "utf-8"
    )


@dataclass
class RunState:
    run_id: str
    text: str
    status: str = "accepted"  # accepted | completed | error
    markdown: str = ""
    error: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    chunks: List[str] = field(default_factory=list)


@dataclass
class SessionState:
    session_id: str
    cwd: str
    model: str
    mode: str = "bypassPermissions"
    agent: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class CursorBridge:
    """All cursor-sdk calls are serialized on one worker thread.

    WinError 10038 (WSAENOTSOCK) is common on Windows when the local
    cursor-bridge sockets are touched from multiple uvicorn/worker threads.
    """

    def __init__(self, api_key: str, cwd: str, default_model: str = DEFAULT_MODEL):
        self.api_key = api_key
        self.cwd = cwd or os.getcwd()
        self.default_model = default_model
        self.connections: Dict[str, Dict[str, SessionState]] = {}
        self.runs: Dict[str, RunState] = {}
        self._models_cache: Optional[List[dict]] = None
        self._sdk_lock = threading.Lock()  # serialize ALL sdk usage

    # -- models -----------------------------------------------------------

    def list_models(self) -> List[dict]:
        if self._models_cache is not None:
            return self._models_cache
        # Avoid Cursor.models.list() in the uvicorn process — on Windows it can
        # launch the local Bridge and hit WinError 10038 during select().
        out = [
            {"modelId": DEFAULT_MODEL, "name": DEFAULT_MODEL,
             "description": "Default Cursor model"},
            {"modelId": "composer-2", "name": "composer-2",
             "description": "Composer 2"},
            {"modelId": "auto", "name": "auto", "description": "Server-selected"},
        ]
        self._models_cache = out
        return out

    # -- agent ------------------------------------------------------------

    def _prompt_subprocess(self, text: str, model: str, cwd: str) -> str:
        """Run Cloud Agents HTTP via run_prompt.py (no local Bridge).

        cursor-sdk Bridge.launch uses select() on stderr pipes; on Windows that
        raises WinError 10038. The worker talks HTTPS to api.cursor.com instead.
        """
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_prompt.py")
        env = os.environ.copy()
        env["CURSOR_API_KEY"] = self.api_key
        env["CURSOR_SIDECAR_MODEL"] = model or self.default_model
        env["CURSOR_SIDECAR_CWD"] = cwd or self.cwd
        # Avoid inheriting a broken asyncio selector state into the child.
        env.pop("PYTHONASYNCIODEBUG", None)

        text = _clean_text(text or "")
        kwargs = dict(
            args=[sys.executable, "-u", worker],
            input=text.encode("utf-8", "replace"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd or self.cwd,
            timeout=300,
        )
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP | DETACHED-ish: don't share console select set
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        with self._sdk_lock:
            proc = subprocess.run(**kwargs)

        stderr = (proc.stderr or b"").decode("utf-8", "replace")
        stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if stderr.strip():
            print("[cursor_sidecar] worker stderr:\n%s" % stderr[-2000:])
        if not stdout:
            raise RuntimeError(
                "Cursor worker produced no output (exit=%s). stderr=%s"
                % (proc.returncode, stderr[-500:])
            )
        # worker prints one JSON line (possibly preceded by SDK noise) — take last {...}
        line = stdout.splitlines()[-1]
        try:
            payload = json.loads(line)
        except ValueError:
            # try find last json object
            i = stdout.rfind("{")
            if i < 0:
                raise RuntimeError("Cursor worker bad output: %s" % stdout[-500:])
            payload = json.loads(stdout[i:])

        if not payload.get("ok"):
            raise RuntimeError(_clean_text(payload.get("error") or "Cursor worker failed"))
        return _clean_text(payload.get("text") or "")

    def ask(self, text: str, model: Optional[str] = None,
            cwd: Optional[str] = None,
            on_chunk: Optional[Callable[[str], None]] = None,
            agent: Any = None) -> str:
        """Ask Cursor via isolated subprocess (Windows-safe)."""
        del agent
        model = model or self.default_model
        cwd = cwd or self.cwd
        answer = self._prompt_subprocess(_clean_text(text or ""), model, cwd)
        if answer and on_chunk:
            on_chunk(answer)
        return answer or "(Cursor returned empty response)"

    # -- simple /runs API -------------------------------------------------

    def start_run(self, text: str) -> RunState:
        run = RunState(run_id=_jid(), text=text)
        self.runs[run.run_id] = run

        def worker():
            try:
                md = self.ask(text, on_chunk=lambda c: self._append_chunk(run, c))
                with run.lock:
                    run.markdown = md or run.markdown
                    run.status = "completed"
            except Exception as exc:  # noqa: BLE001
                with run.lock:
                    run.status = "error"
                    run.error = str(exc)
                    run.markdown = "⚠️ Cursor sidecar error: %s" % exc
            finally:
                run.done.set()

        threading.Thread(target=worker, daemon=True).start()
        return run

    def _append_chunk(self, run: RunState, chunk: str) -> None:
        with run.lock:
            run.chunks.append(chunk)
            run.markdown = "".join(run.chunks)

    # -- ACP sessions -----------------------------------------------------

    def connect(self) -> str:
        cid = _jid()
        self.connections[cid] = {}
        return cid

    def new_session(self, connection_id: str, cwd: str) -> SessionState:
        models = self.list_models()
        model = self.default_model
        ids = [m["modelId"] for m in models]
        if model not in ids and ids:
            model = ids[0]
        sess = SessionState(
            session_id=_jid(),
            cwd=cwd or self.cwd,
            model=model,
            agent=None,  # never keep a live Agent; prompt is one-shot per message
        )
        self.connections.setdefault(connection_id, {})[sess.session_id] = sess
        return sess

    def get_session(self, connection_id: str, session_id: str) -> Optional[SessionState]:
        return self.connections.get(connection_id, {}).get(session_id)

    def set_model(self, sess: SessionState, model_id: str) -> None:
        if not model_id or model_id == sess.model:
            return
        with sess.lock:
            sess.model = model_id
            sess.agent = None


def build_app(bridge: CursorBridge) -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"data": {"status": "ok", "backend": "cursor"}})

    async def create_run(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = body.get("text") or body.get("prompt") or ""
        if not str(text).strip():
            return JSONResponse({"error": "missing text"}, status_code=400)
        run = bridge.start_run(str(text))
        return JSONResponse(
            {"data": {"runId": run.run_id, "status": "accepted"}},
            status_code=202,
        )

    async def stream_run(request: Request) -> StreamingResponse:
        run_id = request.path_params["run_id"]
        run = bridge.runs.get(run_id)
        if run is None:
            return JSONResponse({"error": "unknown runId"}, status_code=404)

        def gen():
            last_len = 0
            while True:
                with run.lock:
                    md = run.markdown
                    status = run.status
                    err = run.error
                if len(md) > last_len:
                    yield _sse_event(
                        "message",
                        {
                            "status": "running" if status == "accepted" else status,
                            "content": {"markdown": md},
                            "agent": {"toolCalls": []},
                        },
                    )
                    last_len = len(md)
                if run.done.is_set():
                    with run.lock:
                        md = run.markdown
                        status = run.status
                    yield _sse_event(
                        "message",
                        {
                            "status": "completed" if status != "error" else "error",
                            "content": {"markdown": md},
                            "agent": {"toolCalls": []},
                            "error": err or None,
                        },
                    )
                    yield _sse_event("done", {})
                    break
                run.done.wait(0.2)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def acp_connect(_request: Request) -> JSONResponse:
        cid = bridge.connect()
        return JSONResponse({"connectionId": cid, "data": {"connectionId": cid}})

    async def acp_rpc(request: Request) -> Response:
        cid = request.headers.get("acp-connection-id") or ""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid json"}, status_code=400)

        method = body.get("method")
        params = body.get("params") or {}
        rid = body.get("id")

        # Notifications (no response body required) — session/cancel
        if rid is None:
            return JSONResponse({"ok": True})

        def result_msg(result: Any) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def error_msg(code: int, message: str) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

        # Non-streaming RPCs return a single SSE frame then end (client stops on id).
        if method == "initialize":
            payload = result_msg({"protocolVersion": 1, "serverInfo": {
                "name": "cursor-sidecar", "version": "1.0"}})
            return StreamingResponse(iter([_sse(payload)]), media_type="text/event-stream")

        if method == "session/new":
            if not cid or cid not in bridge.connections:
                # Allow connect-less usage by creating an implicit connection.
                cid = bridge.connect()
            cwd = params.get("cwd") or bridge.cwd
            sess = bridge.new_session(cid, cwd)
            models = bridge.list_models()
            payload = result_msg({
                "sessionId": sess.session_id,
                "models": {
                    "currentModelId": sess.model,
                    "availableModels": models,
                },
                "modes": {
                    "currentModeId": sess.mode,
                    "availableModes": DEFAULT_MODES,
                },
            })
            # Include connection id hint in a custom header? Panel already has cid
            # from /connect. If panel connected, fine.
            return StreamingResponse(iter([_sse(payload)]), media_type="text/event-stream")

        if method == "session/set_model":
            sess = bridge.get_session(cid, params.get("sessionId"))
            if sess is None:
                return StreamingResponse(
                    iter([_sse(error_msg(-32000, "session not found"))]),
                    media_type="text/event-stream",
                )
            bridge.set_model(sess, params.get("modelId") or "")
            return StreamingResponse(
                iter([_sse(result_msg({"modelId": sess.model}))]),
                media_type="text/event-stream",
            )

        if method == "session/set_mode":
            sess = bridge.get_session(cid, params.get("sessionId"))
            if sess is None:
                return StreamingResponse(
                    iter([_sse(error_msg(-32000, "session not found"))]),
                    media_type="text/event-stream",
                )
            sess.mode = params.get("modeId") or sess.mode
            return StreamingResponse(
                iter([_sse(result_msg({"modeId": sess.mode}))]),
                media_type="text/event-stream",
            )

        if method == "session/prompt":
            sess = bridge.get_session(cid, params.get("sessionId"))
            if sess is None:
                return StreamingResponse(
                    iter([_sse(error_msg(-32000, "Connection not found"))]),
                    media_type="text/event-stream",
                )
            prompt_parts = params.get("prompt") or []
            texts = []
            for p in prompt_parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text") or "")
                elif isinstance(p, str):
                    texts.append(p)
            user_text = _clean_text("\n".join(texts)).strip()
            if not user_text:
                return StreamingResponse(
                    iter([_sse(error_msg(-32602, "empty prompt"))]),
                    media_type="text/event-stream",
                )

            queue: List[bytes] = []
            done = threading.Event()
            err_holder: List[str] = []

            def on_chunk(chunk: str) -> None:
                queue.append(_sse({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": chunk},
                        }
                    },
                }))

            def worker():
                try:
                    bridge.ask(
                        user_text,
                        model=sess.model,
                        cwd=sess.cwd,
                        on_chunk=on_chunk,
                        agent=None,
                    )
                except Exception as exc:  # noqa: BLE001
                    err_holder.append(str(exc))
                    traceback.print_exc()
                    queue.append(_sse({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": "\n⚠️ Cursor error: %s" % exc,
                                },
                            }
                        },
                    }))
                finally:
                    result = {"stopReason": "end_turn"}
                    if err_holder:
                        result = {
                            "stopReason": "end_turn",
                            "errorMessage": err_holder[0],
                        }
                    queue.append(_sse(result_msg(result)))
                    done.set()

            threading.Thread(target=worker, daemon=True).start()

            def gen():
                while True:
                    while queue:
                        yield queue.pop(0)
                    if done.is_set() and not queue:
                        break
                    done.wait(0.05)

            return StreamingResponse(gen(), media_type="text/event-stream")

        return StreamingResponse(
            iter([_sse(error_msg(-32601, "method not found: %s" % method))]),
            media_type="text/event-stream",
        )

    routes = [
        Route("/api/v1/health", health, methods=["GET"]),
        Route("/api/v1/runs", create_run, methods=["POST"]),
        Route("/api/v1/runs/{run_id}/stream", stream_run, methods=["GET"]),
        Route("/api/v1/acp/connect", acp_connect, methods=["POST"]),
        Route("/api/v1/acp", acp_rpc, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Cursor sidecar for RenderDoc AI panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--api-key", default=os.environ.get("CURSOR_API_KEY", ""))
    parser.add_argument("--cwd", default=os.environ.get(
        "CURSOR_SIDECAR_CWD", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))))
    parser.add_argument("--model", default=os.environ.get("CURSOR_SIDECAR_MODEL", DEFAULT_MODEL))
    args = parser.parse_args(argv)

    api_key = (args.api_key or "").strip().strip('"').strip("'")
    # BOM / CR from notepad or `echo` redirection
    if api_key.startswith("\ufeff"):
        api_key = api_key.lstrip("\ufeff")
    api_key = api_key.replace("\r", "").replace("\n", "").strip()

    if not api_key:
        raise SystemExit(
            "CURSOR_API_KEY is required.\n"
            "  run set_cursor_api_key.bat\n"
            "  or: set CURSOR_API_KEY=...\n"
            "Get a User API Key: https://cursor.com/dashboard/integrations"
        )

    try:
        import httpx  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "httpx is required. Install with:\n"
            "  pip install httpx\n"
            "Original error: %s" % exc
        )

    bridge = CursorBridge(api_key=api_key, cwd=args.cwd, default_model=args.model)
    app = build_app(bridge)
    # Masked fingerprint only — never print the full key.
    if len(api_key) >= 12:
        key_fp = "%s...%s (len=%d)" % (api_key[:4], api_key[-4:], len(api_key))
    else:
        key_fp = "(len=%d, unusually short)" % len(api_key)
    print("Cursor sidecar listening on http://%s:%d" % (args.host, args.port))
    print("  cwd=%s  model=%s" % (args.cwd, args.model))
    print("  api_key=%s" % key_fp)
    print("  Panel: Window → AI 助手 → 重新连接 (port %d)" % args.port)
    print("  Backend: Cloud Agents HTTP (avoids Windows WinError 10038 local Bridge)")
    print("  CodeBuddy unchanged — stop this process to use codebuddy --serve instead.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
