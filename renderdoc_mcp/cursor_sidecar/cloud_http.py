"""Cursor Cloud Agents HTTP client (no local Bridge)."""
from __future__ import annotations

import json
import time
from base64 import b64encode
from typing import Callable, List, Optional, Tuple

import httpx

API_DEFAULT = "https://api.cursor.com"

_TIMEOUT = httpx.Timeout(connect=20.0, read=180.0, write=60.0, pool=20.0)
_STREAM_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)

SYSTEM_BOOT = (
    "You are helping inside a RenderDoc AI panel. "
    "Reply in Chinese unless the user writes in English. Be concise and technical."
)

# Fallback if GET /v1/models is unavailable.
FALLBACK_MODELS = [
    ("composer-2.5", "Composer 2.5", "Default Cursor coding model"),
    ("composer-2", "Composer 2", "Composer 2"),
    ("composer-2-fast", "Composer 2 Fast", "Faster Composer 2"),
    ("claude-4.6-sonnet", "Claude 4.6 Sonnet", "Anthropic Sonnet"),
    ("claude-4.6-sonnet-thinking", "Claude 4.6 Sonnet Thinking", "Sonnet with thinking"),
    ("claude-4.6-opus", "Claude 4.6 Opus", "Anthropic Opus"),
    ("claude-4.5-sonnet", "Claude 4.5 Sonnet", "Anthropic Sonnet 4.5"),
    ("claude-4.5-opus-high", "Claude 4.5 Opus", "Anthropic Opus 4.5"),
    ("gpt-5.1", "GPT-5.1", "OpenAI GPT-5.1"),
    ("gpt-5-mini", "GPT-5 Mini", "OpenAI GPT-5 Mini"),
    ("gemini-3-flash", "Gemini 3 Flash", "Google Gemini Flash"),
    ("gemini-3-pro", "Gemini 3 Pro", "Google Gemini Pro"),
    ("grok-4-20", "Grok 4", "xAI Grok"),
    ("kimi-k2.5", "Kimi K2.5", "Moonshot Kimi"),
    ("auto", "Auto", "Server-selected model"),
]

OnChunk = Optional[Callable[[str], None]]


def clean_text(s: str) -> str:
    if not s:
        return ""
    return "".join(
        ch if not (0xD800 <= ord(ch) <= 0xDFFF) else "\ufffd"
        for ch in s
    )


def _auth_headers(api_key: str, scheme: str = "basic") -> dict:
    if scheme == "bearer":
        auth = "Bearer %s" % api_key
    else:
        token = b64encode(("%s:" % api_key).encode("utf-8")).decode("ascii")
        auth = "Basic %s" % token
    return {"Authorization": auth, "Content-Type": "application/json"}


class CloudAgentsClient:
    def __init__(self, api_key: str, api_base: str = API_DEFAULT):
        self.api_key = (api_key or "").strip()
        self.api = (api_base or API_DEFAULT).rstrip("/")
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._scheme = "basic"

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last = None
        schemes = (self._scheme, "bearer" if self._scheme == "basic" else "basic")
        seen = []
        for s in schemes:
            if s not in seen:
                seen.append(s)
        for scheme in seen:
            r = self._client.request(
                method, url, headers=_auth_headers(self.api_key, scheme), **kwargs
            )
            last = r
            if r.status_code != 401:
                self._scheme = scheme
                return r
        return last

    def probe_me(self) -> Tuple[int, dict]:
        r = self._request("GET", "%s/v1/me" % self.api, timeout=30.0)
        try:
            body = r.json()
        except Exception:
            body = {"raw": (r.text or "")[:200]}
        return r.status_code, body if isinstance(body, dict) else {"raw": body}

    def list_models(self) -> List[dict]:
        """Return ACP-shaped models from GET /v1/models (account catalog)."""
        try:
            r = self._request("GET", "%s/v1/models" % self.api, timeout=45.0)
            if r.status_code >= 400:
                raise RuntimeError("HTTP %s" % r.status_code)
            data = r.json() or {}
            items = data.get("items") or data.get("models") or []
            out = []
            seen = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id") or item.get("modelId")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                name = item.get("displayName") or item.get("name") or mid
                desc = item.get("description") or ""
                # Prefer default variant display name when present.
                variants = item.get("variants") or []
                for v in variants:
                    if isinstance(v, dict) and v.get("isDefault") and v.get("displayName"):
                        name = v.get("displayName")
                        break
                out.append({
                    "modelId": mid,
                    "name": name,
                    "description": desc or name,
                })
            if out:
                if "auto" not in seen:
                    out.append({
                        "modelId": "auto",
                        "name": "Auto",
                        "description": "Server-selected model",
                    })
                return out
        except Exception as exc:
            print("[cursor_sidecar] GET /v1/models failed: %s — using fallback" % exc)

        return [
            {"modelId": mid, "name": name, "description": desc}
            for mid, name, desc in FALLBACK_MODELS
        ]

    def _model_body(self, model: str) -> Optional[dict]:
        if not model or model in ("auto", "default"):
            return None
        body = {"id": model}
        # Only Composer family benefits from the fast flag by default.
        if model.startswith("composer"):
            body["params"] = [{"id": "fast", "value": "true"}]
        return body

    def create_agent(self, text: str, model: str) -> Tuple[str, str]:
        body = {
            "prompt": {"text": text},
            "name": "RenderDoc AI panel",
        }
        mb = self._model_body(model)
        if mb:
            body["model"] = mb
        r = self._request("POST", "%s/v1/agents" % self.api, json=body)
        if r.status_code >= 400 and "model" in body:
            body.pop("model", None)
            r = self._request("POST", "%s/v1/agents" % self.api, json=body)
        if r.status_code >= 400:
            raise RuntimeError("create agent HTTP %s: %s" % (r.status_code, r.text[:800]))
        data = r.json()
        agent = data.get("agent") or {}
        run = data.get("run") or {}
        agent_id = agent.get("id")
        run_id = run.get("id") or agent.get("latestRunId")
        if not agent_id or not run_id:
            raise RuntimeError("create missing ids: %s" % json.dumps(data)[:400])
        return agent_id, run_id

    def create_run(self, agent_id: str, text: str) -> str:
        body = {"prompt": {"text": text}}
        url = "%s/v1/agents/%s/runs" % (self.api, agent_id)
        for attempt in range(8):
            r = self._request("POST", url, json=body)
            if r.status_code == 409:
                time.sleep(1.0 + attempt * 0.5)
                continue
            if r.status_code >= 400:
                raise RuntimeError(
                    "create run HTTP %s: %s" % (r.status_code, r.text[:800])
                )
            run = (r.json() or {}).get("run") or {}
            run_id = run.get("id")
            if not run_id:
                raise RuntimeError("create run missing id: %s" % r.text[:400])
            return run_id
        raise RuntimeError("create run: agent busy too long")

    def get_run(self, agent_id: str, run_id: str) -> dict:
        url = "%s/v1/agents/%s/runs/%s" % (self.api, agent_id, run_id)
        r = self._request("GET", url, timeout=httpx.Timeout(20.0, read=60.0))
        if r.status_code >= 400:
            raise RuntimeError("get run HTTP %s: %s" % (r.status_code, r.text[:800]))
        return r.json()

    def stream_run(
        self,
        agent_id: str,
        run_id: str,
        on_chunk: OnChunk = None,
        timeout_s: float = 280.0,
    ) -> str:
        url = "%s/v1/agents/%s/runs/%s/stream" % (self.api, agent_id, run_id)
        parts = []
        terminal = ("FINISHED", "ERROR", "CANCELLED", "EXPIRED")
        deadline = time.monotonic() + timeout_s

        try:
            with self._client.stream(
                "GET",
                url,
                headers=_auth_headers(self.api_key, self._scheme),
                timeout=_STREAM_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(
                        "stream HTTP %s: %s" % (resp.status_code, resp.read()[:400])
                    )
                event = "message"
                data_buf = []
                for line in resp.iter_lines():
                    if time.monotonic() > deadline:
                        break
                    if line is None:
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip() or "message"
                        continue
                    if line.startswith("data:"):
                        data_buf.append(line[5:].lstrip())
                        continue
                    if line.strip() == "":
                        if not data_buf:
                            event = "message"
                            continue
                        raw = "\n".join(data_buf)
                        data_buf = []
                        try:
                            payload = json.loads(raw) if raw else {}
                        except ValueError:
                            payload = {}
                        if not isinstance(payload, dict):
                            payload = {}

                        if event == "assistant":
                            t = clean_text(payload.get("text") or "")
                            if t:
                                parts.append(t)
                                if on_chunk:
                                    on_chunk(t)
                        elif event == "result":
                            status = (payload.get("status") or "").upper()
                            text = clean_text(payload.get("text") or "".join(parts))
                            if status and status != "FINISHED":
                                raise RuntimeError(
                                    "Cloud run %s: %s" % (status, text or payload)
                                )
                            return text or "".join(parts)
                        elif event == "error":
                            raise RuntimeError(
                                "stream error: %s"
                                % (payload.get("message") or payload)
                            )
                        elif event == "done":
                            return "".join(parts)
                        event = "message"
        except RuntimeError:
            raise
        except Exception as exc:
            print("[cursor_sidecar] SSE stream fallback to poll: %s" % exc)

        last = {}
        while time.monotonic() < deadline:
            last = self.get_run(agent_id, run_id)
            status = (last.get("status") or "").upper()
            if status in terminal:
                result = clean_text(last.get("result") or "".join(parts))
                if status != "FINISHED":
                    raise RuntimeError("Cloud run %s: %s" % (status, result or last))
                if result and on_chunk and not parts:
                    on_chunk(result)
                return result
            time.sleep(1.0)
        raise RuntimeError(
            "Timed out waiting for cloud run; last=%s"
            % clean_text(json.dumps(last, ensure_ascii=True)[:400])
        )

    def ask(
        self,
        text: str,
        model: str,
        agent_id: Optional[str] = None,
        on_chunk: OnChunk = None,
    ) -> Tuple[str, str]:
        text = clean_text(text or "").strip()
        if not text:
            raise RuntimeError("empty prompt")

        if agent_id:
            run_id = self.create_run(agent_id, text)
            answer = self.stream_run(agent_id, run_id, on_chunk=on_chunk)
            return answer or "(Cursor returned empty response)", agent_id

        boot = "%s\n\nUser:\n%s" % (SYSTEM_BOOT, text)
        agent_id, run_id = self.create_agent(boot, model)
        answer = self.stream_run(agent_id, run_id, on_chunk=on_chunk)
        return answer or "(Cursor returned empty response)", agent_id