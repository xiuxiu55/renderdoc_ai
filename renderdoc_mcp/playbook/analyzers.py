"""Local analyzers: JSON tool bags -> Chinese report text (Py3.6 compatible)."""

from __future__ import print_function

import json
import re


def _loads(raw, default=None):
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _cap(s, n=12000):
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "\n…（已截断）"


def _walk_actions(nodes, out_names, flat=None):
    if not isinstance(nodes, list):
        return
    for n in nodes:
        if not isinstance(n, dict):
            continue
        eid = n.get("eventId", n.get("event_id"))
        name = n.get("name") or n.get("action") or n.get("customName") or ""
        if eid is not None:
            out_names[int(eid)] = name
            if flat is not None:
                indices = n.get("numIndices")
                instances = n.get("numInstances")
                score = 0
                try:
                    score = int(indices or 0) * max(int(instances or 1), 1)
                except Exception:
                    score = 0
                flat.append((score, int(eid), name, indices, instances))
        for key in ("children", "actions", "childActions"):
            if key in n:
                _walk_actions(n[key], out_names, flat)


def _to_ms(v):
    # EventGPUDuration is seconds; large values may be nanoseconds.
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v > 1000.0:
        return v / 1e6
    return v * 1000.0


def analyze_timing_topn(bag, params):
    top_n = int(params.get("top_n") or 30)
    hot_pct = float(params.get("hot_pct") or 5.0)
    actions = _loads(bag.get("list_actions"), [])
    counters = _loads(bag.get("fetch_counters"), [])
    names = {}
    if isinstance(actions, list):
        _walk_actions(actions, names)
    elif isinstance(actions, dict) and "error" not in actions:
        _walk_actions(actions.get("children") or actions.get("actions") or [actions], names)

    ranked = []
    fetch_error = None
    if isinstance(counters, dict) and counters.get("error"):
        fetch_error = counters.get("error")
    if isinstance(counters, list):
        for row in counters:
            if not isinstance(row, dict):
                continue
            eid = int(row.get("eventId", -1))
            val = float(row.get("value", 0.0))
            ranked.append((val, eid, names.get(eid, "")))
    ranked.sort(reverse=True)

    lines = [
        "【GPU 耗时 Top-%d】" % top_n,
        "绘制事件数(含名称): %d；计数器样本数: %d" % (len(names), len(ranked)),
        "计数器: EventGPUDuration（已换算为 ms）",
        "",
        "Top %d 最耗时事件:" % top_n,
    ]
    top_rows = []
    if fetch_error:
        lines.append("(采样失败: %s)" % fetch_error)
    elif not ranked:
        lines.append("(未拿到耗时样本。该 API/驱动可能不支持 EventGPUDuration。)")
    else:
        total = sum(v for v, _e, _n in ranked) or 1.0
        for i, (val, eid, name) in enumerate(ranked[:top_n], 1):
            ms = _to_ms(val)
            pct = 100.0 * val / total
            lines.append("%2d. EID %-6d  %8.3f ms  (%5.1f%%)  %s" % (i, eid, ms, pct, name))
            top_rows.append((i, eid, ms, pct, name))

    lines += ["", _interpret_timing(top_rows, len(ranked), hot_pct)]
    return "\n".join(lines)


def _interpret_timing(top_rows, sample_count, hot_pct=5.0):
    if not top_rows:
        return (
            "【本地解读】未拿到可用的 GPU 耗时样本。"
            "可尝试换一台支持 GPU timer 的设备，或确认抓帧已完整 replay。"
        )
    lines = ["【本地解读】（不依赖模型）"]
    top1 = top_rows[0]
    lines.append(
        "1. 本帧采样 %d 个事件；最重的是 EID %d（%.3f ms，约占采样合计 %.1f%%）%s%s。"
        % (sample_count, top1[1], top1[2], top1[3],
           " — " if top1[4] else "", top1[4] or "")
    )
    heavy = [r for r in top_rows if r[3] >= hot_pct][:5]
    if len(heavy) >= 2:
        bits = ["EID %d(%.1f%%)" % (r[1], r[3]) for r in heavy]
        lines.append("2. 占比 ≥%.0f%% 的热点：%s。" % (hot_pct, "、".join(bits)))
    else:
        lines.append(
            "2. 耗时较分散；建议在 Event Browser 跳到 EID %d 查看所属 Pass / shader。"
            % top1[1]
        )
    lines.append(
        "3. 建议：选中上述 EID → Pipeline State / Texture Viewer / PS 反汇编，"
        "检查 overdraw、过大 RT、昂贵 blend/post。"
    )
    return "\n".join(lines)


def analyze_frame_overview(bag, params):
    data = _loads(bag.get("get_current_frame"), {})
    if not isinstance(data, dict) or data.get("error"):
        return "【当前帧】获取失败：%s" % (data.get("error") if isinstance(data, dict) else data)
    lines = [
        "【当前帧概览】",
        "API: %s" % data.get("api"),
        "Renderer: %s" % data.get("localRenderer"),
        "当前 EID: %s" % data.get("currentEvent"),
        "Action 总数: %s" % data.get("totalActions"),
    ]
    action = data.get("action") or {}
    if action:
        lines += [
            "",
            "当前 Action:",
            "  name: %s" % action.get("name"),
            "  flags: %s" % action.get("flags"),
        ]
        if "numIndices" in action:
            lines.append("  indices=%s instances=%s" % (
                action.get("numIndices"), action.get("numInstances")))
        if action.get("outputs"):
            lines.append("  outputs: %s" % ", ".join(action.get("outputs") or []))
    pipe = data.get("pipeline") or {}
    if pipe:
        lines += ["", "管线摘要:", _cap(json.dumps(pipe, ensure_ascii=False, indent=2), 4000)]
    lines += [
        "",
        "【本地解读】关注当前 EID 的输出 RT、绑定着色器与拓扑；"
        "若在排查性能，接着跑「GPU 最耗时的 Draw」。",
    ]
    return "\n".join(lines)


def analyze_drawcall_summary(bag, params):
    top_n = int(params.get("top_n") or 20)
    actions = _loads(bag.get("list_actions"), [])
    names = {}
    flat = []
    if isinstance(actions, list):
        _walk_actions(actions, names, flat)
    flat.sort(reverse=True)
    lines = [
        "【Drawcall 摘要】",
        "draw/dispatch 数量: %d" % len(flat),
        "按 numIndices*numInstances 估算 Top %d（非真实 GPU 耗时）：" % top_n,
    ]
    if not flat:
        lines.append("(未解析到 drawcall)")
    else:
        for i, (score, eid, name, indices, instances) in enumerate(flat[:top_n], 1):
            lines.append(
                "%2d. EID %-6d  idx=%s  inst=%s  score=%s  %s"
                % (i, eid, indices, instances, score, name)
            )
    lines += [
        "",
        "【本地解读】score 只反映几何量级。真实瓶颈请用「GPU 最耗时的 Draw」"
        "（EventGPUDuration）。",
    ]
    return "\n".join(lines)


def analyze_pipeline_check(bag, params):
    data = _loads(bag.get("get_pipeline_state"), {})
    if not isinstance(data, dict) or data.get("error"):
        return "【管线状态】获取失败：%s" % (data.get("error") if isinstance(data, dict) else data)
    lines = ["【管线状态】", _cap(json.dumps(data, ensure_ascii=False, indent=2), 8000)]
    hints = []
    text = json.dumps(data, ensure_ascii=False).lower()
    if "null" in text or "none" in text:
        hints.append("存在空绑定 / Null 资源的可能，检查 RT、深度与纹理槽。")
    if "blend" in text:
        hints.append("关注 blend 是否开启；全屏后处理 + blend 常成为带宽热点。")
    lines += ["", "【本地解读】"]
    if hints:
        for i, h in enumerate(hints, 1):
            lines.append("%d. %s" % (i, h))
    else:
        lines.append("1. 核对 VS/PS 是否齐全、RT 尺寸与视口是否匹配。")
    lines.append("2. 可继续跑「PS 是否偏重」或「纹理分辨率」问题。")
    return "\n".join(lines)


def analyze_ps_disasm(bag, params):
    raw = bag.get("get_shader_disassembly")
    data = _loads(raw, None)
    text = ""
    if isinstance(data, dict):
        if data.get("error"):
            return "【PS 反汇编】%s" % data.get("error")
        text = data.get("disassembly") or data.get("text") or json.dumps(data, ensure_ascii=False)
    else:
        text = raw if isinstance(raw, str) else ("" if data is None else str(data))
    text = text or ""
    lines = ["【PS 反汇编摘要】", "长度: %d 字符" % len(text)]
    # Cheap heuristics on disassembly text.
    low = text.lower()
    samples = 0
    for key in ("texture", "sample", "txl", "tex2d", "image"):
        samples += low.count(key)
    discard = low.count("discard") + low.count("kill")
    lines.append("纹理/采样相关词频(粗计): %d；discard/kill: %d" % (samples, discard))
    lines += ["", "反汇编（截断）:", _cap(text, 6000)]
    lines += [
        "",
        "【本地解读】采样与分支多通常更贵；结合 GPU Top Draw 的 EID 看是否全屏 PS。",
    ]
    return "\n".join(lines)


_SYNC_RE = re.compile(
    r"(wait|sync|fence|flush|finish|glfinish|glflush|clientwaitsync|"
    r"waitforsync|mapbuffer|unmap)",
    re.I,
)


def analyze_sync_stall(bag, params):
    top_n = int(params.get("top_n") or 30)
    actions = _loads(bag.get("list_actions"), [])
    names = {}
    _walk_actions(actions if isinstance(actions, list) else [], names)
    hits = []
    for eid, name in names.items():
        if _SYNC_RE.search(name or ""):
            hits.append((eid, name))
    hits.sort(key=lambda x: x[0])
    lines = [
        "【同步 / Wait 事件】",
        "匹配到 %d 个可疑事件（名称含 wait/sync/fence/flush/map…）" % len(hits),
    ]
    if not hits:
        lines.append("(未发现明显同步类 API 名；不代表没有隐式同步。)")
    else:
        for eid, name in hits[:top_n]:
            lines.append("  EID %-6d  %s" % (eid, name))
        if len(hits) > top_n:
            lines.append("  …另有 %d 条" % (len(hits) - top_n))
    lines += [
        "",
        "【本地解读】频繁 ClientWaitSync / Finish / 同步 Map 容易造成 CPU 空等；"
        "对照 Timeline / GPU 耗时看是否与卡顿对齐。",
    ]
    return "\n".join(lines)


def analyze_texture_overview(bag, params):
    top_n = int(params.get("top_n") or 25)
    data = _loads(bag.get("list_textures"), [])
    if isinstance(data, dict) and data.get("error"):
        return "【纹理列表】%s" % data.get("error")
    if not isinstance(data, list):
        return "【纹理列表】无法解析结果"
    scored = []
    for t in data:
        if not isinstance(t, dict):
            continue
        w = int(t.get("width") or t.get("Width") or 0)
        h = int(t.get("height") or t.get("Height") or 0)
        name = t.get("name") or t.get("resourceId") or ""
        fmt = t.get("format") or t.get("creationFlags") or ""
        scored.append((w * h, w, h, name, fmt))
    scored.sort(reverse=True)
    lines = [
        "【纹理概览】共 %d 个" % len(scored),
        "按宽*高排序 Top %d：" % top_n,
    ]
    for i, (area, w, h, name, fmt) in enumerate(scored[:top_n], 1):
        lines.append("%2d. %4dx%-4d  %s  %s" % (i, w, h, fmt, name))
    big = [s for s in scored if s[1] >= 2048 or s[2] >= 2048][:8]
    lines += ["", "【本地解读】"]
    if big:
        lines.append(
            "1. 发现 ≥2048 边长纹理 %d 个（展示 Top）：%s"
            % (len([s for s in scored if s[1] >= 2048 or s[2] >= 2048]),
               "、".join("%dx%d" % (b[1], b[2]) for b in big))
        )
        lines.append("2. 移动端上过大 RT/中间缓冲很伤带宽，确认是否可降分辨率或复用。")
    else:
        lines.append("1. 未见明显 ≥2048 大纹理；仍需结合当前 Pass 的 RT 绑定确认。")
    return "\n".join(lines)


def analyze_capture_info(bag, params):
    info = _loads(bag.get("get_capture_info"), None)
    frame = _loads(bag.get("get_current_frame"), None)
    lines = ["【抓帧信息】"]
    if isinstance(info, dict) and not info.get("error"):
        lines.append(_cap(json.dumps(info, ensure_ascii=False, indent=2), 4000))
    elif isinstance(frame, dict) and not frame.get("error"):
        lines.append("API: %s" % frame.get("api"))
        lines.append("Renderer: %s" % frame.get("localRenderer"))
        lines.append("当前 EID: %s" % frame.get("currentEvent"))
    else:
        err = None
        if isinstance(info, dict):
            err = info.get("error")
        if isinstance(frame, dict) and not err:
            err = frame.get("error")
        lines.append("获取失败：%s" % (err or "无数据"))
    lines += ["", "【本地解读】确认 API（GL/Vulkan/D3D）后再选对应的耗时/同步分析方法。"]
    return "\n".join(lines)


ANALYZERS = {
    "timing_topn": analyze_timing_topn,
    "frame_overview": analyze_frame_overview,
    "drawcall_summary": analyze_drawcall_summary,
    "pipeline_check": analyze_pipeline_check,
    "ps_disasm": analyze_ps_disasm,
    "sync_stall": analyze_sync_stall,
    "texture_overview": analyze_texture_overview,
    "capture_info": analyze_capture_info,
}
