"""Intent router: playbook match -> catalog keyword score -> intent class.

Python 3.6 compatible.
"""

from __future__ import print_function

import re

from . import registry

try:
    from renderdoc_mcp.playbook import match_question
except ImportError:
    try:
        from playbook import match_question  # type: ignore
    except ImportError:
        match_question = None


_STAGE_MAP = {
    "vs": "Vertex", "vertex": "Vertex", "顶点": "Vertex",
    "hs": "Hull", "hull": "Hull",
    "ds": "Domain", "domain": "Domain",
    "gs": "Geometry", "geometry": "Geometry", "几何": "Geometry",
    "ps": "Pixel", "pixel": "Pixel", "fragment": "Pixel", "片元": "Pixel", "像素": "Pixel",
    "cs": "Compute", "compute": "Compute", "计算": "Compute",
}

# Identity / help — never run capture tools or Cloud LLM.
_META_RE = re.compile(
    r"(?:"
    r"你是什么模型|你的模型|模型名称|模型名字|哪个模型|什么模型|"
    r"你是谁|你叫什么|你会什么|你能做什么|怎么用(?:你|这个|面板|助手)?|"
    r"who\s+are\s+you|what\s+model|model\s*name|"
    r"^(?:帮助|help|你好|您好|hello|hi|hey|谢谢|thanks|再见|bye|ok|好的)[\s!！。.?？]*$"
    r")",
    re.I,
)


def is_meta_or_chitchat(text):
    """True for greetings / model-identity / help — local canned reply only."""
    t = (text or "").strip()
    if not t:
        return False
    if _META_RE.search(t):
        return True
    if len(t) <= 16 and re.search(r"你好|您好|hello|hi|thanks|谢谢", t, re.I):
        return True
    return False


def extract_slots(text):
    """Pull structured slots from free text."""
    t = text or ""
    slots = {}
    m = re.search(r"(?:EID|eid|event(?:Id)?|事件)\s*[#:=]?\s*(\d+)", t, re.I)
    if not m:
        m = re.search(r"\b(\d{2,7})\b", t)
        if m and re.search(r"事件|eid|draw|drawcall|action", t, re.I):
            slots["event_id"] = int(m.group(1))
    else:
        slots["event_id"] = int(m.group(1))

    tl = t.lower()
    for key, stage in _STAGE_MAP.items():
        if key in tl or key in t:
            slots["stage"] = stage
            break

    m = re.search(r"(?:纹理|texture|rt|render\s*target)[:：\s]+([A-Za-z0-9_./\\-]+)", t, re.I)
    if m:
        slots["resource_name"] = m.group(1)
    return slots


def _score_intents(text):
    """Score each catalog intent by keyword hits + priority.

    Returns list of (score, intent, hits) sorted descending.
    """
    t = (text or "").strip()
    if not t:
        return []
    tl = t.lower()
    scored = []
    meta = registry.intent_meta()
    for intent, info in meta.items():
        if intent in ("general", "chitchat"):
            continue
        kws = info.get("keywords") or []
        hits = 0
        for kw in kws:
            if not kw:
                continue
            if kw.lower() in tl or kw in t:
                hits += 1
        if hits <= 0:
            continue
        score = hits * 1000 + int(info.get("priority") or 0)
        scored.append((score, intent, hits))
    scored.sort(key=lambda x: (-x[0], -x[2], x[1]))
    return scored


def classify_intent(text, slots=None):
    """Map free text to an intent class using tools_catalog.json keywords."""
    t = (text or "").strip()
    if not t:
        return "general"
    slots = slots or {}

    if is_meta_or_chitchat(t):
        return "chitchat"

    if re.search(
        r"为什么|为何|怎么这么慢|why\s*(is\s*)?(it\s*)?slow|解释原因",
        t,
        re.I,
    ) and re.search(r"慢|卡|耗时|瓶颈|duration|stall", t, re.I):
        return "why_slow"

    scored = _score_intents(t)
    if scored:
        best_score, best_intent, hits = scored[0]
        if slots.get("event_id") is not None and best_intent not in (
            "event", "shader", "pipeline", "why_slow", "timing", "cbuffer",
            "black_screen",
        ):
            for s, intent, h in scored:
                if intent == "event":
                    return "event"
        return best_intent

    if slots.get("event_id") is not None or re.search(
            r"当前.*事件|这个事件|eid|event\s*browser|chunk|参数", t, re.I):
        return "event"
    return "general"


def route(text, path="panel"):
    """Return routing decision dict.

    Keys: kind (playbook|plan|chitchat), question_id?, intent?, slots, confidence
    """
    t = (text or "").strip()
    slots = extract_slots(t)

    # Meta / identity first — never playbook or capture tools.
    if is_meta_or_chitchat(t):
        return {
            "kind": "chitchat",
            "question_id": None,
            "intent": "chitchat",
            "slots": slots,
            "confidence": "high",
        }

    if match_question is not None:
        q = match_question(t, path=path)
        if q is not None:
            return {
                "kind": "playbook",
                "question_id": q["id"],
                "title": q.get("title"),
                "intent": None,
                "slots": slots,
                "confidence": "high",
            }

    intent = classify_intent(t, slots)
    if intent == "chitchat":
        return {
            "kind": "chitchat",
            "question_id": None,
            "intent": intent,
            "slots": slots,
            "confidence": "high",
        }

    scored = _score_intents(t)
    conf = "low"
    if intent == "why_slow":
        conf = "high"
    elif scored and scored[0][1] == intent:
        hits = scored[0][2]
        pri = (registry.intent_meta().get(intent) or {}).get("priority", 0)
        if hits >= 2 or pri >= 90:
            conf = "high"
        else:
            conf = "medium"
    elif intent != "general":
        conf = "medium"

    return {
        "kind": "plan",
        "question_id": None,
        "intent": intent,
        "slots": slots,
        "confidence": conf,
        "score_top": [
            {"intent": i, "score": s, "hits": h} for s, i, h in scored[:3]
        ],
    }
