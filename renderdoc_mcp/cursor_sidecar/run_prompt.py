"""Ask Cursor via Cloud Agents HTTP API (no local Bridge).

The local cursor-sdk Bridge uses select() on stderr pipes, which raises
WinError 10038 on Windows. Cloud /v1/agents is pure HTTPS and works here.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from base64 import b64encode

try:
    import httpx
except ImportError:
    print(json.dumps({"ok": False, "error": "httpx required: pip install httpx"}, ensure_ascii=True))
    sys.exit(2)

API = os.environ.get("CURSOR_API_BASE", "https://api.cursor.com").rstrip("/")


def _clean_text(s: str) -> str:
    """Drop lone UTF-16 surrogates that cannot be UTF-8 encoded (common on Windows)."""
    if not s:
        return ""
    return "".join(
        ch if not (0xD800 <= ord(ch) <= 0xDFFF) else "\ufffd"
        for ch in s
    )


def _out(payload: dict) -> None:
    # ensure_ascii avoids pipe/console encoding surprises on Windows.
    print(json.dumps(payload, ensure_ascii=True))


def _auth_header(api_key: str) -> dict:
    token = b64encode(("%s:" % api_key).encode("utf-8")).decode("ascii")
    return {
        "Authorization": "Basic %s" % token,
        "Content-Type": "application/json",
    }


def _create_agent(client, api_key: str, text: str, model: str) -> dict:
    body = {
        "prompt": {"text": text},
        "name": "RenderDoc AI panel",
        # Omit repos/env → no-repo cloud agent (chat-style, no GitHub required).
    }
    if model and model not in ("auto", "default"):
        body["model"] = {"id": model}
    r = client.post("%s/v1/agents" % API, headers=_auth_header(api_key), json=body)
    # Unknown model IDs → retry with server default.
    if r.status_code >= 400 and "model" in body:
        body.pop("model", None)
        r = client.post("%s/v1/agents" % API, headers=_auth_header(api_key), json=body)
    if r.status_code >= 400:
        hint = ""
        if r.status_code == 401:
            hint = (
                " | Fix: run set_cursor_api_key.bat with a fresh User API Key from "
                "https://cursor.com/dashboard/integrations (API Keys). "
                "Old .cursor_api_key files often have a trailing space from echo."
            )
        raise RuntimeError(
            "create agent HTTP %s: %s%s" % (r.status_code, r.text[:800], hint)
        )
    return r.json()


def _get_run(client, api_key: str, agent_id: str, run_id: str) -> dict:
    url = "%s/v1/agents/%s/runs/%s" % (API, agent_id, run_id)
    r = client.get(url, headers=_auth_header(api_key))
    if r.status_code >= 400:
        raise RuntimeError("get run HTTP %s: %s" % (r.status_code, r.text[:800]))
    return r.json()


def _wait_run(client, api_key: str, agent_id: str, run_id: str, timeout_s: float = 280.0) -> dict:
    terminal = ("FINISHED", "ERROR", "CANCELLED", "EXPIRED")
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        last = _get_run(client, api_key, agent_id, run_id)
        status = (last.get("status") or "").upper()
        if status in terminal:
            return last
        time.sleep(1.5)
    raise RuntimeError("Timed out waiting for cloud run; last=%s" % json.dumps(last)[:400])


def main() -> int:
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not api_key:
        _out({"ok": False, "error": "CURSOR_API_KEY missing"})
        return 2
    model = os.environ.get("CURSOR_SIDECAR_MODEL", "composer-2.5").strip() or "composer-2.5"
    # Binary stdin + replace: avoid locale/cp936 turning UTF-8 into surrogates.
    raw = sys.stdin.buffer.read()
    text = _clean_text(raw.decode("utf-8", "replace"))
    if not text.strip():
        _out({"ok": False, "error": "empty prompt"})
        return 2

    prompt = _clean_text(
        "You are helping a user inside a RenderDoc graphics debugger AI panel. "
        "Answer in Chinese unless they write in English. Be concise and technical.\n\n"
        "User message:\n%s" % text
    )

    try:
        with httpx.Client(timeout=60.0) as client:
            created = _create_agent(client, api_key, prompt, model)
            agent = created.get("agent") or {}
            run = created.get("run") or {}
            agent_id = agent.get("id")
            run_id = run.get("id") or agent.get("latestRunId")
            if not agent_id or not run_id:
                raise RuntimeError(
                    "create response missing agent/run id: %s"
                    % _clean_text(json.dumps(created, ensure_ascii=True)[:500])
                )
            done = _wait_run(client, api_key, agent_id, run_id)
            status = (done.get("status") or "").upper()
            result = _clean_text(done.get("result") or "")
            if status != "FINISHED":
                _out({
                    "ok": False,
                    "error": _clean_text("Cloud run %s: %s" % (status, result or done)),
                    "agentId": agent_id,
                    "runId": run_id,
                })
                return 1
            _out({
                "ok": True,
                "text": result,
                "status": status,
                "agentId": agent_id,
                "runId": run_id,
                "backend": "cloud-http",
            })
            return 0
    except Exception as exc:
        _out({
            "ok": False,
            "error": _clean_text(str(exc)),
            "trace": _clean_text(traceback.format_exc()[-1500:]),
            "backend": "cloud-http",
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
