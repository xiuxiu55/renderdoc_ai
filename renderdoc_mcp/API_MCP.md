# RenderDoc 对外接口 → MCP 工具对照表

面向 MCP 服务：从 RenderDoc 公开 C++/Python API（`renderdoc/api/replay/renderdoc_replay.h`）整理可封装的功能接口名。

**机器路由表**：[`orchestrator/tools_catalog.json`](orchestrator/tools_catalog.json)  
**意图说明**：[`capabilities.md`](capabilities.md)

Python 绑定（`import renderdoc as rd`）方法名与下表 **PascalCase** 一致，例如 `controller.GetUsage(...)`。

---

## 1. 已封装为 MCP / Panel 的工具名

| MCP / Panel 工具名 | 路径 | 对应 Native API |
|--------------------|------|-----------------|
| `load_capture` | MCP | `ICaptureFile::OpenFile` + `OpenCapture` |
| `close_capture` | MCP | `IReplayController::Shutdown` |
| `get_status` | MCP | 会话状态（非 Replay 单方法） |
| `get_capture_info` | MCP | `GetAPIProperties` + `GetFrameInfo` |
| `get_current_frame` | Panel | `GetAPIProperties` + `GetRootActions` + `GetPipelineState` |
| `list_actions` | Both | `GetRootActions` |
| `get_action` | Both | `ActionDescription`（由 `GetRootActions` 树查找） |
| `set_event` | MCP | `SetFrameEvent` |
| `get_event_chunk` | Both | `GetStructuredFile` |
| `get_pipeline_state` | Both | `GetPipelineState` |
| `list_resources` | Both | `GetResources` |
| `list_textures` | Both | `GetTextures` |
| `list_buffers` | MCP | `GetBuffers` |
| `save_texture` | MCP | `SaveTexture` |
| `get_disassembly_targets` | MCP | `GetDisassemblyTargets` |
| `get_shader_disassembly` | Both | `DisassembleShader` |
| `get_shader_reflection` | Both | `GetShader` / PipeState reflection |
| `get_constant_buffer` | MCP | `GetCBufferVariableContents` |
| `list_counters` | Both | `EnumerateCounters` + `DescribeCounter` |
| `pick_duration_counter` | Panel | `EnumerateCounters` |
| `fetch_counters` | Both | `FetchCounters` |
| `list_hot_questions` | MCP | playbook 元数据 |
| `describe_hot_question` | MCP | playbook 元数据 |
| `run_question` | MCP | playbook |
| `analyze_question` | MCP | orchestrator |

---

## 2. `IReplayController` 全量方法（MCP 候选）

标注：`done` = 已有 MCP/Panel；`P1/P2/P3` = 建议封装优先级。

### 会话 / 帧

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `GetAPIProperties` | （含在 `get_capture_info`） | done |
| `GetFrameInfo` | （含在 `get_capture_info`） | done |
| `GetFatalErrorStatus` | `get_fatal_error_status` | P2 |
| `FileChanged` | `notify_file_changed` | P3 |
| `ClearReplayCache` | `clear_replay_cache` | P3 |
| `AddFakeMarkers` | `add_fake_markers` | P3 |
| `Shutdown` | `close_capture` | done |
| `GetSupportedWindowSystems` / `CreateOutput` / `ReplayLoop` / `CancelReplayLoop` | —（UI） | P3 |
| `CreateRGPProfile` | `create_rgp_profile` | P3 |

### 事件

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `SetFrameEvent` | `set_event` | done |
| `GetRootActions` | `list_actions` / `get_action` | done |
| `GetStructuredFile` | `get_event_chunk` | done |

### 管线

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `GetPipelineState` | `get_pipeline_state` | done（可加深） |
| `GetD3D11PipelineState` | `get_d3d11_pipeline_state` | P2 |
| `GetD3D12PipelineState` | `get_d3d12_pipeline_state` | P2 |
| `GetGLPipelineState` | `get_gl_pipeline_state` | P2 |
| `GetVulkanPipelineState` | `get_vulkan_pipeline_state` | P2 |

### 着色器

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `GetDisassemblyTargets` | `get_disassembly_targets` | done |
| `DisassembleShader` | `get_shader_disassembly` | done |
| `GetShaderEntryPoints` | `get_shader_entry_points` | P2 |
| `GetShader` | `get_shader` | P2 |
| `GetCBufferVariableContents` | `get_constant_buffer` | done |
| `DebugVertex` | `debug_vertex` | P2 |
| `DebugPixel` | `debug_pixel` | P2 |
| `DebugThread` | `debug_thread` | P2 |
| `DebugMeshThread` | `debug_mesh_thread` | P3 |
| `ContinueDebug` / `FreeTrace` | （配合 debug_*） | P2 |
| `BuildTargetShader` / `FreeTargetResource` / `ReplaceResource` / `RemoveReplacement` | `build_target_shader` / `replace_resource` | P3 |
| `BuildCustomShader` / `FreeCustomShader` / `SetCustomShaderIncludes` | —（UI viz） | P3 |
| `GetTargetShaderEncodings` / `GetCustomShaderEncodings` / `GetCustomShaderSourcePrefixes` | `list_shader_encodings` | P3 |
| `ReloadShaderDebugInformation` | `reload_shader_debug_info` | P3 |

### 资源 / 描述符

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `GetResources` | `list_resources` | done |
| `GetTextures` | `list_textures` | done |
| `GetBuffers` | `list_buffers` | done |
| `GetDescriptorStores` | `list_descriptor_stores` | P2 |
| **`GetUsage`** | **`get_resource_usage`** | **P1** |
| `GetDescriptorAccess` | `get_descriptor_access` | P2 |
| `GetDescriptors` | `get_descriptors` | P2 |
| `GetSamplerDescriptors` | `get_sampler_descriptors` | P2 |
| `GetDescriptorLocations` | `get_descriptor_locations` | P2 |

### 纹理 / 像素

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `SaveTexture` | `save_texture` | done |
| **`PickPixel`** | **`pick_pixel`** | **P1** |
| **`PixelHistory`** | **`get_pixel_history`** | **P1** |
| `GetMinMax` | `get_texture_minmax` | P2 |
| `GetHistogram` | `get_texture_histogram` | P2 |
| `GetTextureData` | `get_texture_data` | P2 |

### Buffer / Mesh

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `GetBufferData` | `get_buffer_data` | P2 |
| `GetPostVSData` | `get_post_vs_data` | P2 |

### 性能 / 诊断

| Native 方法 | 建议 MCP 名 | 状态 |
|-------------|-------------|------|
| `EnumerateCounters` / `DescribeCounter` / `FetchCounters` | `list_counters` / `fetch_counters` | done |
| `GetDebugMessages` | `get_debug_messages` | P2 |

---

## 3. 相邻公开接口

### `ICaptureFile`

| Native | 建议 MCP | 状态 |
|--------|----------|------|
| `OpenFile` / `OpenCapture` | `load_capture` | done |
| `GetSection*` / `FindSection*` | `list_capture_sections` | P2 |
| `HasCallstacks` / `GetResolve` | `resolve_callstack` | P2 |
| `Convert` / `GetThumbnail` | `convert_capture` / `get_capture_thumbnail` | P3 |

### `ITargetControl`（注入进程）

| Native | 建议 MCP | 状态 |
|--------|----------|------|
| `TriggerCapture` / `QueueCapture` | `trigger_capture` | P3 |
| `CopyCapture` / `DeleteCapture` | `copy_live_capture` | P3 |
| `GetTarget` / `GetAPI` / `GetPID` | `get_target_info` | P3 |

### `IRemoteServer`

| Native | 建议 MCP | 状态 |
|--------|----------|------|
| `ExecuteAndInject` / `OpenCapture` / `CopyCaptureToRemote` | `remote_*` | P3 |

### `IReplayOutput`

`SetTextureDisplay` / `Display` / `PickVertex` / `AddThumbnail` 等 → **UI 专用，不进 MCP**。

---

## 4. 优先补 MCP（Top 10）

1. `get_resource_usage` ← `GetUsage`
2. `get_pixel_history` ← `PixelHistory`
3. `pick_pixel` ← `PickPixel`
4. `get_descriptor_access` ← `GetDescriptorAccess`
5. `get_descriptors` ← `GetDescriptors`
6. `get_texture_minmax` ← `GetMinMax`
7. `get_buffer_data` ← `GetBufferData`
8. `get_debug_messages` ← `GetDebugMessages`
9. `get_post_vs_data` ← `GetPostVSData`
10. `debug_pixel` ← `DebugPixel`

---

## 5. 维护约定

1. 新工具：选 Native 名 → `server.py` `@mcp.tool` →（可选）`live_frame.py` → `tools_catalog.json`。
2. MCP 名 snake_case；Native 保持 PascalCase。
3. `IReplayOutput` / ReplayLoop 不进 MCP。
4. `GetTextureData` / `GetBufferData` 必须限大小与截断。
