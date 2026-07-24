# RenderDoc AI 启动与使用

本仓库在 RenderDoc 上集成了：

- **本地热门问题 Playbook**（不依赖任何 AI 额度）
- **面板聊天后端**：CodeBuddy **或** Cursor sidecar（二选一）
- **Cursor MCP**：在 Cursor IDE 里分析 `.rdc`

---

## 1. 编译并启动 RenderDoc

在仓库根目录执行：

```bat
build_and_run.bat
```

或 PowerShell：

```powershell
.\build_and_run.ps1
```

脚本会：结束旧进程 → 编译 `Development|x64` → 安装 AI 扩展 → 启动 `qrenderdoc.exe`。

可选参数：

```powershell
.\build_and_run.ps1 -Configuration Release
.\build_and_run.ps1 -SkipInstall    # 不重装扩展
.\build_and_run.ps1 -NoLaunch       # 只编译不启动
```

产物路径：

```text
x64\Development\qrenderdoc.exe
```

---

## 2. 加载 AI 面板扩展

首次或扩展更新后：

1. RenderDoc → **Tools → Manage Extensions**
2. 找到 **AI 助手 (CodeBuddy)** → **Load**（建议勾选 **Always Load**）
3. **Window → AI 助手 (CodeBuddy)** 打开面板

也可单独安装扩展：

```bat
python renderdoc_mcp\extension\install.py
```

安装目录：`%APPDATA%\qrenderdoc\extensions\renderdoc_mcp`

---

## 3. 日常怎么用（推荐顺序）

### A. 本地分析（无需后端）

打开抓帧后，在面板中：

- 下拉 **热门问题** → 点 **分析**
- 或点快捷按钮：**分析当前帧 / Drawcalls / GPU 耗时 / 管线状态 / PS 反汇编**

这些走本地 Playbook，**不需要** CodeBuddy / Cursor sidecar。

### B. 面板里和 AI 聊天（需启动一个后端）

聊天后端二选一，**不要同时占 8080 端口**。

面板上有 **复制 CodeBuddy 命令** / **复制 Cursor sidecar 命令** 按钮，点一下即可复制到剪贴板。

#### 方案 1：CodeBuddy（有额度时）

```bat
codebuddy --serve --port 8080
```

然后在面板点 **重新连接**。

#### 方案 2：Cursor sidecar（CodeBuddy 没额度时）

1. 申请 API Key：Cursor Dashboard → 左侧 **API Keys** → 创建并复制  
   https://cursor.com/dashboard/integrations （再点侧栏 **API Keys**）
2. 安装依赖（首次）：

```bat
pip install httpx starlette uvicorn
```

3. 保存 Key（推荐只做一次），然后启动：

```bat
set_cursor_api_key.bat
start_cursor_sidecar.bat
```

说明：在另一个 CMD 里 `set CURSOR_API_KEY=...` **不会**作用到双击打开的 `.bat`。
`set_cursor_api_key.bat` 会写入仓库根目录的 `.cursor_api_key`（已 gitignore）。

若坚持在当前窗口临时设置：

```bat
set CURSOR_API_KEY=cursor_你的密钥
start_cursor_sidecar.bat
```

4. 面板点 **重新连接**（端口保持 `8080`）。

---

## 4. 在 Cursor IDE 里用 MCP（可选）

项目已配置 [`.cursor/mcp.json`](.cursor/mcp.json)。

1. 重启 Cursor，或到 **Settings → MCP** 刷新
2. 确认 `renderdoc` 服务在线
3. 在对话里可调用例如：
   - `list_hot_questions`
   - `run_question`
   - `load_capture` / `fetch_counters` 等

说明：本仓库的 `renderdoc.pyd` 按 **Python 3.6** 构建；MCP 进程用 3.12 可启动并列出工具，完整 replay 需要匹配的 Python 版本或改编到 3.12。

这与 RenderDoc 面板互不冲突，CodeBuddy / sidecar 配置也不会被删掉。

---

## 5. 一条龙检查清单

| 步骤 | 做什么 | 是否必须 |
|------|--------|----------|
| 1 | `build_and_run.bat` | 首次 / 改 C++ 后 |
| 2 | 扩展 Load + 打开 AI 面板 | 要用面板时 |
| 3 | 热门问题 / GPU 耗时 | 本地分析，推荐先做 |
| 4 | 启动 CodeBuddy **或** Cursor sidecar | 仅面板自由聊天需要 |
| 5 | 面板 **重新连接** | 聊天前 |
| 6 | Cursor MCP 刷新 | 仅在 Cursor 里分析 `.rdc` 时 |

---

## 6. 常见问题

**面板显示未连接**  
先确认 8080 上已有 `codebuddy --serve` 或 `cursor_sidecar`，再点重新连接。

**复制命令后终端报错找不到模块**  
在仓库根目录执行 Cursor sidecar；并确认已 `pip install httpx starlette uvicorn`。

**Cursor sidecar 提示没有 CURSOR_API_KEY / Invalid User API Key (401)**  
1. 打开 https://cursor.com/dashboard/integrations → **API Keys** → 新建 User API Key  
2. 关掉旧 sidecar，重新跑一次 `set_cursor_api_key.bat`（会重写 `.cursor_api_key`，去掉以前 `echo` 留下的尾部空格）  
3. 再 `start_cursor_sidecar.bat`；启动日志里的 `api_key=xxxx...yyyy (len=N)` 可核对长度是否正常  
不要用 GitHub / Integrations 里其它令牌冒充 API Key。

**Cursor sidecar 报 WinError 10038 / 非套接字**  
本地 `cursor-sdk` Bridge 在 Windows 上会踩 `select()` 坑。当前 sidecar 已改为 **Cloud Agents HTTP**（`api.cursor.com`）。请**完全关掉**旧的 sidecar 窗口后重新运行 `start_cursor_sidecar.bat`，再在面板点「重新连接」。首条回复可能要等几十秒（云端创建 agent + 跑完）。


**只要性能 Top-N，不想开 AI**  
只用 **GPU 耗时** / 热门问题即可，不必启动任何 serve。

---

## 7. 相关文件

| 文件 | 作用 |
|------|------|
| `build_and_run.bat` / `.ps1` | 编译 + 装扩展 + 启动 RenderDoc |
| `set_cursor_api_key.bat` | 保存 API Key 到 `.cursor_api_key` |
| `start_cursor_sidecar.bat` / `.ps1` | 启动 Cursor 聊天后端 |
| `renderdoc_mcp/extension/` | 面板扩展 |
| `renderdoc_mcp/cursor_sidecar/` | Cursor sidecar 实现 |
| `renderdoc_mcp/playbook/` | 热门问题库 |
| `.cursor/mcp.json` | Cursor MCP 配置 |
| `renderdoc_mcp/README.md` | 更完整的 MCP / 能力说明 |
| `renderdoc_mcp/capabilities.md` | 意图 → 工具路由表 |
