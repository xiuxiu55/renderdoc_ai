# RenderDoc 意图 → 工具路由表

面向 AI / MCP：按**用户意图**选工具，而不是按 RenderDoc UI 菜单逐项映射。

两条路径能力不完全相同：

| 路径 | 入口 | 抓帧来源 |
|------|------|----------|
| **Standalone MCP** | `server.py`（Cursor 等） | 自开 `.rdc` |
| **In-UI 面板** | `extension/live_frame.py` | 当前 UI 已打开的抓帧 |

图例：`MCP` = 仅 standalone；`Panel` = 仅面板；`Both` = 两边都有。

---

## 1. 意图路由（先看这张表）

| 用户意图（关键词示例） | 推荐调用顺序 | 可用路径 | 说明 |
|------------------------|--------------|----------|------|
| 有没有打开抓帧 / 当前在哪 | `get_status` → 可选 `get_capture_info` | MCP；面板用 `get_current_frame` | 面板没有 `get_status`，用当前帧概览代替 |
| 这帧是什么 API / 驱动 / 概览 | `get_capture_info` 或 `get_current_frame` | Both（名称不同） | 面板：`get_current_frame` |
| Event Browser / 事件树 / drawcall 列表 | `list_actions(drawcalls_only=true)` | Both | `max_depth` 控制层级；默认可只取 draw/dispatch |
| 某个 EID 在干什么 | `set_event`（MCP）→ `get_action` → 可选 `get_event_chunk` | Both（`set_event` 仅 MCP） | 面板光标跟 UI 选中事件走 |
| 某次 API 调用的参数 | `get_event_chunk(event_id)` | Both | 结构化 chunk，不是耗时 |
| **GPU 耗时 / 性能 / 瓶颈 / Event Browser 耗时** | `list_actions(drawcalls_only=true)` → `fetch_counters(["GPUDuration"])` | Both | 耗时来自计数器，**不是** `list_resources`。面板对这类问题会预取 |
| 有哪些性能计数器 | `list_counters` → 再 `fetch_counters` | Both | 先枚举再采样 |
| 管线状态 / PSO / RT / 视口 | `get_pipeline_state` | Both | 可指定 `event_id` |
| PS/VS/CS 反汇编 | `get_shader_disassembly(stage=…)` | Both | stage: `Vertex/Hull/Domain/Geometry/Pixel/Compute` |
| 着色器输入输出 / 资源绑定反射 | `get_shader_reflection(stage=…)` | Both | 看签名与绑定槽位 |
| 可用反汇编格式 | `get_disassembly_targets` | MCP | 面板暂无；默认 target 即可 |
| 常量缓冲 / cbuffer 内容 | `get_constant_buffer(stage, slot?)` | MCP | 面板暂缺 |
| 纹理列表 / 分辨率格式 | `list_textures` | Both | 可用 `name_filter` |
| 资源列表（含非纹理） | `list_resources` | Both | 只有 id/name/type，**无耗时** |
| Buffer 列表 | `list_buffers` | MCP | 面板暂缺 |
| 导出/保存纹理截图 | `save_texture(resource_id, out_path, …)` | MCP | 面板暂缺 |
| 打开 / 关闭 `.rdc` | `load_capture` / `close_capture` | MCP | 面板使用 UI 已加载的抓帧，不要走这条 |

### 常见误路由（不要这样）

| 用户说的 | 错误工具 | 正确做法 |
|----------|----------|----------|
| 「所有资源耗时」 | `list_resources` | `fetch_counters(["GPUDuration"])` + `list_actions` |
| 「Event Browser 分析」却只要树 | 只 `list_actions` | 要性能时必须再采 `GPUDuration` |
| 「黑屏 / RT 不对」 | 只看 drawcall 名 | `get_pipeline_state` + `list_textures`，MCP 可再 `save_texture` |
| 「这个 shader 慢」 | 只 `list_actions` | 先 `fetch_counters` 定位 EID，再 `get_shader_disassembly` / `get_shader_reflection` |

---

## 2. 工具清单（按类别）

### 会话 / 抓帧

| 工具 | 路径 | 作用 |
|------|------|------|
| `load_capture` | MCP | 打开 `.rdc` |
| `close_capture` | MCP | 关闭抓帧 |
| `get_status` | MCP | 是否已加载、当前 EID |
| `get_capture_info` | MCP | API / 驱动 / 帧信息 |
| `get_current_frame` | Panel | 当前选中事件 + 管线摘要 |

### 事件 / Event Browser

| 工具 | 路径 | 作用 |
|------|------|------|
| `list_actions` | Both | 事件/绘制树 |
| `get_action` | Both | 单 action 详情 |
| `set_event` | MCP | 移动 replay 光标 |
| `get_event_chunk` | Both | API 调用参数 |

### 性能

| 工具 | 路径 | 作用 |
|------|------|------|
| `list_counters` | Both | 枚举计数器 |
| `fetch_counters` | Both | 采样；耗时用 `GPUDuration` |

### 管线 / 着色器

| 工具 | 路径 | 作用 |
|------|------|------|
| `get_pipeline_state` | Both | 管线绑定与目标 |
| `get_shader_disassembly` | Both | 反汇编 |
| `get_shader_reflection` | Both | 反射 |
| `get_disassembly_targets` | MCP | 反汇编格式列表 |
| `get_constant_buffer` | MCP | 读 CB 变量值 |

### 资源

| 工具 | 路径 | 作用 |
|------|------|------|
| `list_textures` | Both | 纹理元数据 |
| `list_resources` | Both | 资源元数据 |
| `list_buffers` | MCP | Buffer 元数据 |
| `save_texture` | MCP | 导出纹理到文件 |

---

## 3. 面板快捷按钮 ↔ 意图

| 按钮 | 等价意图 | 预取数据 |
|------|----------|----------|
| 分析当前帧 | 概览 + 当前事件 | `get_current_frame` |
| Drawcalls | 绘制列表 / 谁可能最重 | `list_actions(drawcalls_only)` |
| GPU 耗时 | Event Browser 真实耗时 | `list_actions` + `fetch_counters(GPUDuration)`（本地排序 Top-N） |
| 管线状态 | PSO / RT / 绑定 | `get_pipeline_state` |
| PS 反汇编 | Pixel shader 分析 | `get_shader_disassembly(Pixel)` |

自由输入时：若命中耗时/性能类关键词，面板会走与「GPU 耗时」相同的预取路径，不依赖模型自觉发 `@@RDTOOL@@`。

---

## 4. 推荐调用链（抄作业）

**全帧 GPU 耗时**

```
list_actions(drawcalls_only=true)
fetch_counters(counters=["GPUDuration"])
→ 按 value 排序，关联 eventId ↔ name，解读 Top 瓶颈
```

**某个慢 draw 深挖**

```
get_action(event_id)
get_pipeline_state(event_id)
get_shader_disassembly(stage="Pixel", event_id=…)
get_shader_reflection(stage="Pixel", event_id=…)
# MCP 可选：
get_constant_buffer(stage="Pixel", slot=0, event_id=…)
save_texture(resource_id=…, out_path=…)
```

**纹理 / RT 排查**

```
get_pipeline_state          # 看当前绑定的 color/depth
list_textures(name_filter?) # 找候选 RT
# MCP：save_texture(...)
```

---

## 5. 尚未封装（暂不支持自动调用）

以下 RenderDoc 能力常见但当前 MCP/面板**没有**工具，遇到应如实说明，勿伪造结果：

- Pixel History / Debug Pixel
- Mesh output / post-VS 数据
- Buffer 内容 hex 读取（仅有 `list_buffers` 元数据）
- Histogram / MinMax / 纹理采样点
- 自定义 shader 可视化
- 多帧对比、Capture 管理以外的 UI 操作

补接口时：先在本表「意图路由」加一行，再实现 `server.py` 与（如需）`live_frame.py`，并更新面板 `RD_TOOL_SPECS`。

---

## 6. 维护约定

1. **新工具**：同步改 `server.py`（MCP）、`live_frame.py`（面板）、本表、面板 `RD_TOOL_SPECS`。
2. **高频意图**：优先做本地预取（参考 GPU 耗时），不要只靠模型 invent 工具调用。
3. **命名**：工具名保持稳定；用户口语写在「关键词示例」列，不改 API 名。