"""Quick smoke test (run: python -m renderdoc_mcp.orchestrator._smoke_test)."""
from __future__ import print_function

from renderdoc_mcp.orchestrator.router import (
    classify_intent, extract_slots, is_graphics_related, route,
)
from renderdoc_mcp.orchestrator.planner import build_plan
from renderdoc_mcp.orchestrator import answer
from renderdoc_mcp.orchestrator import router as _r
from renderdoc_mcp.playbook import CallableBackend


def main():
    assert classify_intent("分析 GPU 耗时") == "timing"
    assert classify_intent("为什么这个 draw 这么慢") == "why_slow"
    assert classify_intent("黑屏了") == "black_screen"
    assert classify_intent("看一下 cbuffer") == "cbuffer"
    assert classify_intent("你的模型名称是什么") == "chitchat"
    assert classify_intent("你好，你是什么模型") == "chitchat"
    assert extract_slots("查看 EID 1234 的管线")["event_id"] == 1234

    r_hi = route("你好", path="panel")
    assert r_hi["kind"] == "model", r_hi
    assert route("你的模型名称是什么", path="panel")["kind"] == "model"

    if getattr(_r, "FORCE_MODEL_ALL", False):
        assert route("分析 GPU 耗时", path="panel")["kind"] == "model"
    else:
        assert is_graphics_related("你好") is False
        assert is_graphics_related("分析 GPU 耗时") is True

    r = route("分析 GPU 耗时", path="panel")
    print("route", r["kind"], r.get("question_id") or r.get("intent") or r.get("domain"))
    plan = build_plan("shader", {"stage": "Vertex"}, path="panel")
    assert plan["steps"][1]["args"]["stage"] == "Vertex"
    print("shader steps", [s["tool"] for s in plan["steps"]])

    def fake(tool, args):
        if tool == "get_status":
            return '{"loaded": true, "filename": "x.rdc", "currentEvent": 0}'
        if tool == "get_capture_info":
            return '{"api": "Vulkan"}'
        if tool == "get_current_frame":
            return '{"api": "Vulkan", "currentEvent": 10, "totalActions": 3}'
        if tool == "list_actions":
            return (
                '[{"eventId": 1, "name": "Draw",'
                ' "numIndices": 100, "numInstances": 1}]'
            )
        if tool == "fetch_counters":
            return (
                '[{"eventId": 1, "counter": "EventGPUDuration",'
                ' "value": 0.001}]'
            )
        return "{}"

    backend = CallableBackend(fake)
    out = answer("分析 GPU 耗时", backend, path="mcp")
    print("answer kind", out["kind"], "text_len", len(out.get("text") or ""))
    if getattr(_r, "FORCE_MODEL_ALL", False):
        assert out.get("kind") == "model"
    else:
        assert out.get("text")
    print("OK")


if __name__ == "__main__":
    main()
