# 界面截图说明

README 的四张图片由本仓库 `native_hud/ui/index.html`、`styles.css` 和 `app.js` 直接渲染，浏览器加载 `demo/bridge.js` 的合成宿主。不来自真实桌面，不使用私人文件、会话、日程、系统指标或模型回复，也没有后期涂抹真实信息。

| 图片 | 展示内容 |
| --- | --- |
| [紧凑入口](images/hud-overview.png) | 搜索输入、合成今日安排和系统摘要 |
| [文件结果](images/hud-files.png) | 三个虚构文件、通用示例路径与选择态 |
| [桌面副驾](images/hud-copilot.png) | 合成窗口材料、固定对话、工具事件和反馈入口 |
| [系统详情](images/hud-system.png) | 按需展开的合成 CPU、内存和 GPU 数值 |

所有任务、日程与回复都是固定样例。界面内部分产品文案使用“实时”表示数据通道，当前截图中的数值仍来自假宿主；图片外围始终标注“合成界面演示”。窗口背景为 CSS 色彩，不能用来证明原生 DWM 亚克力材质。

## 重新生成

启动 `python demo/serve.py --port 18766`。可直接在浏览器查看并截图；自动截图脚本需要 Node.js 与 Playwright：

```powershell
npm install --no-save playwright
npx playwright install chromium
node scripts/capture_demo.cjs http://127.0.0.1:18766
```

也可在 `DEMO_BROWSER_PATH` 环境变量中提供本机已核实的 Chromium 系浏览器绝对路径。脚本只接受回环 URL，阻断非演示来源的网络请求，验证合成搜索与截图流程，并检查私有路径及真实接口返回 404。

输出为宽 1120 像素、高度随实际面板调整的 PNG，保存到 `docs/images/`。图片需逐张目检后再提交。不要用原生程序当前窗口或完整桌面替代公开截图。
