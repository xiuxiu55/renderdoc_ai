"""CLI/one-shot wrapper around CloudAgentsClient (debug / legacy subprocess)."""
from __future__ import annotations

import json
import os
import sys
import traceback

try:
    from renderdoc_mcp.cursor_sidecar.cloud_http import CloudAgentsClient, clean_text
except ImportError:
    # Allow `python run_prompt.py` from this directory.
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from renderdoc_mcp.cursor_sidecar.cloud_http import CloudAgentsClient, clean_text


def _out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True))


def main() -> int:
    api_key = os.environ.get("CURSOR_API_KEY", "").strip().strip('"').strip("'")
    api_key = api_key.replace("\r", "").replace("\n", "").strip()
    if api_key.startswith("\ufeff"):
        api_key = api_key.lstrip("\ufeff")
    if not api_key:
        _out({"ok": False, "error": "CURSOR_API_KEY missing"})
        return 2

    model = os.environ.get("CURSOR_SIDECAR_MODEL", "composer-2.5").strip() or "composer-2.5"
    agent_id = os.environ.get("CURSOR_SIDECAR_AGENT_ID", "").strip() or None
    text = clean_text(sys.stdin.buffer.read().decode("utf-8", "replace"))
    if not text.strip():
        _out({"ok": False, "error": "empty prompt"})
        return 2

    client = CloudAgentsClient(api_key)
    try:
        answer, agent_id = client.ask(text, model=model, agent_id=agent_id)
        _out({
            "ok": True,
            "text": answer,
            "agentId": agent_id,
            "backend": "cloud-http",
        })
        return 0
    except Exception as exc:
        _out({
            "ok": False,
            "error": clean_text(str(exc)),
            "trace": clean_text(traceback.format_exc()[-1500:]),
            "backend": "cloud-http",
        })
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
