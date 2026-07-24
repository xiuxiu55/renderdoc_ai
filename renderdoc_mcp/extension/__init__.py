"""RenderDoc UI extension: an in-app AI assistant panel that talks *directly* to
CodeBuddy and analyzes the frame you're currently looking at.

CodeBuddy is run separately as an HTTP server (``codebuddy --serve --port 8080``).
This panel connects straight to it from inside RenderDoc. Because RenderDoc's
bundled Python 3.6 has no ``_socket`` module, the HTTP calls go through a tiny
ctypes/ws2_32 client (``http_ctypes.py``) instead of the standard library.

Conversations use CodeBuddy's ACP protocol (``acp_client.py``), which supports
model selection (``session/set_model``) and streaming replies. Every message is
augmented with live context about the current frame/event, and the quick-action
buttons fetch richer data (drawcalls, pipeline state, shader disassembly) so
CodeBuddy analyzes exactly what you're viewing.

The panel applies RenderDoc's dark UI style (``RDDark``) for a dark appearance.
"""

import json
import os
import sys
import threading

import qrenderdoc as qrd

_EXT_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_EXT_DIR)
for _p in (_EXT_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import acp_client
import codebuddy_client
import http_ctypes
from live_frame import LiveFrame

try:
    from playbook import (  # type: ignore
        CallableBackend,
        format_result as _pb_format,
        list_questions as _pb_list,
        match_question as _pb_match,
        run_question as _pb_run,
    )
    _HAS_PLAYBOOK = True
except Exception:  # noqa: BLE001
    _HAS_PLAYBOOK = False
    CallableBackend = None  # type: ignore
    _pb_format = _pb_list = _pb_match = _pb_run = None  # type: ignore

extiface_version = ""

live = None          # type: LiveFrame
cur_window = None    # type: Window

DEFAULT_PORT = 8080

CMD_CODEBUDDY = "codebuddy --serve --port 8080"
CMD_CURSOR_SIDECAR = "python -m renderdoc_mcp.cursor_sidecar --port 8080"


def _copy_to_clipboard(text):
    """Copy Unicode text to the Windows clipboard (RenderDoc Python has no Qt clipboard helper)."""
    text = text or ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not user32.EmptyClipboard():
                raise ctypes.WinError(ctypes.get_last_error())
            hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not hmem:
                raise ctypes.WinError(ctypes.get_last_error())
            ptr = kernel32.GlobalLock(hmem)
            if not ptr:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(hmem)
            if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
                raise ctypes.WinError(ctypes.get_last_error())
            # Ownership of hmem transferred to the clipboard; do not free.
        finally:
            user32.CloseClipboard()
        return True
    except Exception:  # noqa: BLE001
        # Fallback: clip.exe (may mangle non-ASCII on some code pages).
        try:
            import subprocess
            p = subprocess.Popen(
                ["clip"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.communicate(text.encode("mbcs", "replace"))
            return p.returncode == 0
        except Exception:  # noqa: BLE001
            return False

# --- RenderDoc tool-calling loop -------------------------------------------
#
# CodeBuddy only receives a text prompt; it has no native way to call back into
# RenderDoc. To let it *actively* pull data from the live frame (drawcall list,
# per-event GPU timing, pipeline, shaders, …) we run a small agent loop here:
# the model asks for data by emitting a `@@RDTOOL@@ {json}` directive, we run the
# matching LiveFrame method on the replay thread and feed the JSON result back
# into the same ACP session, repeating until it produces a final answer.

TOOL_MARKER = "@@RDTOOL@@"
MAX_TOOL_STEPS = 8

# name -> ({argName: "描述"}, "工具说明"). The name must match a LiveFrame method.
RD_TOOL_SPECS = [
    ("get_current_frame", {},
     "当前帧概览：API、当前事件 EID、action 总数、当前选中事件的管线摘要"),
    ("list_actions",
     {"drawcalls_only": "bool 可选，true 只列 draw/dispatch", "max_depth": "int 可选，0=全部层级"},
     "列出事件/绘制树（Event Browser 的数据来源）"),
    ("get_action", {"event_id": "int 必填"},
     "单个 action 的绘制参数、输出目标与 API 事件列表"),
    ("get_pipeline_state", {"event_id": "int 可选，默认当前事件"},
     "指定事件的管线状态（拓扑/各阶段着色器/渲染目标/视口）"),
    ("get_shader_disassembly",
     {"stage": "str 必填: Vertex/Hull/Domain/Geometry/Pixel/Compute", "event_id": "int 可选", "target": "str 可选"},
     "反汇编某阶段着色器"),
    ("get_shader_reflection", {"stage": "str 必填", "event_id": "int 可选"},
     "着色器输入/输出/常量块/资源反射"),
    ("list_textures", {"name_filter": "str 可选，按名字过滤"},
     "纹理列表（尺寸/格式/mip/array）"),
    ("list_resources", {"name_filter": "str 可选"}, "资源列表"),
    ("list_counters", {}, "列出当前抓帧可用的 GPU 性能计数器（耗时一般为 EventGPUDuration）"),
    ("pick_duration_counter", {}, "自动挑选本抓帧可用的耗时计数器"),
    ("fetch_counters",
     {"counters": "[str] 必填，如 [\"EventGPUDuration\"]（可用 GPUDuration 别名）",
      "event_ids": "[int] 可选，只取这些事件"},
     "采样 GPU 计数器，得到每个事件的真实耗时——分析资源/绘制耗时就用它"),
    ("get_event_chunk", {"event_id": "int 必填"}, "某事件记录的 API 调用及其参数"),
]
RD_TOOL_NAMES = frozenset(name for name, _args, _desc in RD_TOOL_SPECS)


def _agent_system_prompt():
    lines = [
        "你是嵌入在 RenderDoc 内的图形调试分析助手。需要帧数据时，按下面协议向本扩展请求，"
        "扩展会从当前已打开的抓帧读取并回传 JSON。",
        "",
        "可用工具：",
    ]
    for name, args, desc in RD_TOOL_SPECS:
        argdesc = "，".join("%s=%s" % (k, v) for k, v in args.items()) if args else "无参数"
        lines.append("- %s(%s)：%s" % (name, argdesc, desc))
    lines += [
        "",
        "意图路由：",
        "- 耗时/性能/瓶颈 → list_actions(drawcalls_only=true) 然后 "
        "fetch_counters(counters=[\"EventGPUDuration\"])（不要用 list_resources）",
        "- 事件树/drawcall → list_actions",
        "- 某个 EID → get_action；API 参数 → get_event_chunk",
        "- 管线/RT → get_pipeline_state；shader → get_shader_disassembly / get_shader_reflection",
        "- 纹理/资源元数据 → list_textures / list_resources",
        "",
        "需要数据时只输出一行：%s {\"tool\": \"名\", \"args\": {...}}" % TOOL_MARKER,
        "已有足够信息时直接用中文给出分析（不要再包含 %s）。" % TOOL_MARKER,
    ]
    return "\n".join(lines)


def _analysis_prompt(topic, question, data_for_model):
    """Short tool-free prompt. Avoid role-play / jailbreak-style framing —
    some models treat that as ``stopReason=refusal``."""
    q = (question or topic or "请分析以下 RenderDoc 采样数据").strip()
    return (
        "下面是 RenderDoc 扩展从当前已打开抓帧读取的性能/帧数据摘要。\n"
        "请直接根据这些数据用中文做简洁技术分析（要点列表），"
        "指出最耗时或最可疑的事件，并给出可执行的优化建议。\n"
        "不需要访问任何外部工具；数据已完整给出。\n\n"
        "问题：%s\n\n"
        "数据摘要：\n%s" % (q, data_for_model)
    )


def _summarize_drawcalls_local(actions_raw, top_n=20):
    """Build a compact local drawcall summary when the model refuses."""
    try:
        actions = json.loads(actions_raw)
    except ValueError:
        return "drawcall 原始数据：\n" + _cap_result(actions_raw, 8000)

    flat = []

    def walk(nodes):
        if not isinstance(nodes, list):
            return
        for n in nodes:
            if not isinstance(n, dict):
                continue
            eid = n.get("eventId", n.get("event_id"))
            name = n.get("name") or ""
            indices = n.get("numIndices")
            instances = n.get("numInstances")
            if eid is not None:
                score = 0
                try:
                    score = int(indices or 0) * max(int(instances or 1), 1)
                except Exception:  # noqa: BLE001
                    score = 0
                flat.append((score, int(eid), name, indices, instances))
            walk(n.get("children") or [])

    if isinstance(actions, list):
        walk(actions)

    flat.sort(reverse=True)
    lines = [
        "【本地 Drawcall 摘要】",
        "draw/dispatch 数量: %d" % len(flat),
        "按 numIndices*numInstances 估算较重的 Top %d（非真实 GPU 耗时；真实耗时请用「GPU 耗时」）：" % top_n,
    ]
    if not flat:
        lines.append("(未解析到 drawcall)")
    else:
        for i, (score, eid, name, indices, instances) in enumerate(flat[:top_n], 1):
            lines.append(
                "%2d. EID %-6d  idx=%s  inst=%s  score=%s  %s" % (
                    i, eid, indices, instances, score, name))
    return "\n".join(lines)


def _model_payload_from_local(local_data, limit=3500):
    """Build a short, model-safe payload from the local display block.

    Prefer the ranked summary / 本地解读 sections; strip raw JSON dumps that
    inflate the prompt and raise refusal rates.
    """
    text = local_data or ""
    cut_markers = (
        "\n完整 drawcall 列表 JSON",
        "\n完整计数器 JSON",
        "\n原始 JSON",
        "\n```json",
    )
    cut_at = len(text)
    for m in cut_markers:
        i = text.find(m)
        if i >= 0:
            cut_at = min(cut_at, i)
    text = text[:cut_at].strip()
    return _cap_result(text, limit)


def _extract_json_object(text, start):
    """Return (json_str, end_index) for the balanced {...} beginning at or after
    ``start``, or (None, -1)."""
    brace = text.find("{", start)
    if brace < 0:
        return None, -1
    depth = 0
    in_str = False
    esc = False
    for i in range(brace, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace:i + 1], i + 1
    return None, -1


def _parse_tool_call(text):
    """Look for a ``@@RDTOOL@@ {json}`` directive. Returns ((name, args), before)
    where ``before`` is any narration the model wrote ahead of the directive, or
    (None, text) if there is no tool call."""
    if not text:
        return None, text
    idx = text.find(TOOL_MARKER)
    if idx < 0:
        return None, text
    blob, _end = _extract_json_object(text, idx + len(TOOL_MARKER))
    if blob is None:
        return None, text
    try:
        obj = json.loads(blob)
    except ValueError:
        return None, text
    name = obj.get("tool") or obj.get("name")
    if not name:
        return None, text
    args = obj.get("args") or obj.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    return (name, args), text[:idx]


def _run_rd_tool(name, args):
    """Execute one LiveFrame tool by name and return its JSON string result
    (errors are returned as a JSON ``{"error": ...}`` so the model can recover)."""
    if live is None or not live.loaded():
        return json.dumps({"error": "RenderDoc 尚未加载任何抓帧。"}, ensure_ascii=False)
    if name not in RD_TOOL_NAMES:
        return json.dumps(
            {"error": "未知工具 '%s'。可用: %s" % (name, ", ".join(sorted(RD_TOOL_NAMES)))},
            ensure_ascii=False)
    try:
        fn = getattr(live, name)
        result = fn(args or {})
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (name, exc)}, ensure_ascii=False)


def _short(s, n):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _cap_result(s, n=20000):
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "\n…（结果过长已截断；可用更精确的参数缩小范围后再取）"


def _tool_result_prompt(name, result):
    return ("工具 `%s` 的结果（JSON）：\n```json\n%s\n```\n"
            "请基于该结果继续：若仍需更多数据，就再用 %s 调用一个工具；"
            "否则用中文给出最终分析结论。" % (name, _cap_result(result), TOOL_MARKER))


# Keywords that mean the user wants Event Browser / GPU timing analysis.
_TIMING_KEYWORDS = (
    "耗时", "耗時", "性能", "瓶颈", "瓶頸", "卡顿", "卡頓", "慢",
    "timing", "duration", "gpuduration", "gpu duration", "perf",
    "event browser", "eventbrowser", "计数器", "計數器", "counter",
)


def _is_timing_query(text):
    t = (text or "").lower()
    if not t:
        return False
    return any(k in t for k in _TIMING_KEYWORDS)


def _looks_like_refusal(text):
    """True only for hard refusals — not for normal disclaimers.

    Models often say "我无法直接访问 RenderDoc，但根据你提供的数据…" and then
    give a useful analysis. Matching phrases like "无法直接" / "cannot access"
    used to discard those answers and show a false refusal error.
    """
    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower()

    # Our own ACP helper notes.
    if "拒绝了该请求" in t or "拒絕了該請求" in t:
        return True
    if "codebuddy 拒绝" in tl:
        return True

    # Hard policy refusals (English / Chinese). Require the refusal to be the
    # main point — if the reply also analyzes provided numbers, keep it.
    hard = (
        "i can't help with that",
        "i cannot help with that",
        "i'm not able to help with that",
        "i am not able to help with that",
        "cannot assist with that",
        "won't be able to assist",
        "against my guidelines",
        "violates",
        "抱歉，我无法协助",
        "抱歉，我不能协助",
        "我不能协助该请求",
        "我无法协助该请求",
        "无法提供这方面的帮助",
    )
    if any(m in tl for m in hard):
        # If it still produced a concrete ranking / ms analysis, treat as OK.
        if any(k in t for k in ("EID", "ms", "耗时", "瓶颈", "draw", "优化")):
            return False
        return True

    # stopReason text leaked into rendered transcript with no real answer.
    if "stopreason=refusal" in tl.replace(" ", ""):
        return True
    if "stopreason" in tl and "refusal" in tl and len(t) < 400:
        return True
    return False


def _is_acp_refusal(acp):
    try:
        meta = getattr(acp, "last_meta", None) or {}
        return meta.get("stopReason") == "refusal"
    except Exception:  # noqa: BLE001
        return False


def _run_playbook_local(question_id, params=None):
    """Collect + analyze via shared playbook (no CodeBuddy)."""
    if not _HAS_PLAYBOOK:
        return "Playbook 未安装。请重新运行 extension/install.py。"
    if live is None or not live.loaded():
        return "RenderDoc 尚未加载任何抓帧。"
    backend = CallableBackend(lambda tool, args: _run_rd_tool(tool, args))
    result = _pb_run(question_id, backend, params=params)
    return _pb_format(result)


def _gather_timing_bundle(top_n=30):
    """Prefetch GPU timing via playbook (local, model-free)."""
    if _HAS_PLAYBOOK:
        return _run_playbook_local("gpu_top_draws", params={"top_n": top_n})
    return "Playbook 未安装，无法采样 GPU 耗时。请运行 extension/install.py。"


class Window(qrd.CaptureViewer):
    def __init__(self, ctx, version):
        super(Window, self).__init__()
        self.ctx = ctx
        self.version = version
        self.mqt = ctx.Extensions().GetMiniQtHelper()

        self.port = DEFAULT_PORT
        self.busy = False
        self._cancel_token = None
        self.history_text = ""
        self._reply_base = ""
        self.acp = None
        self.model_by_name = {}
        self.mode_by_name = {}
        self.perm_mode = acp_client.DEFAULT_MODE

        _apply_dark_theme(ctx)

        self.topWindow = self.mqt.CreateToplevelWidget(
            "AI 助手 (CodeBuddy)", lambda c, w, d: _window_closed())

        root = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.topWindow, root)

        welcome = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(
            welcome,
            "欢迎使用 AI 助手。聊天后端二选一（点下方按钮复制命令到剪贴板），"
            "在终端运行后再点“重新连接”。"
            "Cursor sidecar 还需设置 CURSOR_API_KEY。"
            "热门问题/快捷按钮为本地分析，不依赖后端。")
        self.mqt.AddWidget(root, welcome)

        cmd_row = self.mqt.CreateHorizontalContainer()
        self.mqt.AddWidget(root, cmd_row)
        self._add_copy_cmd(
            cmd_row, "复制 CodeBuddy 命令", CMD_CODEBUDDY)
        self._add_copy_cmd(
            cmd_row, "复制 Cursor sidecar 命令", CMD_CURSOR_SIDECAR)
        self.copyHint = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(self.copyHint, "")
        self.mqt.AddWidget(cmd_row, self.copyHint)

        # connection + model row
        conn = self.mqt.CreateHorizontalContainer()
        self.mqt.AddWidget(root, conn)
        self.statusLabel = self.mqt.CreateLabel()
        self.mqt.AddWidget(conn, self.statusLabel)
        portLabel = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(portLabel, "   端口:")
        self.mqt.AddWidget(conn, portLabel)
        self.portBox = self.mqt.CreateTextBox(True, lambda c, w, d: None)
        self.mqt.SetWidgetText(self.portBox, str(self.port))
        self.mqt.AddWidget(conn, self.portBox)
        self.reconnectBtn = self.mqt.CreateButton(lambda c, w, d: self._reconnect())
        self.mqt.SetWidgetText(self.reconnectBtn, "重新连接")
        self.mqt.AddWidget(conn, self.reconnectBtn)
        permLabel = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(permLabel, "   权限:")
        self.mqt.AddWidget(conn, permLabel)
        self.modeCombo = self.mqt.CreateComboBox(False, lambda c, w, d: self._on_mode_change())
        self.mqt.AddWidget(conn, self.modeCombo)

        # conversation history
        self.history = self.mqt.CreateTextBox(False, lambda c, w, d: None)
        try:
            self.mqt.SetWidgetFont(self.history, "Consolas", 9, False, False)
        except Exception:  # noqa: BLE001
            pass
        self.mqt.AddWidget(root, self.history)

        self.contextLabel = self.mqt.CreateLabel()
        self.mqt.AddWidget(root, self.contextLabel)

        # hot-question playbook row (local analysis, no CodeBuddy required)
        play = self.mqt.CreateHorizontalContainer()
        self.mqt.AddWidget(root, play)
        playLabel = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(playLabel, "热门问题:")
        self.mqt.AddWidget(play, playLabel)
        self.questionCombo = self.mqt.CreateComboBox(
            False, lambda c, w, d: None)
        self.mqt.AddWidget(play, self.questionCombo)
        self.runQuestionBtn = self.mqt.CreateButton(
            lambda c, w, d: self._qa_run_selected_question())
        self.mqt.SetWidgetText(self.runQuestionBtn, "分析")
        self.mqt.AddWidget(play, self.runQuestionBtn)
        self._question_titles = []
        self._question_ids = []
        self._populate_questions()

        # quick actions (all local playbook paths)
        quick = self.mqt.CreateHorizontalContainer()
        self.mqt.AddWidget(root, quick)
        self._add_quick(quick, "分析当前帧", self._qa_analyze_frame)
        self._add_quick(quick, "Drawcalls", self._qa_drawcalls)
        self._add_quick(quick, "GPU 耗时", self._qa_gpu_timing)
        self._add_quick(quick, "管线状态", self._qa_pipeline)
        self._add_quick(quick, "PS 反汇编", self._qa_ps_disasm)

        # input box (single line -> small height). The callback fires on every
        # edit and again (with unchanged text) on Enter, which we use to submit.
        self._last_input = None
        self.input = self.mqt.CreateTextBox(True, lambda c, w, d: self._on_input_changed(d))
        self.mqt.AddWidget(root, self.input)

        # send button + model selector, placed below the input box
        actions = self.mqt.CreateHorizontalContainer()
        self.mqt.AddWidget(root, actions)
        self.sendBtn = self.mqt.CreateButton(lambda c, w, d: self._on_send_click())
        self.mqt.SetWidgetText(self.sendBtn, "发送")
        self.mqt.AddWidget(actions, self.sendBtn)
        self.cancelBtn = self.mqt.CreateButton(lambda c, w, d: self._on_cancel())
        self.mqt.SetWidgetText(self.cancelBtn, "取消")
        self.mqt.SetWidgetEnabled(self.cancelBtn, False)
        self.mqt.AddWidget(actions, self.cancelBtn)
        modelLabel = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(modelLabel, "   模型:")
        self.mqt.AddWidget(actions, modelLabel)
        self.modelCombo = self.mqt.CreateComboBox(False, lambda c, w, d: self._on_model_change())
        self.mqt.AddWidget(actions, self.modelCombo)

        ctx.AddCaptureViewer(self)
        self._set_status("未连接")
        self._refresh_context(ctx.CurEvent() if ctx.IsCaptureLoaded() else 0)
        self._reconnect()

    # -- small helpers ----------------------------------------------------

    def _add_quick(self, parent, label, fn):
        btn = self.mqt.CreateButton(lambda c, w, d: fn())
        self.mqt.SetWidgetText(btn, label)
        self.mqt.AddWidget(parent, btn)

    def _add_copy_cmd(self, parent, label, command):
        def on_click(c=None, w=None, d=None, cmd=command, name=label):
            ok = _copy_to_clipboard(cmd)
            if ok:
                tip = "已复制: %s" % cmd
            else:
                tip = "复制失败，请手动复制: %s" % cmd
            try:
                self.mqt.SetWidgetText(self.copyHint, "  " + tip)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._set_status(tip)
            except Exception:  # noqa: BLE001
                pass
        btn = self.mqt.CreateButton(on_click)
        self.mqt.SetWidgetText(btn, label)
        self.mqt.AddWidget(parent, btn)

    def _populate_questions(self):
        self._question_titles = []
        self._question_ids = []
        if not _HAS_PLAYBOOK:
            self._question_titles = ["(playbook 未安装)"]
            self._question_ids = [""]
        else:
            for q in _pb_list(path="panel"):
                self._question_ids.append(q["id"])
                self._question_titles.append(q.get("title") or q["id"])
            if not self._question_titles:
                self._question_titles = ["(无问题)"]
                self._question_ids = [""]
        try:
            self.mqt.SetComboOptions(self.questionCombo, self._question_titles)
            if self._question_titles:
                self.mqt.SelectComboOption(self.questionCombo, self._question_titles[0])
        except Exception:  # noqa: BLE001
            pass

    def _show_local_report(self, label, report):
        self.history_text += "🧑 你：\n%s\n\n🤖 本地分析：\n%s\n\n" % (label, report)
        self.mqt.SetWidgetText(self.history, self.history_text)

    def _qa_playbook(self, question_id, label=None):
        if self.busy:
            return
        if not question_id:
            return
        self.busy = True
        self.mqt.SetWidgetEnabled(self.sendBtn, False)
        shown = label or question_id
        self.history_text += "🧑 你：\n%s\n\n" % shown
        self.mqt.SetWidgetText(
            self.history, self.history_text + "🤖 本地分析：\n（采样中…）")

        def worker():
            try:
                report = _run_playbook_local(question_id)
            except Exception as exc:  # noqa: BLE001
                report = "分析失败: %s" % exc

            def finish():
                self.history_text += "🤖 本地分析：\n%s\n\n" % report
                self.mqt.SetWidgetText(self.history, self.history_text)
                self.mqt.SetWidgetEnabled(self.sendBtn, True)
                self.busy = False
            self._ui(finish)

        _spawn(worker)

    def _qa_run_selected_question(self):
        try:
            title = self.mqt.GetWidgetText(self.questionCombo)
        except Exception:  # noqa: BLE001
            title = ""
        qid = ""
        if title in self._question_titles:
            qid = self._question_ids[self._question_titles.index(title)]
        elif self._question_ids:
            qid = self._question_ids[0]
            title = self._question_titles[0]
        self._qa_playbook(qid, label=title)

    def _set_status(self, text):
        self.mqt.SetWidgetText(self.statusLabel, "状态: " + text)

    def _ui(self, fn):
        try:
            self.mqt.InvokeOntoUIThread(fn)
        except Exception:  # noqa: BLE001
            pass

    def _read_port(self):
        try:
            return int(self.mqt.GetWidgetText(self.portBox).strip())
        except (ValueError, TypeError):
            return DEFAULT_PORT

    def _refresh_context(self, event):
        name = ""
        if self.ctx.IsCaptureLoaded():
            action = self.ctx.GetAction(event)
            if action is not None:
                try:
                    name = action.GetName(self.ctx.GetStructuredFile())
                except Exception:
                    name = getattr(action, "customName", "")
        self.mqt.SetWidgetText(self.contextLabel, "当前事件: EID #%d   %s" % (event, name))

    # -- connection / models ---------------------------------------------

    def _reconnect(self):
        self.port = self._read_port()
        self._set_status("连接中…")

        def worker():
            try:
                session = acp_client.AcpSession(self.port, cwd=_EXT_DIR, mode=self.perm_mode)
                session.open()
                self.acp = session
                names = [m[1] for m in session.models]
                self.model_by_name = dict((m[1], m[0]) for m in session.models)
                cur_name = None
                for mid, name, _desc in session.models:
                    if mid == session.current_model:
                        cur_name = name
                        break

                mode_names = [m[1] for m in session.modes]
                self.mode_by_name = dict((m[1], m[0]) for m in session.modes)
                cur_mode_name = None
                for mid, name, _desc in session.modes:
                    if mid == session.current_mode:
                        cur_mode_name = name
                        break

                def apply():
                    self.mqt.SetComboOptions(self.modelCombo, names)
                    if cur_name:
                        self.mqt.SelectComboOption(self.modelCombo, cur_name)
                    if mode_names:
                        self.mqt.SetComboOptions(self.modeCombo, mode_names)
                        if cur_mode_name:
                            self.mqt.SelectComboOption(self.modeCombo, cur_mode_name)
                    self._set_status("已连接 (127.0.0.1:%d) — %d 个模型，权限: %s"
                                     % (self.port, len(names), session.current_mode))
                self._ui(apply)
            except Exception as exc:  # noqa: BLE001
                self.acp = None
                # Fall back to a health check on the simple gateway.
                ok = codebuddy_client.health(self.port)
                msg = ("已连接(基础模式，无法切换模型): %s" % exc) if ok else (
                    "未连接 — 请先运行 codebuddy --serve 或 "
                    "python -m renderdoc_mcp.cursor_sidecar --port %d" % self.port)
                self._ui(lambda: self._set_status(msg))
        _spawn(worker)

    def _on_model_change(self):
        if self.acp is None:
            return
        try:
            name = self.mqt.GetWidgetText(self.modelCombo)
        except Exception:  # noqa: BLE001
            return
        model_id = self.model_by_name.get(name)
        if not model_id or model_id == self.acp.current_model:
            return

        def worker():
            try:
                self.acp.set_model(model_id)
                self._ui(lambda: self._set_status("已切换模型: %s" % name))
            except Exception as exc:  # noqa: BLE001
                self._ui(lambda: self._set_status("切换模型失败: %s" % exc))
        _spawn(worker)

    def _on_mode_change(self):
        try:
            name = self.mqt.GetWidgetText(self.modeCombo)
        except Exception:  # noqa: BLE001
            return
        mode_id = self.mode_by_name.get(name)
        if not mode_id:
            return
        # Remember the choice so it survives auto-reconnects / manual reconnects.
        self.perm_mode = mode_id
        if self.acp is None or mode_id == self.acp.current_mode:
            return

        def worker():
            try:
                self.acp.set_mode(mode_id)
                self._ui(lambda: self._set_status("已切换权限模式: %s" % name))
            except Exception as exc:  # noqa: BLE001
                self._ui(lambda: self._set_status("切换权限失败: %s" % exc))
        _spawn(worker)

    # -- sending ----------------------------------------------------------

    def _on_input_changed(self, text):
        # returnPressed re-fires the callback with the same text as the last
        # edit; identical consecutive text means the user pressed Enter.
        if text and text == self._last_input:
            self._last_input = None
            self._on_send_click()
        else:
            self._last_input = text

    def _on_send_click(self):
        try:
            text = self.mqt.GetWidgetText(self.input).strip()
        except Exception:  # noqa: BLE001
            text = ""
        if not text:
            return
        self._last_input = None
        self.mqt.SetWidgetText(self.input, "")
        self._send(text, attach_context=True)

    def _on_cancel(self):
        token = self._cancel_token
        if not self.busy or token is None:
            return
        self._set_status("正在取消…")
        self.mqt.SetWidgetEnabled(self.cancelBtn, False)

        def worker():
            # Close the streaming socket locally to unblock the read, and ask
            # CodeBuddy to abandon the run.
            token.cancel()
            if self.acp is not None:
                self.acp.cancel()
        _spawn(worker)

    def _send(self, user_text, attach_context=True, prebuilt=None, label=None):
        if self.busy:
            return
        self.busy = True
        token = http_ctypes.CancelToken()
        self._cancel_token = token
        self.mqt.SetWidgetEnabled(self.sendBtn, False)
        self.mqt.SetWidgetEnabled(self.cancelBtn, True)

        shown = label or user_text
        self.history_text += "🧑 你：\n%s\n\n" % shown
        self.mqt.SetWidgetText(self.history, self.history_text)
        self._reply_base = self.history_text + "🤖 CodeBuddy：\n"
        self.mqt.SetWidgetText(self.history, self._reply_base + "（思考中…）")

        port = self._read_port()

        def worker():
            try:
                # Prefer shared playbook match — local report, no model required.
                matched = None
                if _HAS_PLAYBOOK and prebuilt is None:
                    matched = _pb_match(user_text or label or "", path="panel")
                if matched is not None:
                    reply = _run_playbook_local(matched["id"])
                else:
                    timing = (_is_timing_query(user_text) or _is_timing_query(label or "")
                              or (prebuilt is not None and "GPUDuration" in (prebuilt or "")))
                    drawcalls = (
                        "drawcall" in (user_text or "").lower()
                        or "drawcall" in (label or "").lower()
                        or "绘制" in (user_text or "")
                        or "绘制" in (label or "")
                    )
                    if timing:
                        reply = self._answer_timing(
                            user_text or label or "分析资源耗时",
                            token, port, prebuilt=prebuilt)
                    elif prebuilt is not None:
                        reply = self._answer_local_first(
                            label or user_text or "分析",
                            user_text or label or "",
                            prebuilt, token, port)
                    elif drawcalls:
                        reply = _run_playbook_local("drawcall_heavy")
                    else:
                        if attach_context:
                            body = self._frame_context() + "\n\n用户问题：\n" + user_text
                        else:
                            body = user_text
                        first_prompt = _analysis_prompt(
                            "自由问答", user_text,
                            _model_payload_from_local(body, 3500))
                        reply = self._run_agent_or_local(
                            first_prompt, token, port, body)
                        if not reply:
                            reply = "(CodeBuddy 没有返回文本内容)"

                def finish():
                    self.history_text = self._reply_base + reply + "\n\n"
                    self.mqt.SetWidgetText(self.history, self.history_text)
                self._ui(finish)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)

                def fail():
                    self.history_text = self._reply_base + "⚠️ 出错：" + msg + "\n\n"
                    self.mqt.SetWidgetText(self.history, self.history_text)
                self._ui(fail)
            finally:
                def reset():
                    self.mqt.SetWidgetEnabled(self.sendBtn, True)
                    self.mqt.SetWidgetEnabled(self.cancelBtn, False)
                self._ui(reset)
                self._cancel_token = None
                self.busy = False

        _spawn(worker)

    def _answer_timing(self, question, token, port, prebuilt=None):
        self._ui(lambda: self.mqt.SetWidgetText(
            self.history, self._reply_base + "（正在从 RenderDoc 采样 GPU 耗时…）"))
        if prebuilt and "【本地解读】" in (prebuilt or ""):
            return prebuilt
        try:
            return _gather_timing_bundle()
        except Exception as exc:  # noqa: BLE001
            return "采样失败：%s" % exc

    def _answer_local_first(self, topic, question, local_data, token, port):
        """Show local RenderDoc data first; ask CodeBuddy only for a short gloss.

        If ACP returns ``refusal`` (common with some models), the local block
        (including 【本地解读】) remains usable so the user is never stuck.
        """
        local_block = (
            "【RenderDoc 本地结果 — 不依赖 CodeBuddy】\n"
            "%s\n\n"
            "---\n" % local_data
        )
        self._ui(lambda: self.mqt.SetWidgetText(
            self.history, self._reply_base + local_block + "（正在请求 CodeBuddy 解读…）"))

        # Compact summary only — never the raw counter JSON dump.
        prompt = _analysis_prompt(
            topic, question, _model_payload_from_local(local_data, 3500))
        answer = self._ask_codebuddy_once(prompt, token, port, prefix=local_block)

        if token is not None and token.cancelled():
            return (local_block + "（已取消）").strip()

        if answer is None:
            # One reconnect retry — idle ACP sessions sometimes refuse oddly.
            try:
                if self.acp is not None:
                    self.acp.reopen()
                    answer = self._ask_codebuddy_once(
                        prompt, token, port, prefix=local_block)
            except Exception:  # noqa: BLE001
                answer = None

        if not answer:
            # Soften the failure line: local Top-N + 本地解读 already answer the question.
            hint = (
                "【CodeBuddy】模型未返回可用解读"
                "（ACP refusal 或空回复）。上面的本地 Top-N / 本地解读可直接使用；"
                "也可换模型后重试。\n"
            )
            return (local_block + hint).strip()

        return (local_block + "【CodeBuddy 解读】\n" + answer).strip()

    def _ask_codebuddy_once(self, prompt, token, port, prefix=""):
        """One ACP turn. Returns answer text, or None on hard refusal/empty.

        Prefer the raw agent answer text. Do not treat normal disclaimers
        ("无法直接访问…但根据数据…") as refusal.
        """
        try:
            raw, rendered = self._one_turn(prompt, token, port, prefix)
        except Exception:  # noqa: BLE001
            return None

        answer = (raw or "").strip()
        # If the model produced real answer text, ignore disclaimer heuristics
        # on the rendered transcript (which may embed our own refusal notes).
        if answer:
            if _is_acp_refusal(self.acp) and _looks_like_refusal(answer):
                return None
            if answer.startswith("CodeBuddy 拒绝"):
                return None
            if _looks_like_refusal(answer) and len(answer) < 80:
                return None
            return answer

        # Empty raw answer: fall back to rendered, then apply stricter checks.
        answer = (rendered or "").strip()
        if not answer:
            return None
        if _is_acp_refusal(self.acp) or _looks_like_refusal(answer):
            return None
        if "拒绝了该请求" in answer and "refusal" in answer.lower():
            return None
        return answer

    def _run_agent_or_local(self, first_prompt, token, port, local_fallback=""):
        reply = self._run_agent(first_prompt, token, port)
        # Only treat as refusal when ACP says so, or reply is a short hard refuse.
        acp_refused = _is_acp_refusal(self.acp)
        text_refused = (not (reply or "").strip()) or (
            _looks_like_refusal(reply or "") and len(reply or "") < 120)
        if acp_refused or text_refused:
            if local_fallback:
                return (
                    "【RenderDoc 本地上下文】\n%s\n\n"
                    "【CodeBuddy】未能生成解读。上面的本地上下文可直接使用；也可换模型重试。\n"
                    % _model_payload_from_local(local_fallback, 8000)
                ).strip()
        return reply

    def _run_agent(self, first_prompt, token, port):
        """Drive a tool-calling loop: send a prompt, and while the model asks for
        RenderDoc data (via the @@RDTOOL@@ directive) run the tool and feed the
        result back, until it produces a plain-text answer."""
        prefix = ""          # persistent transcript of tool calls shown in panel
        prompt = first_prompt
        for _step in range(MAX_TOOL_STEPS):
            raw, rendered = self._one_turn(prompt, token, port, prefix)
            if token is not None and token.cancelled():
                return (prefix + rendered).strip() or "（已取消）"

            # Prefer answer text, but also scan the full rendered transcript —
            # some models put the directive outside last_state["answer"].
            call, before = _parse_tool_call(raw)
            if call is None:
                call, before = _parse_tool_call(rendered)

            if call is None:
                return (prefix + rendered).strip()

            name, args = call
            before = (before or "").strip()
            if before:
                prefix += before + "\n"
            prefix += "🔧 调用 %s %s\n" % (name, _short(json.dumps(args, ensure_ascii=False), 300))
            self._ui(lambda p=prefix: self.mqt.SetWidgetText(
                self.history, self._reply_base + p + "（执行工具中…）"))

            result = _run_rd_tool(name, args)
            prefix += "   → " + _short(result, 500) + "\n\n"
            self._ui(lambda p=prefix: self.mqt.SetWidgetText(self.history, self._reply_base + p))
            prompt = _tool_result_prompt(name, result)

        return (prefix + "（已达到工具调用上限，基于已获取的数据给出结论。）").strip()

    def _one_turn(self, prompt, token, port, prefix):
        """Run one CodeBuddy round. Returns (raw_answer_text, rendered_transcript).
        ``raw_answer_text`` is parsed for tool directives; ``rendered`` is what we
        show while streaming."""
        def on_update(md):
            self._ui(lambda: self.mqt.SetWidgetText(self.history, self._reply_base + prefix + md))

        if self.acp is not None and self.acp.session_id:
            rendered = self.acp.prompt(prompt, on_update=on_update, cancel=token)
            try:
                raw = (self.acp.last_state or {}).get("answer", "") or ""
            except Exception:  # noqa: BLE001
                raw = rendered
            return raw, rendered
        reply, _ = codebuddy_client.send(port, prompt, on_update=on_update)
        return reply or "", reply or ""

    # -- frame context ----------------------------------------------------

    def _frame_context(self):
        if live is None or not live.loaded():
            return "【当前帧上下文】RenderDoc 尚未加载任何抓帧。"
        try:
            return "【当前帧上下文】\n" + live.get_current_frame({})
        except Exception as exc:  # noqa: BLE001
            return "【当前帧上下文】获取失败：%s" % exc

    # -- quick actions ----------------------------------------------------

    def _qa_generic(self, label, instruction, gather):
        if self.busy:
            return

        def worker():
            try:
                data = gather()
            except Exception as exc:  # noqa: BLE001
                data = "(获取数据失败: %s)" % exc
            prompt = "%s\n\n以下是 RenderDoc 当前帧的相关数据：\n%s" % (instruction, data)
            self._ui(lambda: self._send(instruction, prebuilt=prompt, label=label))
        _spawn(worker)

    def _qa_analyze_frame(self):
        self._qa_playbook("current_frame", label="分析当前帧")

    def _qa_drawcalls(self):
        self._qa_playbook("drawcall_heavy", label="分析 Drawcalls")

    def _qa_gpu_timing(self):
        self._qa_playbook("gpu_top_draws", label="分析 GPU 耗时")

    def _qa_pipeline(self):
        self._qa_playbook("pipeline_state", label="分析管线状态")

    def _qa_ps_disasm(self):
        self._qa_playbook("ps_disasm", label="分析 PS 反汇编")

    # -- CaptureViewer callbacks -----------------------------------------

    def OnCaptureLoaded(self):
        if live is not None:
            live.current_event = self.ctx.CurEvent()
        self._refresh_context(self.ctx.CurEvent())

    def OnCaptureClosed(self):
        self._refresh_context(0)

    def OnSelectedEventChanged(self, event):
        pass

    def OnEventChanged(self, event):
        if live is not None:
            live.current_event = event
        self._refresh_context(event)


def _apply_dark_theme(ctx):
    try:
        cfg = ctx.Config()
        if cfg.UIStyle != "RDDark":
            cfg.UIStyle = "RDDark"
            cfg.SetStyle()
            cfg.Save()
    except Exception as exc:  # noqa: BLE001
        print("[renderdoc_mcp] could not apply dark theme: %s" % exc)


def _spawn(fn):
    t = threading.Thread(target=fn)
    t.daemon = True
    t.start()


def _window_closed():
    global cur_window
    if cur_window is not None:
        cur_window.ctx.RemoveCaptureViewer(cur_window)
    cur_window = None


def open_window_callback(ctx, data):
    global cur_window
    if cur_window is None:
        cur_window = Window(ctx, extiface_version)
        if ctx.HasTextureViewer():
            ctx.AddDockWindow(cur_window.topWindow, qrd.DockReference.RightOf,
                              ctx.GetTextureViewer().Widget(), 0.3)
        else:
            ctx.AddDockWindow(cur_window.topWindow, qrd.DockReference.MainToolArea, None)
    ctx.RaiseDockWindow(cur_window.topWindow)


def register(version, ctx):
    global extiface_version, live
    extiface_version = version

    live = LiveFrame(ctx)

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Window, ["AI 助手 (CodeBuddy)"], open_window_callback)
    print("[renderdoc_mcp] registered CodeBuddy AI panel.")


def unregister():
    global cur_window
    if cur_window is not None:
        cur_window.ctx.Extensions().GetMiniQtHelper().CloseToplevelWidget(cur_window.topWindow)
        cur_window = None
