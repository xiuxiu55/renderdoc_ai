"""Execute tool plans against a FrameBackend; build EvidenceBag.

Python 3.6 compatible.
"""

from __future__ import print_function

import json

try:
    from renderdoc_mcp.playbook import analyzers
except ImportError:
    try:
        from playbook import analyzers  # type: ignore
    except ImportError:
        analyzers = None


def _loads(raw, default=None):
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def truncate_text(s, limit=8000):
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…（已截断，完整结果在 EvidenceBag）"


class EvidenceBag(object):
    def __init__(self):
        self.steps = []  # list of {tool, args, ok, error, summary, raw_key}
        self.raw = {}    # tool -> raw string (last write wins; unique keys if dup)
        self.errors = []

    def add(self, tool, args, raw, error=None):
        key = tool
        n = 1
        while key in self.raw:
            n += 1
            key = "%s#%d" % (tool, n)
        text = "" if raw is None else (raw if isinstance(raw, str) else str(raw))
        self.raw[key] = text
        summary = truncate_text(text, 1200)
        ok = error is None and not (
            isinstance(_loads(text, None), dict) and (_loads(text) or {}).get("error")
        )
        if error:
            self.errors.append("%s:%s" % (tool, error))
        entry = {
            "tool": tool,
            "args": args or {},
            "ok": ok,
            "error": error,
            "summary": summary,
            "raw_key": key,
        }
        self.steps.append(entry)
        return entry

    def bag_for_analyzer(self):
        """Map primary tool names -> raw JSON for playbook analyzers."""
        bag = {}
        for key, raw in self.raw.items():
            base = key.split("#")[0]
            if base not in bag:
                bag[base] = raw
        return bag

    def steps_report(self):
        lines = ["【自动调用】"]
        if not self.steps:
            lines.append("(无工具步骤)")
            return "\n".join(lines)
        for i, s in enumerate(self.steps, 1):
            status = "ok" if s.get("ok") else "FAIL"
            args = s.get("args") or {}
            arg_s = ""
            if args:
                try:
                    arg_s = " " + json.dumps(args, ensure_ascii=False)
                except Exception:
                    arg_s = " %s" % args
            lines.append("%d. %s%s → %s" % (i, s.get("tool"), arg_s, status))
            if s.get("error"):
                lines.append("   error: %s" % s["error"])
        return "\n".join(lines)


def _timing_top1_event_id(bag):
    counters = _loads(bag.get("fetch_counters"), [])
    if not isinstance(counters, list) or not counters:
        return None
    best_eid = None
    best_val = -1.0
    for row in counters:
        if not isinstance(row, dict):
            continue
        try:
            val = float(row.get("value", 0.0))
            eid = int(row.get("eventId", -1))
        except (TypeError, ValueError):
            continue
        if eid >= 0 and val > best_val:
            best_val = val
            best_eid = eid
    return best_eid


def execute_plan(plan, backend, slots=None):
    """Run plan steps; return (evidence, analyzer_report, meta)."""
    slots = dict(slots or {})
    evidence = EvidenceBag()
    params = dict(plan.get("params") or {})
    if slots.get("event_id") is not None:
        params["event_id"] = int(slots["event_id"])
    if slots.get("stage"):
        params["stage"] = slots["stage"]

    for step in plan.get("steps") or []:
        tool = step.get("tool")
        if not tool:
            continue
        args = dict(step.get("args") or {})
        optional = bool(step.get("optional"))

        if step.get("fill_event_from_timing_top1") and "event_id" not in args:
            top1 = _timing_top1_event_id(evidence.bag_for_analyzer())
            if top1 is not None:
                args["event_id"] = int(top1)
                slots["event_id"] = int(top1)
            elif slots.get("event_id") is not None:
                args["event_id"] = int(slots["event_id"])

        if not backend.has_tool(tool):
            msg = "backend 不支持工具 %s" % tool
            if optional:
                evidence.add(tool, args, json.dumps({"error": msg}, ensure_ascii=False), error=msg)
                continue
            evidence.add(tool, args, json.dumps({"error": msg}, ensure_ascii=False), error=msg)
            continue

        try:
            raw = backend.call(tool, args)
            evidence.add(tool, args, raw, error=None)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            payload = json.dumps({"error": err}, ensure_ascii=False)
            evidence.add(tool, args, payload, error=err)
            if not optional:
                # continue collecting optional follow-ups
                pass

    bag = evidence.bag_for_analyzer()
    analyze_name = plan.get("analyze")
    report = ""
    if analyzers is not None and analyze_name:
        fn = analyzers.ANALYZERS.get(analyze_name)
        if fn is not None:
            try:
                report = fn(bag, params)
            except Exception as exc:  # noqa: BLE001
                report = "分析器异常: %s" % exc
                evidence.errors.append("analyzer_error:%s" % exc)
        else:
            report = "未实现的分析器: %s" % analyze_name
            evidence.errors.append("missing_analyzer:%s" % analyze_name)
    else:
        report = "（无本地分析器；见工具摘要）\n" + evidence.steps_report()

    meta = {
        "intent": plan.get("intent"),
        "analyze": analyze_name,
        "explain_with_llm": bool(plan.get("explain_with_llm")),
        "slots": slots,
        "params": params,
    }
    return evidence, report, meta
