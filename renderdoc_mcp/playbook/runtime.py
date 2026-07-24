"""Execute playbook questions against a FrameBackend (Py3.6 compatible)."""

from __future__ import print_function

import json

from . import analyzers
from . import registry


class FrameBackend(object):
    """Minimal backend: call(tool, args) -> JSON/text string."""

    def call(self, tool, args):
        raise NotImplementedError

    def has_tool(self, tool):
        return True


class CallableBackend(FrameBackend):
    def __init__(self, caller, tools=None):
        self._caller = caller
        self._tools = set(tools) if tools else None

    def call(self, tool, args):
        return self._caller(tool, args or {})

    def has_tool(self, tool):
        if self._tools is None:
            return True
        return tool in self._tools


def load_registry(force=False):
    return registry.load_registry(force=force)


def list_questions(path=None, tag=None):
    return registry.list_questions(path=path, tag=tag)


def match_question(text, path=None):
    return registry.match_question(text, path=path)


def describe_question(question_id):
    return registry.describe_question(question_id)


def run_question(question_id, backend, params=None):
    """Collect tool data then run the local analyzer.

    Returns dict: question_id, title, report, followups, errors, analyze.
    """
    q = registry.get_question(question_id)
    if q is None:
        return {
            "question_id": question_id,
            "title": None,
            "report": "未知问题 id: %s。可用 list_hot_questions 查看。" % question_id,
            "followups": [],
            "errors": ["unknown_question"],
            "analyze": None,
        }

    merged = {}
    merged.update(q.get("params") or {})
    if params:
        merged.update(params)

    bag = {}
    errors = []
    for step in q.get("collect") or []:
        tool = step.get("tool")
        if not tool:
            continue
        optional = bool(step.get("optional"))
        args = dict(step.get("args") or {})
        # Allow run-time overrides like event_id.
        if merged.get("event_id") is not None and "event_id" not in args:
            if tool in ("get_pipeline_state", "get_action", "get_shader_disassembly",
                        "get_shader_reflection", "get_event_chunk"):
                args["event_id"] = int(merged["event_id"])
        if not backend.has_tool(tool):
            if optional:
                continue
            errors.append("missing_tool:%s" % tool)
            bag[tool] = json.dumps({"error": "backend 不支持工具 %s" % tool}, ensure_ascii=False)
            continue
        try:
            bag[tool] = backend.call(tool, args)
        except Exception as exc:  # noqa: BLE001
            if optional:
                bag[tool] = json.dumps({"error": str(exc)}, ensure_ascii=False)
                continue
            errors.append("%s:%s" % (tool, exc))
            bag[tool] = json.dumps({"error": str(exc)}, ensure_ascii=False)

    analyze_name = q.get("analyze")
    fn = analyzers.ANALYZERS.get(analyze_name)
    if fn is None:
        report = "未实现的分析器: %s\n原始数据键: %s" % (
            analyze_name, ", ".join(sorted(bag.keys())))
        errors.append("missing_analyzer:%s" % analyze_name)
    else:
        try:
            report = fn(bag, merged)
        except Exception as exc:  # noqa: BLE001
            report = "分析器异常: %s" % exc
            errors.append("analyzer_error:%s" % exc)

    return {
        "question_id": q["id"],
        "title": q.get("title"),
        "report": report,
        "followups": list(q.get("followups") or []),
        "errors": errors,
        "analyze": analyze_name,
    }


def format_result(result):
    """Render a run_question result as panel/MCP-friendly text."""
    if not result:
        return "(空结果)"
    lines = [
        "【热门问题】%s" % (result.get("title") or result.get("question_id")),
        "id: %s" % result.get("question_id"),
        "",
        result.get("report") or "(无报告)",
    ]
    followups = result.get("followups") or []
    if followups:
        lines += ["", "可继续: %s" % ", ".join(followups)]
    errs = result.get("errors") or []
    if errs:
        lines += ["", "警告: %s" % "; ".join(str(e) for e in errs)]
    return "\n".join(lines)
