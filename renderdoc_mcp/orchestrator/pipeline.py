"""Top-level answer(question) orchestration entry.

Python 3.6 compatible.
"""

from __future__ import print_function

import json
import re

from . import executor
from . import planner
from . import router

try:
    from renderdoc_mcp.playbook import describe_question as _pb_describe
    from renderdoc_mcp.playbook import format_result as _pb_format
    from renderdoc_mcp.playbook import run_question as _pb_run
except ImportError:
    try:
        from playbook import describe_question as _pb_describe  # type: ignore
        from playbook import format_result as _pb_format  # type: ignore
        from playbook import run_question as _pb_run  # type: ignore
    except ImportError:
        _pb_describe = None
        _pb_format = None
        _pb_run = None


def _session_gate(backend, path="panel"):
    """Return (ok, message, info_dict)."""
    info = {}
    # Prefer get_status on MCP; get_current_frame on panel.
    probes = []
    if path == "mcp":
        probes = ["get_status", "get_capture_info"]
    else:
        probes = ["get_current_frame"]

    last_err = None
    for tool in probes:
        if not backend.has_tool(tool):
            continue
        try:
            raw = backend.call(tool, {})
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            data = {"raw": raw}
        info[tool] = data
        if isinstance(data, dict):
            if data.get("error"):
                last_err = data.get("error")
                continue
            # MCP get_status shape: {loaded, filename, currentEvent, ...}
            if tool == "get_status":
                if "loaded" in data and not data.get("loaded"):
                    return False, "请先用 load_capture 打开 .rdc 抓帧。", info
            # Panel current frame
            if tool == "get_current_frame" and data.get("currentEvent") is None:
                if "error" in data:
                    return False, "当前没有可用抓帧：%s" % data.get("error"), info
        return True, "", info

    if last_err:
        # Soft-fail: some backends throw before load; still allow plan to try.
        if "not loaded" in last_err.lower() or "no capture" in last_err.lower() or "请先" in last_err:
            return False, "请先打开抓帧（.rdc）再提问。详情: %s" % last_err, info
    return True, "", info


def _chitchat_text(question, params=None):
    """Local canned reply for greetings / model identity — no tools, no LLM."""
    params = params or {}
    model = (params.get("model_name") or "").strip()
    q = question or ""
    about_model = bool(re.search(
        r"模型|model|你是谁|你叫什么|who are you", q, re.I))

    lines = []
    if about_model:
        lines.append("【本地说明】本回答不经过大模型。")
        lines.append("")
        lines.append(
            "抓帧分析由面板内本地 orchestrator / playbook 完成："
            "按问题匹配意图 → 调用 RenderDoc 接口 → 生成本地报告。"
        )
        if model:
            lines.append(
                "面板下拉当前选中的后端模型名：%s"
                "（仅在显式开启「模型解读」时才会用到；默认分析不用它）。" % model
            )
        else:
            lines.append(
                "面板可连接 Cursor sidecar 选择模型，但默认分析路径不调用该模型。"
            )
        lines.append("")
        lines.append(
            "试试：「分析 GPU 耗时」「当前管线状态」「PS 反汇编」「为什么这个 draw 慢」。"
        )
    else:
        lines.append("你好。我可以自动调用 RenderDoc 接口分析当前抓帧（本地工具，不经模型）。")
        lines.append("试试：「分析 GPU 耗时」「当前管线状态」「PS 反汇编」「为什么这个 draw 慢」。")
    return "\n".join(lines)


def format_report(result):
    """Render orchestrator result as panel/MCP text."""
    if not result:
        return "(空结果)"
    if result.get("kind") == "model":
        return result.get("text") or (
            "该问题与图形学/抓帧分析无关，请由对话模型回答。"
        )
    if result.get("kind") == "chitchat":
        return result.get("text") or "你好，我可以帮你分析当前 RenderDoc 抓帧。"
    if result.get("kind") == "gate_fail":
        return result.get("text") or "请先打开抓帧。"

    lines = []
    title = result.get("title")
    if title:
        lines.append("【自动分析】%s" % title)
    else:
        intent = result.get("intent") or "general"
        lines.append("【自动分析】意图: %s" % intent)

    steps_text = result.get("steps_text")
    if steps_text:
        lines += ["", steps_text]

    report = result.get("report") or ""
    if report:
        lines += ["", "【本地分析】", report]

    followups = result.get("followups") or []
    if followups:
        lines += ["", "可继续: %s" % ", ".join(followups)]

    errs = result.get("errors") or []
    if errs:
        lines += ["", "警告: %s" % "; ".join(str(e) for e in errs)]

    if result.get("explain_with_llm"):
        lines += [
            "",
            "【提示】如需模型结合证据解读，可把上方报告粘贴到对话或继续追问「解释原因」。",
        ]

    # Prefer preformatted text if provided (playbook path).
    if result.get("text") and result.get("kind") == "playbook":
        return result["text"]
    return "\n".join(lines)


def answer(question, backend, path="panel", params=None):
    """Main entry: question + FrameBackend -> result dict.

    Result keys:
      kind, text, title, intent, report, steps, steps_text, errors,
      explain_with_llm, followups, question_id, slots, evidence_raw_keys
    """
    text = (question or "").strip()
    params = dict(params or {})

    if not text:
        return {
            "kind": "gate_fail",
            "text": "请输入要分析的问题。",
            "errors": ["empty_question"],
        }

    # Route first: non-graphics → model; graphics → local MCP/playbook.
    decision = router.route(text, path=path)
    slots = dict(decision.get("slots") or {})
    if params.get("event_id") is not None:
        slots["event_id"] = int(params["event_id"])

    if decision.get("kind") in ("model", "chitchat"):
        # Non-graphics (greetings, identity, general chat) → panel/MCP client
        # should call the selected LLM. No local canned text.
        return {
            "kind": "model",
            "domain": "other",
            "text": "",
            "intent": decision.get("intent") or "general",
            "slots": slots,
            "errors": [],
            "explain_with_llm": True,
            "followups": [],
        }

    ok, gate_msg, _gate_info = _session_gate(backend, path=path)
    if not ok:
        return {
            "kind": "gate_fail",
            "text": gate_msg,
            "errors": ["no_capture"],
        }

    # --- Playbook short-circuit ---
    if decision.get("kind") == "playbook" and _pb_run is not None:
        qid = decision["question_id"]
        pb_params = dict(params)
        if slots.get("event_id") is not None:
            pb_params.setdefault("event_id", slots["event_id"])
        pb = _pb_run(qid, backend, params=pb_params)
        steps_lines = ["【自动调用】playbook:%s" % qid]
        info = _pb_describe(qid) if _pb_describe else None
        collect = (info or {}).get("collect") or []
        for i, step in enumerate(collect, 1):
            tool = step.get("tool")
            args = step.get("args") or {}
            try:
                arg_s = json.dumps(args, ensure_ascii=False) if args else ""
            except Exception:
                arg_s = str(args)
            steps_lines.append("%d. %s %s" % (i, tool, arg_s))
        steps_text = "\n".join(steps_lines)
        formatted = _pb_format(pb) if _pb_format else (pb.get("report") or "")
        body = "\n".join([
            "【自动分析】命中热门问题配方",
            "id: %s" % qid,
            "",
            steps_text,
            "",
            formatted,
        ])
        return {
            "kind": "playbook",
            "question_id": qid,
            "title": pb.get("title") or decision.get("title"),
            "intent": None,
            "report": pb.get("report"),
            "text": body,
            "steps": collect,
            "steps_text": steps_text,
            "errors": list(pb.get("errors") or []),
            "explain_with_llm": False,
            "followups": list(pb.get("followups") or []),
            "slots": slots,
        }

    # --- Rule plan ---
    intent = decision.get("intent") or "general"
    # Local-first: only enable Cloud LLM narrative when caller opts in.
    # Catalog plans keep explain_with_llm=False for speed.
    explain_hint = bool(params.get("explain_with_llm"))
    plan = planner.build_plan(intent, slots=slots, path=path, explain_hint=explain_hint)
    plan = planner.validate_plan(plan, path=path)
    # Merge caller params into plan params
    plan_params = dict(plan.get("params") or {})
    plan_params.update(params)
    plan["params"] = plan_params

    evidence, report, meta = executor.execute_plan(plan, backend, slots=slots)
    result = {
        "kind": "plan",
        "question_id": None,
        "title": None,
        "intent": meta.get("intent"),
        "report": report,
        "steps": evidence.steps,
        "steps_text": evidence.steps_report(),
        "errors": list(evidence.errors),
        "explain_with_llm": bool(meta.get("explain_with_llm")),
        "followups": [],
        "slots": meta.get("slots") or slots,
        "evidence_raw_keys": list(evidence.raw.keys()),
        "analyze": meta.get("analyze"),
    }
    result["text"] = format_report(result)
    return result
