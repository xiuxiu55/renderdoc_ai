"""Intent router: playbook match -> slot extract -> intent class.

Python 3.6 compatible.
"""

from __future__ import print_function

import re

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

_CHITCHAT = (
    "你好", "您好", "hello", "hi", "hey", "thanks", "thank you", "谢谢",
    "再见", "bye", "ok", "好的", "嗯",
)


def extract_slots(text):
    """Pull structured slots from free text."""
    t = text or ""
    slots = {}
    m = re.search(r"(?:EID|eid|event(?:Id)?|事件)\s*[#:=]?\s*(\d+)", t, re.I)
    if not m:
        m = re.search(r"\b(\d{2,7})\b", t)
        # Only treat bare numbers as EID when context mentions event/draw.
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


def classify_intent(text, slots=None):
    """Map free text to an intent class."""
    t = (text or "").strip()
    if not t:
        return "general"
    tl = t.lower()
    slots = slots or {}

    if any(c in tl or c in t for c in _CHITCHAT) and len(t) < 24:
        return "chitchat"

    if re.search(r"为什么|为何|怎么这么慢|为何慢|为什么慢|why\s*(is\s*)?(it\s*)?slow", t, re.I):
        return "why_slow"

    timing_kw = (
        "耗时", "性能", "瓶颈", "卡顿", "gpu", "timing", "duration", "fps",
        "慢", "hot", "top", "最耗时", "counter", "计数器",
    )
    if any(k in tl or k in t for k in timing_kw):
        if re.search(r"有哪些.*(计数器|counter)|list.*counter", t, re.I):
            return "counters"
        return "timing"

    if re.search(r"同步|barrier|fence|stall|等待|卡住", t, re.I):
        return "sync"

    if re.search(r"反汇编|disasm|shader|着色器|hlsl|spirv|ps\b|vs\b|cs\b", t, re.I):
        return "shader"

    if re.search(r"管线|pipeline|pso|viewport|视口|混合|blend|深度|depth", t, re.I):
        return "pipeline"

    if re.search(r"纹理|texture|rendertarget|\brt\b|分辨率", t, re.I):
        return "texture"

    if re.search(r"drawcall|绘制调用|draw\s*call|dispatch", t, re.I):
        return "drawcall"

    if slots.get("event_id") is not None or re.search(
            r"当前.*事件|这个事件|eid|event\s*browser|chunk|参数", t, re.I):
        return "event"

    if re.search(r"抓帧|capture|api|驱动|renderer|打开.*rdc|有没有.*帧", t, re.I):
        return "capture"

    if re.search(r"当前帧|这一帧|概览|在做什么", t, re.I):
        return "frame"

    return "general"


def route(text, path="panel"):
    """Return routing decision dict.

    Keys: kind (playbook|plan|chitchat), question_id?, intent?, slots, confidence
    """
    t = (text or "").strip()
    slots = extract_slots(t)

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

    conf = "medium"
    if intent in ("timing", "shader", "pipeline", "why_slow", "texture", "sync"):
        conf = "high"
    elif intent == "general":
        conf = "low"

    return {
        "kind": "plan",
        "question_id": None,
        "intent": intent,
        "slots": slots,
        "confidence": conf,
    }
