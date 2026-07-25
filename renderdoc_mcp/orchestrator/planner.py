"""Rule-based tool planner (v1). Emits validated JSON plans only.

Python 3.6 compatible.
"""

from __future__ import print_function

import copy

from .registry import get_intent_plan, tools_for_path


def _apply_slots_to_args(args, slots, tool):
    out = dict(args or {})
    eid = slots.get("event_id")
    stage = slots.get("stage")
    if eid is not None and "event_id" not in out:
        if tool in (
            "get_pipeline_state", "get_action", "get_event_chunk",
            "get_shader_disassembly", "get_shader_reflection", "set_event",
            "get_constant_buffer",
        ):
            out["event_id"] = int(eid)
    if stage and tool in (
        "get_shader_disassembly", "get_shader_reflection", "get_constant_buffer",
    ):
        out["stage"] = stage
    name = slots.get("resource_name")
    if name and tool in ("list_textures", "list_resources") and "name_filter" not in out:
        out["name_filter"] = name
    return out


def build_plan(intent, slots=None, path="panel", explain_hint=False):
    """Build a tool plan for an intent + slots.

    Returns dict: intent, steps, analyze, explain_with_llm, params
    """
    slots = slots or {}
    template = get_intent_plan(intent)
    allowed = tools_for_path(path)
    steps = []
    for raw in template.get("steps") or []:
        step = copy.deepcopy(raw)
        tool = step.get("tool")
        if not tool:
            continue
        if tool not in allowed:
            # Panel: swap get_status -> get_current_frame; skip MCP-only.
            if tool == "get_status" and "get_current_frame" in allowed:
                step["tool"] = "get_current_frame"
                tool = "get_current_frame"
            elif tool == "get_capture_info" and path == "panel":
                # Panel may lack get_capture_info; prefer current frame.
                if "get_current_frame" in allowed:
                    step["tool"] = "get_current_frame"
                    tool = "get_current_frame"
                else:
                    continue
            else:
                if step.get("optional"):
                    continue
                # Keep optional skip; for required missing tools mark optional fail later
                step["optional"] = True
        step["args"] = _apply_slots_to_args(step.get("args"), slots, tool)
        steps.append(step)

    explain = bool(template.get("explain_with_llm"))
    if explain_hint:
        explain = True

    return {
        "intent": intent,
        "steps": steps,
        "analyze": template.get("analyze"),
        "explain_with_llm": explain,
        "params": dict(template.get("params") or {}),
    }


def validate_plan(plan, path="panel"):
    """Drop unknown tools; ensure at least one step when possible."""
    if not plan:
        return build_plan("general", path=path)
    allowed = tools_for_path(path)
    steps = []
    for step in plan.get("steps") or []:
        tool = step.get("tool")
        if tool in allowed:
            steps.append(step)
    plan = dict(plan)
    plan["steps"] = steps
    if not steps:
        return build_plan("frame", path=path)
    return plan
