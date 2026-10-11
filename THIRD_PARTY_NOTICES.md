# 第三方依赖与资源

MIT 只适用于本仓库作者拥有权利的代码、文档与合成演示。第三方软件、SDK、模型和服务不因被本项目调用而改用 MIT；安装与再分发应遵循它们各自的实际版本条款。

| 依赖或服务 | 用途 | 官方项目或说明入口 |
| --- | --- | --- |
| psutil | 本机进程与系统指标 | [psutil](https://github.com/giampaolo/psutil) |
| python-zstandard | DSH 压缩会话读取 | [python-zstandard](https://github.com/indygreg/python-zstandard) |
| comtypes | Windows COM / UIA | [comtypes](https://github.com/enthought/comtypes) |
| rapidocr-onnxruntime | 可选本地 OCR，实际权重与运行库按安装来源核验 | [RapidOCR](https://github.com/RapidAI/RapidOCR) |
| agent-client-protocol | ACP 客户端 | [ACP Python SDK](https://github.com/agentclientprotocol/python-sdk) |
| MCP Python SDK | 可选桌面工具接口 | [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) |
| Microsoft WebView2 | 原生 HUD 内嵌网页 | [WebView2](https://learn.microsoft.com/microsoft-edge/webview2/) |
| .NET / WPF | Windows 宿主 | [.NET](https://github.com/dotnet/wpf) |
| Everything / Listary | 本机文件索引与搜索，自行安装 | [Everything](https://www.voidtools.com/)、[Listary](https://www.listary.com/) |
| Wallpaper Engine | 可选网页壁纸宿主，自行购买/安装 | [Wallpaper Engine](https://www.wallpaperengine.io/) |
| 滴答清单 | 可选账户服务与 MCP，账户条款另行适用 | [滴答清单](https://dida365.com/) |

DSH 运行时与所配置的模型服务不包含在本仓库中。本项目只提供客户端与适配逻辑，使用者另行核实运行时获取方式、兼容性、许可与模型数据政策。

项目开发曾与 qoder-deck 系列代码共享背景，公开版限定当前作者维护的 Python 与原生 HUD 方向；未包含其 Electron 实现。网页看板保留终端风格设计，但不包含 TERMINAL 02 原始壁纸、备份、创意工坊素材或字体。Decima Mono 等商业字体不随本仓库分发，也不提供自动复制逻辑。

截图来自本仓库实际 UI，内容为合成样例，背景由 CSS 绘制，不使用原桌面、真实文档或第三方图片。测试中使用的姓名、路径、标题和数字用于构造软件场景，不应用来联系或识别真实个人。

引入新的外部源码、图标、字体、背景或模型时，请保留原许可与作者声明。无法确认授权的材料不纳入公开分发。
