# RenderDoc MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
[RenderDoc](https://renderdoc.org)'s replay analysis to AI agents (Cursor, Claude
Desktop, etc.). It lets an assistant load a `.rdc` capture and inspect the frame:
walk the draw/event tree, move the replay cursor, read pipeline state, dump
shader disassembly and reflection, list resources/textures, save textures to
disk, and sample GPU counters.

It is built directly on top of RenderDoc's Python API (`renderdoc` module), the
same API used by the RenderDoc UI and the `docs/python_api` examples in this
repository.

## Two ways to use it

1. **Standalone MCP server** (`server.py`) — opens its own `.rdc` file. Best for
   headless/automation and connecting an MCP host like Cursor. See below.
2. **In-RenderDoc AI panel** (`extension/`) — embeds an "AI 助手 (CodeBuddy)"
   panel *inside RenderDoc* that connects directly to CodeBuddy and analyzes the
   live frame you are looking at. See "In-RenderDoc AI panel".

## Requirements

- A built copy of RenderDoc producing the native python module
  (`renderdoc.pyd` on Windows / `renderdoc.so` on Linux) and its native library
  (`renderdoc.dll` / `librenderdoc.so`).
- **Python must match the version RenderDoc was built against.** On Windows the
  default is Python 3.6. If you built against a different Python, use that
  interpreter to run this server.
- The `mcp` package: `pip install "mcp"`.

## Installation

```bash
cd renderdoc_mcp
pip install -r requirements.txt
```

Tell the server where the native RenderDoc module lives (the directory that
contains `renderdoc.pyd`/`renderdoc.so` and the native library):

```bash
# Windows (PowerShell)
$env:RENDERDOC_MODULE_PATH = "C:\path\to\renderdoc\build\bin"

# Linux
export RENDERDOC_MODULE_PATH=/path/to/renderdoc/build/bin
```

You can also pass `module_path` directly to the `load_capture` tool instead of
using the environment variable.

## Running

```bash
python -m renderdoc_mcp.server
# or, from inside this folder:
python server.py
```

The server speaks MCP over stdio.

## Configuring in Cursor

This repo already ships a project MCP config at [`.cursor/mcp.json`](../.cursor/mcp.json).
It points at the local Development build and does **not** replace the in-UI
CodeBuddy panel — both can coexist:

| 入口 | 用途 |
|------|------|
| Cursor MCP (`renderdoc`) | 在 Cursor 里 `load_capture` / `run_question` 分析 `.rdc` |
| RenderDoc 面板 CodeBuddy | 仍可用 `codebuddy --serve`（有额度时） |
| 面板热门问题 Playbook | 本地分析，不依赖 CodeBuddy / Cursor |

Reload MCP in Cursor (**Settings → MCP → refresh**, or restart Cursor) after
editing `.cursor/mcp.json`.

**Python ABI note:** `renderdoc.pyd` in this tree is built for **Python 3.6**.
The MCP config uses Python 3.12 so the server process can start and expose
playbook tools; `load_capture` / replay tools need a matching 3.6 interpreter
(or rebuild RenderDoc against 3.12 and point `command` at that Python).

Manual / alternate `mcp.json` shape:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe",
      "args": ["-m", "renderdoc_mcp.server"],
      "cwd": "G:/renderdoc_ai_XIUXIU",
      "env": {
        "RENDERDOC_MODULE_PATH": "G:/renderdoc_ai_XIUXIU/x64/Development",
        "PYTHONPATH": "G:/renderdoc_ai_XIUXIU;G:/renderdoc_ai_XIUXIU/x64/Development/pymodules;G:/renderdoc_ai_XIUXIU/x64/Development"
      }
    }
  }
}
```

## Available tools

For **intent → tool routing** (what to call for timing, Event Browser, shaders, etc.),
see [`capabilities.md`](capabilities.md).

Session lifecycle:

- `load_capture(path, module_path?)` — open a `.rdc` and begin replay.
- `close_capture()` — release the capture.
- `get_status()` — is a capture loaded, current cursor event.
- `get_capture_info()` — API, renderer, frame info.

Actions / events:

- `list_actions(parent_event_id?, max_depth?, drawcalls_only?)` — the action tree.
- `get_action(event_id)` — draw params, outputs and API events for one action.
- `set_event(event_id, force?)` — move the replay cursor.
- `get_event_chunk(event_id)` — the recorded API call and its parameters.

Pipeline / shaders:

- `get_pipeline_state(event_id?)` — topology, bound shaders, viewports, targets.
- `get_disassembly_targets()` — available disassembly formats.
- `get_shader_disassembly(stage, event_id?, target?)` — disassemble a shader.
- `get_shader_reflection(stage, event_id?)` — inputs/outputs/CBs/resources.
- `get_constant_buffer(stage, slot?, event_id?)` — read constant/uniform values.

Resources:

- `list_resources(name_filter?)`
- `list_textures(name_filter?)`
- `list_buffers()`
- `save_texture(resource_id, out_path, event_id?, file_type?, mip?, array_slice?)`

GPU counters:

- `list_counters()`
- `fetch_counters(counters, event_ids?)`

Auto analysis + hot-question playbook (local, no LLM required):

- `analyze_question(text, params?)` — natural language → Intent→Plan→Execute → report
- `list_hot_questions(tag?)` — catalog from `playbook/questions.json`
- `describe_hot_question(question_id)` — collect steps / followups
- `run_question(question_id, params?)` — collect + rule-based report for a known id

Prefer `analyze_question` for free-form questions; `run_question` when you know the id.
Shared logic lives in `orchestrator/` + `playbook/`; see [`capabilities.md`](capabilities.md).

`stage` is one of `Vertex`, `Hull`, `Domain`, `Geometry`, `Pixel`, `Compute`.

## Notes on threading

RenderDoc's `ReplayController` has thread affinity — every call must run on the
thread that opened the capture. This server funnels all RenderDoc interactions
through a single dedicated worker thread (`rd_session.py`), so it stays correct
even though MCP tool handlers can be dispatched from a worker pool.

## In-RenderDoc AI panel (chat about the live frame)

Under `extension/` is a RenderDoc **UI extension** that embeds an AI assistant
panel. You can chat about the **frame currently loaded in the UI**. Hot-question
buttons run **locally** (no backend). Free-form chat needs one of:

| Backend | How to start | Notes |
|---------|--------------|--------|
| **Cursor sidecar** (recommended if CodeBuddy has no quota) | `start_cursor_sidecar.bat` or `python -m renderdoc_mcp.cursor_sidecar --port 8080` | Needs `CURSOR_API_KEY` + `pip install cursor-sdk` |
| **CodeBuddy** | `codebuddy --serve --port 8080` | Unchanged; use when you have CodeBuddy quota |

```
RenderDoc panel  --ctypes/ws2_32 HTTP-->  cursor_sidecar OR codebuddy  (:8080)
      ^ live frame / playbook (local)
```

Both backends speak the same CodeBuddy-compatible ACP + `/api/v1/runs` surface.
Only one process should listen on port 8080 at a time.

### Cursor sidecar

```bash
# once
pip install cursor-sdk starlette uvicorn
set CURSOR_API_KEY=cursor_...   # https://cursor.com/dashboard/integrations

# run (repo root)
start_cursor_sidecar.bat
# or:
python -m renderdoc_mcp.cursor_sidecar --port 8080 --cwd G:/renderdoc_ai_XIUXIU
```

### How the panel connects

RenderDoc bundles a **cut-down Python 3.6 with no `_socket` module**, so the
panel uses a tiny **ctypes/ws2_32 HTTP client** (`extension/http_ctypes.py`).
Conversations use **ACP** (`extension/acp_client.py`) with `/api/v1/runs`
(`codebuddy_client.py`) as fallback. Nothing Cursor-related is installed into
RenderDoc itself — only the sidecar process needs `cursor-sdk`.

### Install & run the panel

```bash
python renderdoc_mcp/extension/install.py      # or --link during dev
```

This installs to `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp` (Windows) or
`~/.local/share/qrenderdoc/extensions/renderdoc_mcp` (Linux). Then:

1. Start **either** Cursor sidecar **or** `codebuddy --serve --port 8080`.
2. In RenderDoc: **Tools → Manage Extensions**, select *AI 助手 (CodeBuddy)*,
   click **Load** (tick **Always Load** to auto-load).
3. Open **Window → AI 助手 (CodeBuddy)** and click **重新连接**.
4. Open a capture; use hot questions (local) or type a question (needs backend).

### Live-frame context

The panel builds prompt context using `extension/live_frame.py`:
`get_current_frame`, `list_actions`, `get_pipeline_state`,
`get_shader_disassembly`, etc. — always describing whatever you have open and
selected in RenderDoc.

## Example prompts

- "Load the capture at C:/caps/frame.rdc and tell me what API it uses."
- "List all the draw calls and find the one with the most indices."
- "Show me the pixel shader disassembly at event 645."
- "Save the color target at event 630 to C:/tmp/out.png."
- "Fetch GPUDuration for every draw and tell me the slowest one."
