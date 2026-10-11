# 安装与配置

先用 README 的独立演示确认界面是否适合自己，再启用需要读取个人数据的真实功能。演示只运行 `demo/serve.py`；真实模式运行 `server.py` 和原生 HUD，它们的行为与配置不同。

## 环境准备

原生宿主面向 Windows。准备 Python 3.10+、.NET 8 SDK 和 WebView2 Runtime，确保当前终端中的 `python` 指向已安装依赖的环境。宿主会以 `python` 启动自己的搜索与现场适配进程，因此启动原生程序的终端也应激活 `.venv`。

`requirements.txt` 包含 psutil、zstandard、comtypes 与本地 OCR 接口；`requirements-copilot.txt` 包含 ACP 和 MCP SDK。OCR 属于可降级能力，未安装或无法加载时不应描述为已完成截图文字识别。首次依赖安装与 .NET 还原需要联网；模型服务也可能联网。

构建命令与启动命令见 README。源码运行保留目录结构；直接将单个 EXE 移到其他位置会导致 UI 与 Python 适配器相对路径失效。目前不提供独立安装包。

## 配置文件搜索

`listary_engine.py` 支持 Everything HTTP 服务与 Listary 本机 HTTP 接口，缺省自动探测。引擎须由用户安装并自行启用对应接口。建议让搜索接口仅监听回环地址，勿暴露私人文件索引到网络。

在启动宿主的同一个终端设置：

```powershell
$env:QD_SEARCH_ENGINE = "everything"
$env:QD_EVERYTHING_PORT = "80"  # 替换为实际本机端口
```

或者：

```powershell
$env:QD_SEARCH_ENGINE = "listary"
$env:QD_LISTARY_PORT = "38431"  # 替换为实际本机端口
```

接口不可达时显示离线。引擎索引范围决定可搜索文件，不代表所有磁盘内容都已被索引。

## 启用 DSH 副驾

本仓库提供 ACP 客户端，不附带 DSH 运行时、模型、服务额度或账户凭据。自行准备支持 `--profile acp` 的兼容运行时及其模型配置。不能用任意命令行聊天程序直接替代 ACP 进程。

```powershell
$copilotDir = Join-Path $env:LOCALAPPDATA "qoder-deck/copilot"
New-Item -ItemType Directory -Path $copilotDir -Force | Out-Null
Copy-Item -LiteralPath copilot/copilot.example.json -Destination (Join-Path $copilotDir "copilot.json")
```

修改复制后的本地配置：

| 字段 | 用途 |
| --- | --- |
| `enabled` | 配置好运行时后改为 `true` |
| `runtime.executable` | 运行时 EXE 的绝对路径 |
| `runtime.args` | 通常为 `["--profile", "acp"]` |
| `runtime.launch_cwd` | 启动运行时的中性目录 |
| `runtime.dsh_home` | 为副驾单独准备的运行时数据目录；不要复用重要会话目录 |
| `default_workspace` | 任务工作区绝对路径；设为自己允许执行端读写的范围 |
| `deck_base_url` | 数据服务地址，默认 `http://127.0.0.1:5000` |
| `mcp_enabled` | 按需启用桌面 MCP，默认关闭 |

`QD_COPILOT_DIR` 可将副驾配置与任务数据库放到其他本地目录。不要把真实配置移进仓库，也不要将凭据放进示例文件。配置 `enabled: false` 后重启服务可停用副驾，不自动删除历史任务库。

HUD 模式以 `QD_PANEL=0` 停用旧 Tk 面板。数据服务通过本机令牌限制控制接口，首次启动在 `%LOCALAPPDATA%/qoder-deck/api-token` 生成令牌。原生宿主默认从相同位置读取，不要将令牌贴到问题、截图或报告里。

## 滴答清单与额度

滴答适配的当前代码端点是 `https://mcp.dida365.com`。自行在服务商账户侧获取 API 口令；服务条款、入口和能力可能变化，请以其现行说明为准。本地 `%LOCALAPPDATA%/qoder-deck/dida.json` 使用 `{"token":"你的本地口令"}` 结构，或在当前进程环境中设置 `QD_DIDA_TOKEN`。不要把真实值提交到 Git。

未配置或同步失败时，HUD 使用明确标注的演示/不可用状态。真实连接后，勾选待办会调用完成任务写回，并非仅修改界面。纯日期截止任务仍是待办，不自动变成定时日程。

额度由 `quota.py` 从支持的本地快照或配置端点获取；配置路径为 `%LOCALAPPDATA%/qoder-deck/quota.json`。额度未知不等于余额为零，窗口剩余量与累计 token 也不是同一个量。

## 可选会话看板

`deck/index.html` 是经典侧栏，`wallpaper/index.html` 是全屏 HUD，两者连接默认本机 5000 端口。源码版使用系统等宽字体；无需商业字体或创意工坊素材。

可以在浏览器查看页面；嵌入桌面背景时，自行准备宿主并加载相应入口。Wallpaper Engine 属于可选的商业软件，本仓库没有替你安装、配置或授权它。

## 桌面编排与恢复

真实服务会启动桌面编排线程。它读取桌面目录、维护图标位置与应用使用分数；不移动、重命名或删除磁盘文件。`pinned.json` 默认空清单，可填本机应用显示名；不要把含私人信息的清单公开提交。

```powershell
python zones_orchestrate.py                   # 仅打印计划
python zones_orchestrate.py --apply           # 明确执行全量坐标编排
python desktop_layout.py list
python desktop_layout.py restore last         # 最近运行前快照
python desktop_layout.py restore factory      # 首次保存的布局
```

`--apply` 会重置应用区的手工摆位。Explorer 图标控件是未文档化接口，系统更新后可能失效；恢复快照与停止编排需要按实际现场操作。项目不修改用户文件内容。

## 常见问题

| 现象 | 检查 |
| --- | --- |
| 演示端口无法绑定 | 换用 `--port`；不要为演示修改系统防火墙或保留端口 |
| HUD 不搜索 | 检查启动宿主终端的 Python 环境、引擎接口、引擎端口 |
| 副驾未启用 | 检查本地配置、绝对路径、ACP 依赖及兼容运行时 |
| 任务中枢未连接 | 检查服务是否运行、端口与本机令牌位置是否一致 |
| 现场材料不完整 | 查看覆盖提示；UIA 和 OCR 不能保证读取完整文档 |
| 热键冲突 | 用 `--hotkey "Ctrl+Shift+K"` 或托盘入口 |
| 拆散程序目录后报错 | 恢复完整源码目录结构并重新构建 |

退出原生窗口使用托盘或页面“退出”。退出手动运行的数据服务使用 Ctrl+C。`scripts/create_startup_shortcut.ps1` 可选地创建用户自启快捷方式，执行前阅读脚本；本次开源准备没有修改本机自启设置。
