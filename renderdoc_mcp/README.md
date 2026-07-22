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

Add an entry to your `mcp.json` (`.cursor/mcp.json` in the project, or the global
one). Adjust the interpreter, script path and `RENDERDOC_MODULE_PATH`:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "python",
      "args": ["-m", "renderdoc_mcp.server"],
      "cwd": "G:/renderdoc-code",
      "env": {
        "RENDERDOC_MODULE_PATH": "C:/path/to/renderdoc/build/bin"
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

`stage` is one of `Vertex`, `Hull`, `Domain`, `Geometry`, `Pixel`, `Compute`.

## Notes on threading

RenderDoc's `ReplayController` has thread affinity — every call must run on the
thread that opened the capture. This server funnels all RenderDoc interactions
through a single dedicated worker thread (`rd_session.py`), so it stays correct
even though MCP tool handlers can be dispatched from a worker pool.

## In-RenderDoc AI panel (chat with CodeBuddy about the live frame)

Under `extension/` is a RenderDoc **UI extension** that embeds an
"AI 助手 (CodeBuddy)" panel directly inside RenderDoc. You chat with CodeBuddy
about the **frame currently loaded in the UI**, at the currently selected event
— no separate `.rdc` load. Each message is automatically augmented with live
frame context (API, EID, current action, pipeline, bound shaders/targets).

```
RenderDoc panel  --ctypes/ws2_32 HTTP-->  codebuddy --serve (ACP, :8080)
      ^ live frame context (via ctx.Replay().BlockInvoke)
```

### How it connects

CodeBuddy runs separately as an HTTP server (`codebuddy --serve --port 8080`).
RenderDoc bundles a **cut-down Python 3.6 with no `_socket` module**, so the
panel talks to CodeBuddy through a tiny **ctypes/ws2_32 HTTP client**
(`extension/http_ctypes.py`) instead of the standard library. Conversations use
CodeBuddy's **ACP protocol** (`extension/acp_client.py`), which supports model
selection and streaming replies; `extension/codebuddy_client.py` is a simpler
`/api/v1/runs` fallback. Nothing needs to be pip-installed into RenderDoc.

### Install & run

```bash
# Copy the extension into RenderDoc's user extensions folder
python renderdoc_mcp/extension/install.py      # or --link to symlink during dev
```

This installs to `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp` (Windows) or
`~/.local/share/qrenderdoc/extensions/renderdoc_mcp` (Linux). Then:

1. Start CodeBuddy's server: `codebuddy --serve --port 8080`.
2. In RenderDoc: **Tools → Manage Extensions**, select *AI 助手 (CodeBuddy)*,
   click **Load** (tick **Always Load** to auto-load). The panel applies
   RenderDoc's dark UI style.
3. Open the panel via **Window → AI 助手 (CodeBuddy)** and click **重新连接**.
   The model dropdown fills from CodeBuddy's available models.
4. Open a capture, select an event, then type a question or use a quick-action
   button (分析当前帧 / Drawcalls / 管线状态 / PS 反汇编).

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
