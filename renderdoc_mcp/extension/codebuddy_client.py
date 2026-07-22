"""Client for CodeBuddy's ``--serve`` HTTP API, using the ctypes HTTP client so
it runs inside RenderDoc's socket-less Python.

Protocol (discovered from a live ``codebuddy --serve --port 8080``):
  POST /api/v1/runs
      headers: X-CodeBuddy-Request: 1
      body:    {"id": "<uuid>", "type": "text", "text": "...",
                "sender": {"id": "...", "name": "..."}}
      -> 202   {"data": {"runId": "...", "status": "accepted"}}
  GET  /api/v1/runs/{runId}/stream   (Server-Sent Events)
      event: message
      data: {"status": "completed", "content": {"markdown": "..."},
             "agent": {"toolCalls": [...]}}
      event: done
      data: {}
"""

import json

try:
    import http_ctypes
except ImportError:
    from . import http_ctypes

_HDR_POST = {"X-CodeBuddy-Request": "1", "Content-Type": "application/json"}
_HDR_GET = {"X-CodeBuddy-Request": "1"}


def _uuid():
    import os
    b = os.urandom(16)
    h = "".join("%02x" % c for c in bytearray(b))
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def health(port, host="127.0.0.1", timeout_ms=4000):
    try:
        status, _, body = http_ctypes.request(
            port, "GET", "/api/v1/health", _HDR_GET, b"", host=host,
            recv_timeout_ms=timeout_ms)
    except Exception:
        return False
    if status != 200:
        return False
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        return data.get("data", {}).get("status") == "ok"
    except ValueError:
        return False


def _parse_sse(body_bytes, on_update=None):
    text = body_bytes.decode("utf-8", "replace")
    final = ""
    completed = None
    tool_calls = []
    for block in text.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        payload = "".join(data_lines)
        if not payload or payload == "{}":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        agent = obj.get("agent") or {}
        for tc in agent.get("toolCalls", []) or []:
            tool_calls.append(tc)
        content = obj.get("content") or {}
        md = content.get("markdown")
        if md is not None:
            final = md
            if on_update:
                on_update(md)
            if obj.get("status") == "completed":
                completed = md
    return completed if completed is not None else final, tool_calls


def send(port, text, sender_id="renderdoc", sender_name="RenderDoc",
         host="127.0.0.1", on_update=None, timeout_ms=180000):
    """Send a prompt to CodeBuddy and return (reply_markdown, tool_calls)."""
    msg_id = _uuid()
    body = json.dumps({
        "id": msg_id,
        "type": "text",
        "text": text,
        "sender": {"id": sender_id, "name": sender_name},
    })

    status, _, rbody = http_ctypes.request(
        port, "POST", "/api/v1/runs", _HDR_POST, body, host=host,
        recv_timeout_ms=30000)
    if status != 202:
        raise RuntimeError("CodeBuddy /runs returned HTTP %d: %s"
                           % (status, rbody.decode("utf-8", "replace")[:500]))
    data = json.loads(rbody.decode("utf-8", "replace"))
    run_id = data.get("data", {}).get("runId")
    if not run_id:
        raise RuntimeError("CodeBuddy /runs gave no runId: %s" % rbody[:300])

    _, _, sbody = http_ctypes.request(
        port, "GET", "/api/v1/runs/%s/stream" % run_id, _HDR_GET, b"", host=host,
        recv_timeout_ms=timeout_ms, stop_marker=b"event: done")
    return _parse_sse(sbody, on_update=on_update)
