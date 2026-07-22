"""CodeBuddy ACP (Agent Client Protocol) client.

The simple ``/api/v1/runs`` gateway cannot choose a model. Model selection is
only possible over ACP (JSON-RPC 2.0 over SSE), which also gives streaming
``agent_message_chunk`` deltas. This client speaks ACP through the ctypes HTTP
client so it runs inside RenderDoc's socket-less Python.

Flow:
  POST /api/v1/acp/connect            -> {connectionId, sessionToken}
  POST /api/v1/acp  initialize
  POST /api/v1/acp  session/new  {cwd, mcpServers:[]}
        -> result.sessionId, result.models.availableModels/currentModelId
  POST /api/v1/acp  session/set_model {sessionId, modelId}
  POST /api/v1/acp  session/prompt   {sessionId, prompt:[{type:"text",text}]}
        -> SSE session/update notifications (agent_message_chunk) then result

All ACP POSTs require headers:
  X-CodeBuddy-Request: 1
  Accept: application/json, text/event-stream
  acp-connection-id: <connectionId>   (except /connect)
"""

import json

try:
    import http_ctypes
except ImportError:
    from . import http_ctypes

_ACC = "application/json, text/event-stream"

# Permission mode applied to every session. CodeBuddy defaults to "default"
# (Always Ask): the first time the agent uses a tool it emits a permission
# request and waits for the client to answer. Our one-shot HTTP client can't
# answer mid-stream, so the turn hangs forever ("思考中"). We must therefore use
# a non-interactive mode. "bypassPermissions" skips all prompts and lets the
# agent read/write files and run commands (so it can e.g. save a report),
# without ever hanging. Use the panel's "权限" dropdown (or change this) to pick
# a safer mode: "dontAsk" allows only safe read-only actions (writes denied),
# "acceptEdits" auto-accepts file edits but may hang on command tools.
DEFAULT_MODE = "bypassPermissions"


def _base_headers():
    return {"X-CodeBuddy-Request": "1", "Content-Type": "application/json"}


def _acp_headers(cid):
    h = _base_headers()
    h["Accept"] = _ACC
    h["acp-connection-id"] = cid
    return h


def _parse_sse_block(block):
    """Parse one SSE event block (bytes or str) into a JSON object, or None."""
    if isinstance(block, bytes):
        block = block.decode("utf-8", "replace")
    data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
    if not data_lines:
        return None
    payload = "".join(data_lines)
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def _content_text(content):
    """Extract text from an ACP content value (dict, list of dicts, or str)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_content_text(c) for c in content)
    if isinstance(content, dict):
        if content.get("type") in (None, "text") and "text" in content:
            return content.get("text", "")
        return ""
    return ""


def _tool_result_text(content):
    """Extract text from a tool_call/tool_call_update ``content`` value, whose
    items look like {"type": "content", "content": {"type": "text", "text": ...}}."""
    if not content:
        return ""
    if isinstance(content, dict):
        content = [content]
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(_content_text(item.get("content")))
    return "".join(parts)


def _truncate(s, n):
    s = s or ""
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


def _compact_json(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _is_mcp_tool(name):
    return bool(name) and (name.startswith("mcp") or "__" in name)


def _render_transcript(state, note=None):
    """Render the live turn (thoughts + tool/MCP calls + answer) as plain text
    for the panel's multiline text box."""
    lines = []

    thought = (state.get("thought") or "").strip()
    if thought:
        lines.append("💭 思考")
        lines.append(_truncate(thought, 4000))
        lines.append("")

    order = state.get("order") or []
    if order:
        lines.append("🔧 工具 / MCP 调用")
        for i, tid in enumerate(order, 1):
            t = state["tools"][tid]
            name = t.get("name") or t.get("title") or "tool"
            mcp = _is_mcp_tool(name)
            label = ("MCP · " + name) if mcp else name
            icon = "🔌" if mcp else "•"
            status = t.get("status") or "in_progress"
            lines.append("  %s %d. %s  [%s]" % (icon, i, label, status))
            if t.get("input"):
                lines.append("       参数: " + _truncate(_compact_json(t["input"]), 300))
            res = (t.get("result") or "").replace("\n", " ")
            if res.strip():
                lines.append("       结果: " + _truncate(res, 300))
        lines.append("")

    answer = (state.get("answer") or "").strip()
    if answer:
        lines.append("📝 回答")
        lines.append(state["answer"].rstrip())

    if note:
        if answer:
            lines.append("")
        lines.append(note)

    return "\n".join(lines).strip()


class AcpError(Exception):
    pass


class AcpSession(object):
    def __init__(self, port, host="127.0.0.1", cwd=".", mode=DEFAULT_MODE):
        self.port = port
        self.host = host
        self.cwd = cwd
        self.connection_id = None
        self.session_id = None
        self.models = []            # list of (modelId, name, description)
        self.current_model = None
        self.modes = []             # list of (modeId, name, description)
        self.current_mode = None
        self.desired_mode = mode
        self._rpc_id = 0

    def _next_id(self):
        self._rpc_id += 1
        return self._rpc_id

    def _rpc(self, method, params, timeout_ms, stop_marker=None, on_notify=None,
             cancel=None):
        rid = self._next_id()
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        captured = {"result": None, "error": None, "raw": []}

        def on_event(block):
            captured["raw"].append(
                block.decode("utf-8", "replace") if isinstance(block, bytes) else block)
            msg = _parse_sse_block(block)
            if msg is None:
                return False
            if msg.get("id") == rid:
                if "error" in msg:
                    captured["error"] = msg["error"]
                else:
                    captured["result"] = msg.get("result")
                return True  # our JSON-RPC reply arrived; stop reading
            if on_notify is not None and msg.get("method") == "session/update":
                on_notify(msg.get("params") or {})
            return False

        status = http_ctypes.stream(
            self.port, "POST", "/api/v1/acp", _acp_headers(self.connection_id), body,
            host=self.host, recv_timeout_ms=timeout_ms, on_event=on_event, cancel=cancel)
        if status not in (200, 202):
            raw = "".join(captured["raw"])[:400]
            raise AcpError("ACP %s HTTP %d: %s" % (method, status, raw))
        if captured["error"]:
            raise AcpError("ACP %s error: %s (%s)" % (
                method, captured["error"].get("message"), captured["error"].get("code")))
        return captured["result"]

    def connect(self):
        status, _, rbody = http_ctypes.request(
            self.port, "POST", "/api/v1/acp/connect", _base_headers(), "{}",
            host=self.host, recv_timeout_ms=8000)
        if status != 200:
            raise AcpError("ACP connect HTTP %d: %s" % (status, rbody[:200]))
        data = json.loads(rbody.decode("utf-8", "replace"))
        self.connection_id = data.get("connectionId") or data.get("data", {}).get("connectionId")
        if not self.connection_id:
            raise AcpError("ACP connect returned no connectionId")
        return self.connection_id

    def initialize(self):
        self._rpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "renderdoc", "version": "1.0"},
            "clientCapabilities": {},
        }, timeout_ms=15000)

    def new_session(self):
        result = self._rpc("session/new", {"cwd": self.cwd, "mcpServers": []},
                           timeout_ms=30000)
        if not result:
            raise AcpError("session/new returned no result")
        self.session_id = result.get("sessionId")
        models = (result.get("models") or {})
        self.current_model = models.get("currentModelId")
        self.models = [(m.get("modelId"), m.get("name"), m.get("description", ""))
                       for m in models.get("availableModels", [])]
        modes = (result.get("modes") or {})
        self.current_mode = modes.get("currentModeId")
        self.modes = [(m.get("id"), m.get("name"), m.get("description", ""))
                      for m in modes.get("availableModes", [])]
        return self.session_id

    def open(self):
        """Full handshake: connect + initialize + new session + non-interactive mode."""
        self.connect()
        self.initialize()
        self.new_session()
        self._apply_desired_mode()
        return self

    def _apply_desired_mode(self):
        """Switch off the interactive "Always Ask" permission mode so tool use
        never blocks waiting for a prompt answer we can't provide."""
        target = self.desired_mode
        if not target or target == self.current_mode:
            return
        available = [m[0] for m in self.modes]
        if self.modes and target not in available:
            return
        try:
            self._rpc("session/set_mode", {"sessionId": self.session_id, "modeId": target},
                      timeout_ms=15000)
            self.current_mode = target
        except AcpError:
            pass

    def set_mode(self, mode_id):
        self.desired_mode = mode_id
        if not mode_id or mode_id == self.current_mode:
            return
        try:
            self._rpc("session/set_mode", {"sessionId": self.session_id, "modeId": mode_id},
                      timeout_ms=15000)
        except AcpError as exc:
            if not self._is_connection_lost(exc):
                raise
            self.reopen()
            return
        self.current_mode = mode_id

    def reopen(self):
        """Re-establish a dropped ACP connection/session, keeping the model.

        CodeBuddy expires an idle ACP connection server-side; once that happens
        every request fails with "Connection not found". We transparently redo
        the handshake and re-apply the previously selected model. Conversation
        history on the server is lost, but the panel keeps working."""
        prev_model = self.current_model
        self.connection_id = None
        self.session_id = None
        self.open()
        if prev_model and prev_model != self.current_model:
            try:
                self._rpc("session/set_model",
                          {"sessionId": self.session_id, "modelId": prev_model},
                          timeout_ms=15000)
                self.current_model = prev_model
            except AcpError:
                pass
        return self

    @staticmethod
    def _is_connection_lost(exc):
        s = str(exc)
        return ("Connection not found" in s) or ("-32000" in s)

    def set_model(self, model_id):
        if not model_id or model_id == self.current_model:
            return
        try:
            self._rpc("session/set_model", {"sessionId": self.session_id, "modelId": model_id},
                      timeout_ms=15000)
        except AcpError as exc:
            if not self._is_connection_lost(exc):
                raise
            self.reopen()
            self._rpc("session/set_model", {"sessionId": self.session_id, "modelId": model_id},
                      timeout_ms=15000)
        self.current_model = model_id

    def cancel(self):
        """Ask CodeBuddy to cancel the current run (ACP ``session/cancel``
        notification). Best-effort; safe to call from another thread."""
        if not self.connection_id or not self.session_id:
            return
        body = json.dumps({"jsonrpc": "2.0", "method": "session/cancel",
                           "params": {"sessionId": self.session_id}})
        try:
            http_ctypes.request(
                self.port, "POST", "/api/v1/acp", _acp_headers(self.connection_id), body,
                host=self.host, recv_timeout_ms=5000)
        except Exception:  # noqa: BLE001
            pass

    def prompt(self, text, on_update=None, timeout_ms=240000, cancel=None):
        try:
            return self._prompt_once(text, on_update, timeout_ms, cancel)
        except AcpError as exc:
            cancelled = cancel is not None and cancel.cancelled()
            if cancelled or not self._is_connection_lost(exc):
                raise
            # Idle connection expired server-side; reconnect and try once more.
            self.reopen()
            return self._prompt_once(text, on_update, timeout_ms, cancel)

    def _prompt_once(self, text, on_update=None, timeout_ms=240000, cancel=None):
        state = {"thought": "", "answer": "", "order": [], "tools": {}}
        meta = {"stopReason": None, "error": None, "interrupted": False}

        def touch_tool(update):
            tid = update.get("toolCallId") or "?"
            t = state["tools"].get(tid)
            if t is None:
                t = {"name": "", "title": "", "kind": "", "input": None,
                     "result": "", "status": ""}
                state["tools"][tid] = t
                state["order"].append(tid)
            m = update.get("_meta") or {}
            name = m.get("codebuddy.ai/toolName") or update.get("toolName")
            if name:
                t["name"] = name
            if update.get("title"):
                t["title"] = update["title"]
            if update.get("kind"):
                t["kind"] = update["kind"]
            if update.get("status"):
                t["status"] = update["status"]
            raw = update.get("rawInput")
            if not raw:
                raw = update.get("input")
            if raw:
                t["input"] = raw
            res = _tool_result_text(update.get("content"))
            if res:
                t["result"] = (t["result"] + res)[:2000]
            return t

        def emit():
            if on_update:
                on_update(_render_transcript(state))

        def on_notify(params):
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                t = _content_text(update.get("content"))
                if t:
                    state["answer"] += t
                    emit()
            elif kind == "agent_thought_chunk":
                t = _content_text(update.get("content"))
                if t:
                    state["thought"] += t
                    emit()
            elif kind in ("tool_call", "tool_call_update"):
                touch_tool(update)
                emit()
            elif kind == "interruption_request":
                meta["interrupted"] = True

        result = self._rpc(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
            timeout_ms=timeout_ms, stop_marker=b'"stopReason"', on_notify=on_notify,
            cancel=cancel)
        if isinstance(result, dict):
            meta["stopReason"] = result.get("stopReason")
            meta["error"] = result.get("errorMessage")
        # Any tool still marked "running" completed once the turn ended.
        for tid in state["order"]:
            t = state["tools"][tid]
            if t["status"] in ("", "in_progress", "pending"):
                t["status"] = "completed"
        self.last_meta = meta
        self.last_state = state

        cancelled = cancel is not None and cancel.cancelled()
        note = None
        if cancelled:
            note = "（已取消）"
        elif not state["answer"].strip():
            note = self._explain_empty(state, meta)
        return _render_transcript(state, note=note)

    @staticmethod
    def _explain_empty(state, meta):
        if meta["error"]:
            return "⚠️ CodeBuddy 返回错误：%s" % meta["error"]
        if meta["interrupted"]:
            return ("CodeBuddy 需要操作授权（权限模式为“Always Ask”），本轮已挂起且无文本回复。"
                    "可在 CodeBuddy 端将权限模式改为 dontAsk/bypassPermissions 后重试。")
        reason = meta["stopReason"]
        if reason == "refusal":
            return ("CodeBuddy 拒绝了该请求（refusal）。"
                    "常见原因：当前模型的安全策略拦截了提示，或会话权限/模型不适合工具型分析。"
                    "可换一个模型后重试；耗时类问题扩展会自动改用精简提示或展示本地采样结果。")
        if reason == "cancelled":
            return "本次请求被取消（cancelled）。"
        if state["order"]:
            return "（模型调用了工具但没有产生文本回复。）"
        if state["thought"].strip():
            return "（模型只产生了思考内容，没有正式回复。）"
        return "(CodeBuddy 没有返回文本内容；stopReason=%s)" % reason
