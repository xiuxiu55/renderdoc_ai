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

from renderdoc_mcp.cursor_sidecar.cloud_http import CloudAgentsClient, clean_text


DEFAULT_MODEL = "composer-2.5"
DEFAULT_MODES = [
    {"id": "bypassPermissions", "name": "bypassPermissions",
     "description": "Non-interactive (sidecar default)"},
    {"id": "dontAsk", "name": "dontAsk", "description": "Read-oriented"},
]


def _jid() -> str:
    return str(uuid.uuid4())


def _clean_text(s: str) -> str:
    return clean_text(s)


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
    agent_id: Optional[str] = None  # reused Cloud Agent across turns
    lock: threading.Lock = field(default_factory=threading.Lock)


class CursorBridge:
    """Cloud Agents HTTP bridge for the RenderDoc panel.

    Pure HTTPS (httpx) — no local cursor-sdk Bridge, so Windows avoids
    WinError 10038. Sessions reuse one cloud agent_id; replies stream via SSE.
    """

    def __init__(self, api_key: str, cwd: str, default_model: str = DEFAULT_MODEL):
        self.api_key = api_key
        self.cwd = cwd or os.getcwd()
        self.default_model = default_model
        self.connections: Dict[str, Dict[str, SessionState]] = {}
        self.runs: Dict[str, RunState] = {}
        self._models_cache: Optional[List[dict]] = None
        self._cloud = CloudAgentsClient(api_key)
        self._ask_lock = threading.Lock()  # one cloud run at a time per sidecar

    # -- models -----------------------------------------------------------

    def list_models(self) -> List[dict]:
        if self._models_cache is not None:
            return self._models_cache
        # Live catalog for this API key (GET /v1/models); falls back if offline.
        out = self._cloud.list_models()
        if not out:
            out = [
                {"modelId": DEFAULT_MODEL, "name": DEFAULT_MODEL,
                 "description": "Default Cursor model"},
                {"modelId": "auto", "name": "Auto", "description": "Server-selected"},
            ]
        self._models_cache = out
        return out

    # -- agent ------------------------------------------------------------

    def ask(self, text: str, model: Optional[str] = None,
            cwd: Optional[str] = None,
            on_chunk: Optional[Callable[[str], None]] = None,
            agent_id: Optional[str] = None,
            sess: Optional[SessionState] = None) -> str:
        """Ask via Cloud Agents; stream chunks; reuse sess.agent_id when set."""
        del cwd  # cloud no-repo path does not use local cwd
        model = model or self.default_model
        text = _clean_text(text or "")
        reuse = agent_id
        if sess is not None:
            with sess.lock:
                reuse = sess.agent_id or agent_id

        streamed: List[str] = []

        def _wrapped(chunk: str) -> None:
            if chunk:
                streamed.append(chunk)
            if on_chunk and chunk:
                on_chunk(chunk)

        with self._ask_lock:
            answer, new_id = self._cloud.ask(
                text,
                model=model,
                agent_id=reuse,
                on_chunk=_wrapped if on_chunk else None,
            )
        if sess is not None and new_id:
            with sess.lock:
                sess.agent_id = new_id
        # If SSE had no assistant deltas, still push the final text once.
        if on_chunk and answer and not streamed:
            on_chunk(answer)
        return answer

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
            agent_id=None,
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
            # Model change → next ask creates a fresh cloud agent.
            sess.agent_id = None


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
                    # Stream tokens as they arrive; reuse cloud agent after turn 1.
                    bridge.ask(
                        user_text,
                        model=sess.model,
                        cwd=sess.cwd,
                        on_chunk=on_chunk,
                        sess=sess,
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
                    done.wait(0.02)

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

    try:
        code, info = bridge._cloud.probe_me()
        if code == 200:
            print(
                "  api_key OK via /v1/me (name=%s)"
                % (info.get("apiKeyName") or info.get("userEmail") or "ok")
            )
        else:
            print("  WARNING: /v1/me HTTP %s — %s" % (code, str(info)[:200]))
            print(
                "  Get a Cloud Agents User API Key:\n"
                "    https://cursor.com/dashboard?tab=cloud-agents\n"
                "    → My Settings → API Keys  (or https://cursor.com/dashboard/api)"
            )
    except Exception as exc:  # noqa: BLE001
        print("  WARNING: could not probe /v1/me: %s" % exc)

    try:
        models = bridge.list_models()
        print("  models: %d available (from /v1/models or fallback)" % len(models))
        preview = ", ".join(m.get("name") or m.get("modelId") for m in models[:8])
        if len(models) > 8:
            preview += ", ..."
        print("  e.g. %s" % preview)
    except Exception as exc:  # noqa: BLE001
        print("  WARNING: list_models failed: %s" % exc)

    print("  Panel: Window → AI 助手 → 重新连接 (port %d)" % args.port)
    print("  Backend: Cloud Agents HTTP + SSE stream, agent reuse per session")
    print("  CodeBuddy unchanged — stop this process to use codebuddy --serve instead.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
