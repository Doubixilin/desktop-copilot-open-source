# 原生 HUD

Windows WPF + WebView2 半透明快捷键入口。默认 `Ctrl+Alt+K` 呼出/收起，Tab 切换文件搜索与副驾；托盘或页面“退出”结束宿主及其归属适配进程。

界面位于 `ui/`，窗口、桥接、文件动作与 HTTP 客户端位于 `host/`。Python `search_adapter.py` 管理真实本机搜索，`scene_adapter.py`、`scene_probe.py` 与可选 `scene_capture.py` 提供现场材料。任务、系统与滴答信息由本地 `server.py` 提供；未配置/失败时按具体功能降级，日程演示明确标注。

从仓库根目录执行 `dotnet build native_hud/host -c Release`，在已激活 Python 环境的终端运行 `native_hud/host/bin/Release/net8.0-windows/NativeHud.exe`。不要拆散相对目录结构。

参数包括 `--hotkey "Ctrl+Shift+K"`、`--screen primary`、`--ui <目录>`、`--page <入口>` 和 `--log <本地日志路径>`。日志默认位于 `%LOCALAPPDATA%/AgentDeckNativeHud/`，不应直接上传。

页面只加载受信本地资产，CSP 禁止页面直接联网；宿主限制导航和桥接动作。真实模型、搜索与日程需要自行配置。窗口材质、多屏 DPI、焦点与中文输入法需要在目标设备验证。

完整说明见 [配置](../docs/setup.md)、[架构](../docs/architecture.md)、[隐私](../docs/privacy.md) 与 [本次验证](../docs/validation.md)。独立演示 `demo/serve.py` 使用同一 UI 与合成数据，不会启动原生宿主或读取个人内容。
