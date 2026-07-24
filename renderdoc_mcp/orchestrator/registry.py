"""Tool catalog and intent -> default tool plans (capabilities.md as code).

Python 3.6 compatible.
"""

from __future__ import print_function

# Tools both Panel (live_frame) and MCP commonly support.
COMMON_TOOLS = frozenset([
    "list_actions",
    "fetch_counters",
    "list_counters",
    "get_pipeline_state",
    "get_shader_disassembly",
    "get_shader_reflection",
    "list_textures",
    "list_resources",
    "get_action",
    "get_event_chunk",
    "get_capture_info",
    "get_current_frame",
    "get_status",
])

MCP_ONLY_TOOLS = frozenset([
    "load_capture",
    "close_capture",
    "set_event",
    "list_buffers",
    "save_texture",
    "get_constant_buffer",
    "get_disassembly_targets",
])

# Intent class -> default plan (v1 rule planner).
# Each step: tool, args, optional?, fill_from? (post-process hint)
INTENT_PLANS = {
    "capture": {
        "steps": [
            {"tool": "get_status", "args": {}, "optional": True},
            {"tool": "get_capture_info", "args": {}, "optional": True},
            {"tool": "get_current_frame", "args": {}, "optional": True},
        ],
        "analyze": "capture_info",
        "explain_with_llm": False,
    },
    "timing": {
        "steps": [
            {"tool": "list_actions", "args": {"drawcalls_only": True}},
            {"tool": "fetch_counters", "args": {"counters": ["EventGPUDuration"]}},
        ],
        "analyze": "timing_topn",
        "explain_with_llm": False,
        "params": {"top_n": 30, "hot_pct": 5.0},
    },
    "counters": {
        "steps": [
            {"tool": "list_counters", "args": {}},
            {
                "tool": "fetch_counters",
                "args": {"counters": ["EventGPUDuration"]},
                "optional": True,
            },
        ],
        "analyze": "timing_topn",
        "explain_with_llm": False,
    },
    "pipeline": {
        "steps": [
            {"tool": "get_pipeline_state", "args": {}},
        ],
        "analyze": "pipeline_check",
        "explain_with_llm": False,
    },
    "shader": {
        "steps": [
            {"tool": "get_pipeline_state", "args": {}, "optional": True},
            {"tool": "get_shader_disassembly", "args": {"stage": "Pixel"}},
            {"tool": "get_shader_reflection", "args": {"stage": "Pixel"}, "optional": True},
        ],
        "analyze": "shader_brief",
        "explain_with_llm": False,
    },
    "texture": {
        "steps": [
            {"tool": "list_textures", "args": {}},
            {"tool": "get_pipeline_state", "args": {}, "optional": True},
        ],
        "analyze": "texture_overview",
        "explain_with_llm": False,
        "params": {"top_n": 20},
    },
    "event": {
        "steps": [
            {"tool": "get_action", "args": {}},
            {"tool": "get_event_chunk", "args": {}, "optional": True},
            {"tool": "get_pipeline_state", "args": {}, "optional": True},
        ],
        "analyze": "event_brief",
        "explain_with_llm": False,
    },
    "drawcall": {
        "steps": [
            {"tool": "list_actions", "args": {"drawcalls_only": True}},
        ],
        "analyze": "drawcall_summary",
        "explain_with_llm": False,
        "params": {"top_n": 20},
    },
    "sync": {
        "steps": [
            {"tool": "list_actions", "args": {"drawcalls_only": False}},
        ],
        "analyze": "sync_stall",
        "explain_with_llm": False,
    },
    "why_slow": {
        # timing first, then inspect hottest event pipeline + PS
        "steps": [
            {"tool": "list_actions", "args": {"drawcalls_only": True}},
            {"tool": "fetch_counters", "args": {"counters": ["EventGPUDuration"]}},
            {
                "tool": "get_pipeline_state",
                "args": {},
                "optional": True,
                "fill_event_from_timing_top1": True,
            },
            {
                "tool": "get_shader_disassembly",
                "args": {"stage": "Pixel"},
                "optional": True,
                "fill_event_from_timing_top1": True,
            },
        ],
        "analyze": "why_slow",
        "explain_with_llm": True,
        "params": {"top_n": 15, "hot_pct": 5.0},
    },
    "frame": {
        "steps": [
            {"tool": "get_current_frame", "args": {}},
        ],
        "analyze": "frame_overview",
        "explain_with_llm": False,
    },
    "general": {
        "steps": [
            {"tool": "get_current_frame", "args": {}, "optional": True},
            {"tool": "list_actions", "args": {"drawcalls_only": True}, "optional": True},
        ],
        "analyze": "frame_overview",
        "explain_with_llm": True,
    },
}


def tools_for_path(path):
    """Return frozenset of tools allowed on this path."""
    if path == "mcp":
        return COMMON_TOOLS | MCP_ONLY_TOOLS
    return COMMON_TOOLS


def get_intent_plan(intent):
    return INTENT_PLANS.get(intent) or INTENT_PLANS["general"]
