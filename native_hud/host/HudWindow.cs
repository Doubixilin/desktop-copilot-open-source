using System.Linq;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Interop;
using Microsoft.Web.WebView2.Core;
using WF = System.Windows.Forms;

namespace NativeHud;

/// <summary>
/// 原生 HUD 宿主：非分层普通 WPF 窗（窗口即面板矩形）+ WebView2。
/// 全局快捷键 Ctrl+Alt+K 呼出/收起；页面经窄 JSON 桥请求隐藏、退出、搜索与文件动作。
/// 窗口由 DWM 亚克力材质与圆角修饰；面板位置（顶居中）由宿主决定，高度随页面内容。
/// </summary>
internal sealed class HudWindow : Window
{
    private const int HotkeyId = 1;
    private const string VirtualHost = "hud.agentdeck";

    private readonly Microsoft.Web.WebView2.Wpf.WebView2 _webView;
    private readonly string _uiDir;
    private readonly string _entryPage;
    private readonly string _hotkeyLabel;
    private readonly uint _hotkeyMods;
    private readonly uint _hotkeyVk;
    private SearchClient? _search;
    private string? _searchUnavailable;
    private SceneClient? _scene;
    private CopilotHubClient? _hub;
    private readonly string[] _args;
    private bool _hubEverOk;
    private System.Windows.Threading.DispatcherTimer? _hubTimer;
    private readonly object _resultGate = new();
    private readonly Dictionary<string, string> _resultPaths = new();

    private HwndSource? _source;
    private IntPtr _hwnd;
    private IntPtr _previousForeground;
    private long _latestRequestId = -1;
    // 票02 现场观测代次：呼出或前台目标变化时前进；迟到的旧代次结果由页面按
    // scene_id 丢弃，禁止污染当前现场（R04）
    private readonly object _sceneGate = new();
    private string _sceneId = "scene-0";
    private long _sceneGeneration;
    private SceneInfo? _lastIdentity;
    private double _targetScale = 1;
    private System.Drawing.Rectangle _workAreaPhys;
    private int _panelXPhys;
    private int _panelYPhys;
    private int _panelWidthPhys;
    private int _maxPanelHeightPhys;
    private double _lastPanelHeightCss;
    private bool _useRegionCorners;
    private bool _forcePrimaryScreen;
    private bool _hotkeyRegistered;
    private string? _hotkeyError;
    private bool _exiting;
    private WF.NotifyIcon? _tray;
    private EventWaitHandle? _showEvent;
    private Thread? _showListener;

    public HudWindow(string[] args)
    {
        _uiDir = ResolveArg(args, "--ui") ?? Path.GetFullPath(
            Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "ui"));
        _entryPage = ResolveArgRaw(args, "--page") ?? "index.html";
        (_hotkeyMods, _hotkeyVk, _hotkeyLabel) = ParseHotkey(ResolveArgRaw(args, "--hotkey") ?? "Ctrl+Alt+K");
        // 诊断用：--screen primary 固定在内屏（默认跟随前台窗口所在屏）
        _forcePrimaryScreen = string.Equals(ResolveArgRaw(args, "--screen"), "primary", StringComparison.OrdinalIgnoreCase);
        try
        {
            _search = new SearchClient();
        }
        catch (Exception ex)
        {
            _searchUnavailable = ex.Message;
            Log.Write("search adapter unavailable at startup: " + ex.Message);
        }
        // 现场感知 L0（M1）：失败降级为"无现场行"，不影响其余功能
        try
        {
            _scene = new SceneClient();
            _scene.OnUpdate += OnSceneUpdate;
        }
        catch (Exception ex)
        {
            _scene = null;
            Log.Write("scene adapter unavailable at startup: " + ex.Message);
        }

        // 任务中枢（M5）：server.py /copilot/hud*；不可达由 UI 诚实显示
        string hubUrl = ResolveArgRaw(args, "--copilot-url") ?? "http://127.0.0.1:5000";
        string? hubToken = ResolveArgRaw(args, "--copilot-token") ?? CopilotHubClient.LoadDefaultToken();
        _hub = new CopilotHubClient(hubUrl, hubToken);
        _hubTimer = new System.Windows.Threading.DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(2),
        };
        _hubTimer.Tick += (_, __) => _ = PollHubAsync();

        _args = args;
        WindowStyle = WindowStyle.None;
        // 非分层普通窗口（窗口即面板矩形）：命中测试与合成都走常规路径，
        // WebView2 子窗口点击/打字不受影响；透明感由 DWM 亚克力材质提供。
        AllowsTransparency = false;
        Background = System.Windows.Media.Brushes.Transparent;
        ResizeMode = ResizeMode.NoResize;
        ShowInTaskbar = false;
        Topmost = true;
        Title = "Agent Deck HUD";

        _webView = new Microsoft.Web.WebView2.Wpf.WebView2();
        Content = _webView;

        SourceInitialized += OnSourceInitialized;
        Activated += (_, __) => _webView.Focus();  // 失焦后点回面板立即恢复输入
        Closed += OnClosed;
    }

    private static string? ResolveArg(string[] args, string name)
    {
        var raw = ResolveArgRaw(args, name);
        return raw is null ? null : Path.GetFullPath(raw);
    }

    private static string? ResolveArgRaw(string[] args, string name)
    {
        for (int i = 0; i + 1 < args.Length; i++)
        {
            if (args[i] == name) return args[i + 1];
        }
        return null;
    }

    // “Ctrl+Alt+K” 形式；仅修饰键 + 单个字母/数字/F1-F24，不合法则禁用热键并提示
    private static (uint mods, uint vk, string label) ParseHotkey(string s)
    {
        uint mods = Win32.MOD_NOREPEAT;
        uint vk = 0;
        foreach (var part in s.Split('+', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
        {
            switch (part.ToLowerInvariant())
            {
                case "ctrl": mods |= Win32.MOD_CONTROL; break;
                case "alt": mods |= Win32.MOD_ALT; break;
                case "shift": mods |= Win32.MOD_SHIFT; break;
                case "win": mods |= Win32.MOD_WIN; break;
                default:
                    if (part.Length == 1) vk = char.ToUpperInvariant(part[0]);
                    else if (part.Length >= 2 && (part[0] == 'F' || part[0] == 'f') &&
                             int.TryParse(part[1..], out int fn) && fn >= 1 && fn <= 24)
                        vk = (uint)(0x70 + fn - 1);
                    break;
            }
        }
        return (mods, vk, s);
    }

    // ---------- 启动 ----------

    private async void OnSourceInitialized(object? sender, EventArgs e)
    {
        _source = (HwndSource)PresentationSource.FromVisual(this)!;
        _hwnd = _source.Handle;
        _source.AddHook(WndProc);
        Log.Write($"window hwnd=0x{_hwnd:X} dpi={Win32.GetDpiForWindow(_hwnd)} ui={_uiDir} page={_entryPage}");

        RegisterGlobalHotkey();
        StartShowEventListener();
        InitTray();
        EnableAcrylic();
        TryRoundCorners();
        await InitWebViewAsync();

        // 首次启动显示一次并聚焦输入
        ShowHud("first-run");
    }

    private void RegisterGlobalHotkey()
    {
        if (_hotkeyVk == 0)
        {
            _hotkeyError = $"快捷键配置「{_hotkeyLabel}」无法识别，已禁用全局热键。可用托盘图标呼出/退出。";
            Log.Write(_hotkeyError);
            return;
        }
        _hotkeyRegistered = Win32.RegisterHotKey(_hwnd, HotkeyId, _hotkeyMods, _hotkeyVk);
        Log.Write($"hotkey {_hotkeyLabel} registered={_hotkeyRegistered}");
        if (!_hotkeyRegistered)
        {
            _hotkeyError = $"全局快捷键 {_hotkeyLabel} 注册失败：可能与其他应用冲突。可用托盘图标呼出/退出，或换 --hotkey 参数。";
        }
    }

    private void StartShowEventListener()
    {
        _showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, Program.ShowEventName);
        _showListener = new Thread(() =>
        {
            while (!_exiting)
            {
                bool signaled;
                try { signaled = _showEvent.WaitOne(500); }
                catch { return; }
                if (_exiting || !signaled) continue;
                Dispatcher.BeginInvoke(() => ShowHud("second-instance"));
            }
        }) { IsBackground = true };
        _showListener.Start();
    }

    private void InitTray()
    {
        var bmp = new Bitmap(16, 16);
        using (var g = Graphics.FromImage(bmp))
        {
            g.Clear(System.Drawing.Color.Transparent);
            using var brush = new SolidBrush(System.Drawing.Color.FromArgb(165, 216, 234));
            g.FillEllipse(brush, 2, 2, 12, 12);
        }
        var menu = new WF.ContextMenuStrip();
        menu.Items.Add("呼出 / 收起", null, (_, _) => Dispatcher.Invoke(() => ToggleHud("tray")));
        menu.Items.Add("退出", null, (_, _) => Dispatcher.Invoke(Shutdown));
        _tray = new WF.NotifyIcon
        {
            Icon = System.Drawing.Icon.FromHandle(bmp.GetHicon()),
            Text = $"Agent Deck HUD ({_hotkeyLabel})",
            Visible = true,
            ContextMenuStrip = menu,
        };
        _tray.DoubleClick += (_, _) => Dispatcher.BeginInvoke(() => ToggleHud("tray"));
    }

    private async Task InitWebViewAsync()
    {
        try
        {
            var userData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "AgentDeckNativeHud", "WebView2");
            var envOptions = new CoreWebView2EnvironmentOptions();
            string? dbgPort = ResolveArgRaw(_args, "--remote-debugging-port");
            if (dbgPort is not null)
            {
                // 仅诊断/验收（cdp.mjs）：显式参数才启用，默认关闭
                envOptions.AdditionalBrowserArguments = $"--remote-debugging-port={dbgPort}";
                Log.Write($"remote debugging on port {dbgPort}");
            }
            var env = await CoreWebView2Environment.CreateAsync(null, userData, envOptions);
            await _webView.EnsureCoreWebView2Async(env);
            var core = _webView.CoreWebView2;

            _webView.DefaultBackgroundColor = System.Drawing.Color.Transparent;
            core.SetVirtualHostNameToFolderMapping(VirtualHost, _uiDir,
                CoreWebView2HostResourceAccessKind.DenyCors);

            var s = core.Settings;
            s.AreDefaultContextMenusEnabled = false;
            s.IsStatusBarEnabled = false;
            s.AreDefaultScriptDialogsEnabled = false;

            core.NavigationStarting += (_, ev) =>
            {
                if (!Uri.TryCreate(ev.Uri, UriKind.Absolute, out var uri) || uri.Host != VirtualHost)
                {
                    Log.Write($"navigation blocked: {ev.Uri}");
                    ev.Cancel = true;
                }
            };
            core.NewWindowRequested += (_, ev) =>
            {
                Log.Write($"new window blocked: {ev.Uri}");
                ev.Handled = true;
            };
            core.WebMessageReceived += OnWebMessage;
            core.WebResourceResponseReceived += (_, ev) =>
            {
                if (ev.Response.StatusCode >= 400)
                    Log.Write($"resource {ev.Request.Uri} -> {ev.Response.StatusCode}");
            };

            var url = $"https://{VirtualHost}/{_entryPage}";
            Log.Write($"navigate {url}");
            core.Navigate(url);
        }
        catch (Exception ex)
        {
            Log.Write($"webview init failed: {ex}");
            System.Windows.MessageBox.Show(
                "WebView2 初始化失败：" + ex.Message, "Agent Deck HUD",
                MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown();
        }
    }

    // ---------- 呼出 / 收起 ----------

    private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (msg == Win32.WM_HOTKEY && wParam.ToInt32() == HotkeyId)
        {
            ToggleHud("hotkey");
            handled = true;
        }
        return IntPtr.Zero;
    }

    internal void ToggleHud(string origin)
    {
        if (IsVisible) HideHud(origin); else ShowHud(origin);
    }

    internal void ShowHud(string origin)
    {
        if (_exiting) return;
        _previousForeground = Win32.GetForegroundWindow();
        PositionOnActiveScreen();
        Show();
        ApplyPanelBounds();
        Activate();
        _webView.Focus();
        Log.Write($"show origin={origin} prevFg=0x{_previousForeground:X} panel={_panelXPhys},{_panelYPhys} {_panelWidthPhys}x{ReportedPanelHeightPhys()}");
        // 面板可用高度随目标屏变化，页面据此约束内容滚动区
        PostToPage(new { type = "bounds", maxPanelHeight = MaxPanelHeightCss() });
        PostToPage(new { type = "visibility", visible = true });
        // F08：呼出立即发布新代次重置——页面马上废弃旧材料与旧身份，
        // 采集完成前允许纯文字发送；探测失败时等待也不会无限延续旧现场
        var round = NewSceneRound("show");
        PostToPage(new { type = "scene-round", scene_id = round.id, generation = round.gen });
        _hubTimer?.Start();
        _ = PollHubAsync();
        // M2：召唤即探测——对召唤前的前台窗口做一次 L1 内容拉取
        if (_scene is not null && _previousForeground != IntPtr.Zero)
            _ = SendSceneProbeAsync(_previousForeground, round.id, round.gen);
        // M7：剪贴板信号（仅文件列表+格式标志；文本内容不出境）
        if (_scene is not null)
            _ = SendClipboardAsync(round.id, round.gen);
    }

    // ---------- 现场观测代次（票02） ----------

    private (string id, long gen) CurrentSceneRound()
    {
        lock (_sceneGate) return (_sceneId, _sceneGeneration);
    }

    private (string id, long gen) NewSceneRound(string reason)
    {
        lock (_sceneGate) return NewSceneRoundLocked(reason);
    }

    private (string id, long gen) NewSceneRoundLocked(string reason)
    {
        _sceneGeneration++;
        _sceneId = $"scene-{_sceneGeneration}-{Environment.TickCount64:x}";
        Log.Write($"scene round gen={_sceneGeneration} reason={reason}");
        return (_sceneId, _sceneGeneration);
    }

    /// 身份相等不含 Ts（事件每次都带新时间戳，值比较会永远"变化"）。
    private static bool SceneIdentityEquals(SceneInfo? a, SceneInfo b) =>
        a is not null && a.Pid == b.Pid && a.Title == b.Title &&
        a.WinClass == b.WinClass && a.Exe == b.Exe;

    // M7：剪贴板快照；新鲜度门槛——距最近剪贴板事件 >120s 视为非现场，不出材料
    private async Task SendClipboardAsync(string sceneId, long gen)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            var snap = await _scene!.ClipboardAsync(cts.Token);
            if (snap.AgeS is double age && age > 120)
            {
                Log.Write($"clipboard skipped: stale age={age}s");
                return;
            }
            if (snap.Files.Count == 0 && !snap.HasText) return;
            PostToPage(new
            {
                type = "scene-clipboard",
                scene_id = sceneId,
                generation = gen,
                files = snap.Files,
                hasText = snap.HasText,
                hasHtml = snap.HasHtml,
            });
            Log.Write($"clipboard snapshot: files={snap.Files.Count} text={snap.HasText} age={(snap.AgeS?.ToString() ?? "?")}");
        }
        catch (OperationCanceledException)
        {
            Log.Write("clipboard snapshot timeout");
        }
        catch (Exception ex)
        {
            Log.Write("clipboard snapshot failed: " + ex.Message);
        }
    }

    internal void HideHud(string origin)
    {
        Log.Write($"hide origin={origin} visible={IsVisible}");
        _hubTimer?.Stop();
        if (!IsVisible) return;
        PostToPage(new { type = "visibility", visible = false });
        InvalidateSearchMap("hide"); // 隐藏即失效宿主侧动作映射（F02）
        Hide();
        // 恢复之前应用焦点；仅当原前台窗口仍然存在时
        if (_previousForeground != IntPtr.Zero && Win32.IsWindow(_previousForeground))
        {
            Win32.SetForegroundWindow(_previousForeground);
        }
    }

    private void PositionOnActiveScreen()
    {
        var anchor = _previousForeground != IntPtr.Zero && Win32.IsWindow(_previousForeground)
            ? _previousForeground
            : _hwnd;
        WF.Screen screen;
        if (_forcePrimaryScreen)
        {
            screen = WF.Screen.PrimaryScreen!;
            // 固定内屏时目标 DPI 用本窗（已在内屏）
            _targetScale = Win32.GetDpiForWindow(_hwnd) / 96.0;
        }
        else
        {
            try { screen = WF.Screen.FromHandle(anchor); }
            catch { screen = WF.Screen.PrimaryScreen!; }
            _targetScale = Win32.GetDpiForWindow(anchor) / 96.0;
        }
        var area = screen.WorkingArea; // 物理像素
        if (_targetScale <= 0) _targetScale = 1;
        _workAreaPhys = area;
        // 宿主决定面板位置：目标屏工作区顶部居中，宽 730（窄屏收缩）；
        // 高度由页面内容决定（layout 消息），此处只给上限
        int margin = (int)Math.Round(20 * _targetScale);
        _panelWidthPhys = Math.Min((int)Math.Round(730 * _targetScale), area.Width - 2 * margin);
        int topOffset = (int)Math.Clamp(area.Height * 0.21, 92 * _targetScale, 230 * _targetScale);
        _panelXPhys = area.Left + (area.Width - _panelWidthPhys) / 2;
        _panelYPhys = area.Top + topOffset;
        _maxPanelHeightPhys = Math.Max((int)Math.Round(160 * _targetScale), area.Height - topOffset - margin);
    }

    // ---------- 桥接 ----------

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            DispatchWebMessage(e.WebMessageAsJson);
        }
        catch (Exception ex)
        {
            // 入口兜底：任何单条消息异常不得拖垮桥接或宿主
            Log.Write("bridge handler error: " + ex.Message);
        }
    }

    private void DispatchWebMessage(string json)
    {
        JsonDocument doc;
        try { doc = JsonDocument.Parse(json); }
        catch { Log.Write("bridge: malformed json ignored"); return; }
        using (doc)
        {
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object ||
                !root.TryGetProperty("type", out var typeEl) || typeEl.ValueKind != JsonValueKind.String)
            {
                Log.Write("bridge: non-object or typeless message ignored");
                return;
            }
            switch (typeEl.GetString())
            {
                case "ready":
                    Log.Write("bridge: page ready");
                    PostToPage(new { type = "visibility", visible = true });
                    if (_hotkeyError is not null)
                        PostToPage(new { type = "hotkey-error", message = _hotkeyError });
                    _ = SendSceneSnapshotAsync();
                    break;
                case "log":
                    if (root.TryGetProperty("msg", out var msgEl) && msgEl.ValueKind == JsonValueKind.String)
                        Log.Write("page: " + Truncate(msgEl.GetString() ?? "", 300));
                    else
                        Log.Write("bridge: log with non-string msg ignored");
                    break;
                case "layout":
                    if (root.TryGetProperty("x", out var xEl) && xEl.ValueKind == JsonValueKind.Number && xEl.TryGetDouble(out var px) &&
                        root.TryGetProperty("y", out var yEl) && yEl.ValueKind == JsonValueKind.Number && yEl.TryGetDouble(out var py) &&
                        root.TryGetProperty("width", out var wEl) && wEl.ValueKind == JsonValueKind.Number && wEl.TryGetDouble(out var pw) &&
                        root.TryGetProperty("height", out var hEl) && hEl.ValueKind == JsonValueKind.Number && hEl.TryGetDouble(out var ph))
                        UpdatePanelRect(px, py, pw, ph);
                    else
                        Log.Write("bridge: layout with non-number rect ignored");
                    break;
                case "hide":
                    HideHud("page");
                    break;
                case "exit":
                    Log.Write("bridge: exit requested by page");
                    Shutdown();
                    break;
                case "search":
                    _ = HandleSearchAsync(root);
                    break;
                case "cancel-search":
                    InvalidateSearchMap("page-cancel");
                    break;
                case "submit-task":
                    _ = HandleHubSubmitAsync(root);
                    break;
                case "hub-permission":
                    _ = HandleHubPostAsync("permission", root);
                    break;
                case "hub-feedback":
                    _ = HandleHubFeedbackAsync(root);
                    break;
                case "today-complete":
                    _ = HandleTodayCompleteAsync(root);
                    break;
                case "hub-cancel":
                    _ = HandleHubPostAsync("cancel", root);
                    break;
                case "open":
                case "reveal":
                    HandleFileAction(typeEl.GetString()!, root);
                    break;
                default:
                    Log.Write($"bridge: unknown type '{typeEl.GetString()}' ignored");
                    break;
            }
        }
    }

    private static string Truncate(string s, int max) => s.Length <= max ? s : s[..max] + "…";

    private async Task SendSceneCaptureAsync(IntPtr hwnd, IReadOnlyList<ProbeMaterial>? l1Materials,
                                            string sceneId, long gen)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(12));
            var cap = await _scene!.CaptureAsync(hwnd.ToInt64(), l1Materials, cts.Token);
            if (cap.Material is not { } m)
            {
                Log.Write($"scene capture none: {cap.Note}");
                return;
            }
            PostToPage(new
            {
                type = "scene-capture",
                scene_id = sceneId,
                generation = gen,
                material = new { kind = m.Kind, source = m.Source, chars = m.Chars,
                                 preview = m.Preview, text = m.Text },
                note = cap.Note,
            });
            // 隐私边界：只记计数不记内容（对齐 adapter 日志纪律）
            Log.Write($"scene capture ok kind={m.Kind} chars={m.Chars}");
        }
        catch (OperationCanceledException)
        {
            Log.Write("scene capture timeout");
        }
        catch (Exception ex)
        {
            Log.Write("scene capture failed: " + ex.Message);
        }
    }

    // ---------- 任务中枢（M5：server.py /copilot/hud* 真通道） ----------

    private async Task PollHubAsync()
    {
        if (_hub is null || !IsVisible) return;
        var (status, body) = await _hub.GetAsync("/copilot/hud", CopilotHubClient.LoadDefaultToken());
        if (status == 200)
        {
            if (!_hubEverOk)
            {
                _hubEverOk = true;
                Log.Write("hub connected: " + BaseUrlSafe());
            }
            PostToPage(new { type = "hub-update", json = body });
        }
        else
            PostToPage(new { type = "hub-unavailable", reason = "HTTP " + status + " " + Truncate(body, 120) });
    }

    private string BaseUrlSafe() => _hub?.BaseUrl ?? "";

    private async Task HandleHubSubmitAsync(JsonElement root)
    {
        if (_hub is null) return;
        string text = root.TryGetProperty("text", out var t) && t.ValueKind == JsonValueKind.String
            ? t.GetString() ?? "" : "";
        if (text.Length == 0 || text.Length > 8000)
        {
            PostToPage(new { type = "hub-submit-result", status = 400, json = "{\"error\":{\"code\":\"bad_text\"}}" });
            return;
        }
        // 隐私边界：不落 prompt/材料内容，只记长度与场景计数（对齐日志纪律）
        int materialCount = 0;
        if (root.TryGetProperty("scene", out var scene) && scene.ValueKind == JsonValueKind.Object &&
            scene.TryGetProperty("materials", out var mats) && mats.ValueKind == JsonValueKind.Array)
            materialCount = mats.GetArrayLength();
        JsonElement? sceneArg = null;
        if (root.TryGetProperty("scene", out var scEl) && scEl.ValueKind == JsonValueKind.Object)
            sceneArg = scEl;
        // B03/R03：结构化引用（context_refs）、请求身份（request_id）与目录字段（workspace）
        // 原样贯穿宿主转发——存在性校验由服务端 controller 执行，宿主只透传不解释
        JsonElement? contextRefsArg = null;
        if (root.TryGetProperty("context_refs", out var crEl) && crEl.ValueKind == JsonValueKind.Array)
            contextRefsArg = crEl;
        string? requestId = root.TryGetProperty("request_id", out var ridEl) && ridEl.ValueKind == JsonValueKind.String
            ? ridEl.GetString() : null;
        string? workspace = root.TryGetProperty("workspace", out var wsEl) && wsEl.ValueKind == JsonValueKind.String
            ? wsEl.GetString() : null;
        var (status, body) = await _hub.PostAsync("/copilot/hud/submit", JsonSerializer.Serialize(new
        {
            text,
            scene = sceneArg,
            conversation_id = root.TryGetProperty("conversation_id", out var ci) && ci.ValueKind == JsonValueKind.String
                ? ci.GetString() : null,
            request_id = requestId,
            context_refs = contextRefsArg,
            workspace,
        }));
        Log.Write($"hub submit: text_len={text.Length} materials={materialCount} -> {status}");
        PostToPage(new { type = "hub-submit-result", status, json = body });
        _ = PollHubAsync();
    }

    private async Task HandleHubFeedbackAsync(JsonElement root)
    {
        if (_hub is null) return;
        var (status, body) = await _hub.PostAsync("/copilot/hud/feedback",
            JsonSerializer.Serialize(new
            {
                run_id = root.TryGetProperty("run_id", out var r) ? r.GetString() : null,
                verdict = root.TryGetProperty("verdict", out var v) ? v.GetString() : null,
            }));
        // 隐私边界：只记状态不记反馈内容
        Log.Write($"hub feedback -> {status}");
        PostToPage(new { type = "hub-action-result", action = "feedback", status, json = body });
    }

    // 今日待办勾选写回（滴答 MCP，fused-desktop 票 07）；完成刷新随下一次 PollHub 到达
    private async Task HandleTodayCompleteAsync(JsonElement root)
    {
        if (_hub is null) return;
        var (status, body) = await _hub.PostAsync("/copilot/hud/today/complete",
            JsonSerializer.Serialize(new
            {
                task_id = root.TryGetProperty("task_id", out var ti) ? ti.GetString() : null,
                project_id = root.TryGetProperty("project_id", out var pi) ? pi.GetString() : null,
            }));
        Log.Write($"today complete -> {status}");
        PostToPage(new { type = "today-complete-result", status, json = body });
        _ = PollHubAsync();
    }

    private async Task HandleHubPostAsync(string action, JsonElement root)
    {
        if (_hub is null) return;
        var (status, body) = await _hub.PostAsync("/copilot/hud/" + action,
            JsonSerializer.Serialize(new
            {
                permission_id = root.TryGetProperty("permission_id", out var pi) ? pi.GetString() : null,
                option_id = root.TryGetProperty("option_id", out var oi) ? oi.GetString() : null,
                run_id = root.TryGetProperty("run_id", out var ri) ? ri.GetString() : null,
            }));
        Log.Write($"hub {action} -> {status}");
        PostToPage(new { type = "hub-action-result", action, status, json = body });
        _ = PollHubAsync();
    }

    // ---------- 现场感知 L0（M1） ----------    // ---------- 现场感知 L0（M1） ----------

    /// <summary>HUD 自身成为前台不算"现场"——保持召唤前的真实现场。</summary>
    private static bool IsSelfScene(SceneInfo s) =>
        s.Pid == Environment.ProcessId ||
        s.Process.Contains("NativeHud", StringComparison.OrdinalIgnoreCase);

    private void OnSceneUpdate(SceneInfo s)
    {
        if (IsSelfScene(s)) return;
        // 票02：前台目标变化（pid/标题/类/exe）即新观测代次；同窗口重复通知不换轮
        var round = CurrentSceneRound();
        lock (_sceneGate)
        {
            if (!SceneIdentityEquals(_lastIdentity, s))
            {
                _lastIdentity = s;
                round = NewSceneRoundLocked("foreground-change");
            }
        }
        PostToPage(new
        {
            type = "scene-update",
            scene_id = round.id,
            generation = round.gen,
            current = new { pid = s.Pid, process = s.Process, exe = s.Exe,
                            title = s.Title, @class = s.WinClass, ts = s.Ts },
        });
    }

    private async Task SendSceneSnapshotAsync()
    {
        if (_scene is null) return;
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var snap = await _scene.SnapshotAsync(cts.Token);
            if (snap.Current is { } cur && !IsSelfScene(cur))
                OnSceneUpdate(cur);
        }
        catch (Exception ex)
        {
            Log.Write("scene snapshot failed: " + ex.Message);
        }
    }

    private void PostProbeToPage(SceneProbe probe, string sceneId, long gen)
    {
        // F07：迟到探测结果绝不改写当前权威身份，也绝不凭身份相等提升代次——
        // 仅当探测所属代次仍是当前代次时，其身份才算最新知识；否则结果连同
        // 旧代次一起由页面按 scene_id 丢弃
        if (probe.Identity is { } id && !IsSelfScene(id))
        {
            lock (_sceneGate)
            {
                if (sceneId == _sceneId)
                {
                    _lastIdentity = id; // 本轮未被取代：探测身份即当前权威
                }
            }
            PostToPage(new
            {
                type = "scene-update",
                scene_id = sceneId,
                generation = gen,
                current = new { pid = id.Pid, process = id.Process, exe = id.Exe,
                                title = id.Title, @class = id.WinClass, ts = id.Ts },
            });
        }
        PostToPage(new
        {
            type = "scene-probe",
            scene_id = sceneId,
            generation = gen,
            scenario = probe.Scenario,
            tier = probe.Tier,
            identity = probe.Identity is { } i2 ? new
            {
                pid = i2.Pid, process = i2.Process, exe = i2.Exe,
                title = i2.Title, @class = i2.WinClass, ts = i2.Ts
            } : null,
            materials = probe.Materials.Select(m => new
            {
                kind = m.Kind, source = m.Source, chars = m.Chars,
                preview = m.Preview, text = m.Text
            }).ToArray(),
            coverage = probe.Coverage,
            note = probe.Note,
        });
        Log.Write($"scene probe ok gen={gen} tier={probe.Tier} scenario={probe.Scenario} materials={probe.Materials.Count} coverage={probe.Coverage}");
    }

    // M6 感知分档：fast（跳过 UIA，<50ms 现场行即时出现）→ 覆盖不足升级 full（UIA 拉取）
    // → 仍不足才 L2 capture（携带 full 材料做融合去重）。逐级付出成本。
    // fast/full 同属一个观测代次，页面按材料标识合并去重（票02 R05）。
    private async Task SendSceneProbeAsync(IntPtr hwnd, string sceneId, long gen)
    {
        try
        {
            using var fastCts = new CancellationTokenSource(TimeSpan.FromSeconds(2.5));
            var fast = await _scene!.ProbeAsync(hwnd.ToInt64(), fastCts.Token, "fast");
            if (fast.Status != "ok")
            {
                Log.Write($"scene probe not ok: {fast.Status} {fast.Note}");
                return;
            }
            PostProbeToPage(fast, sceneId, gen);
            if (fast.Coverage == "full") return; // 快档已齐（典型：经典弹窗），不做 UIA

            using var fullCts = new CancellationTokenSource(TimeSpan.FromSeconds(2.5));
            var probe = await _scene.ProbeAsync(hwnd.ToInt64(), fullCts.Token, "full");
            if (probe.Status != "ok")
            {
                Log.Write($"scene probe not ok: {probe.Status} {probe.Note}");
                return;
            }
            PostProbeToPage(probe, sceneId, gen);
            // M4：覆盖不全（generic 恒然；partial 视缺口）自动 L2 兜底——截窗+OCR 仅文本
            if (probe.Coverage != "full")
                _ = SendSceneCaptureAsync(hwnd, probe.Materials, sceneId, gen);
        }
        catch (OperationCanceledException)
        {
            Log.Write("scene probe timeout (window unresponsive?)");
        }
        catch (Exception ex)
        {
            Log.Write("scene probe failed: " + ex.Message);
        }
    }

    // ---------- 搜索 ----------

    private async Task HandleSearchAsync(JsonElement root)
    {
        long requestId = root.TryGetProperty("requestId", out var idEl) &&
                         idEl.ValueKind == JsonValueKind.Number && idEl.TryGetInt64(out var id) ? id : -1;
        string query = root.TryGetProperty("query", out var qEl) && qEl.ValueKind == JsonValueKind.String
            ? qEl.GetString() ?? "" : "";
        if (requestId < 0 || query.Length == 0 || query.Length > 256)
        {
            Log.Write($"search: bad request rejected (id={requestId}, q_len={query.Length})");
            PostToPage(new { type = "search-result", requestId, status = "error", total = 0,
                items = Array.Empty<object>(), error = "bad_request" });
            return;
        }
        // 新查询意图即失效旧动作映射（F01/F02）；仅最新代次的响应可重建映射
        _latestRequestId = requestId;
        lock (_resultGate) _resultPaths.Clear();
        try
        {
            if (_search is null)
            {
                PostToPage(new { type = "search-result", requestId, status = "unavailable", total = 0,
                    items = Array.Empty<object>(), error = "adapter_unavailable", engine = "" });
                return;
            }
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            var resp = await _search.SearchAsync(query, 8, 0, timeout.Token);
            // 发放结果标识：页面之后只能凭标识请求打开/定位
            var items = new List<object>();
            var newMap = new Dictionary<string, string>();
            int n = 0;
            foreach (var item in resp.Items)
            {
                string token = $"r{requestId}_{n++}";
                newMap[token] = item.Path;
                items.Add(new
                {
                    id = token,
                    name = item.Name,
                    path = item.Path,
                    kind = item.Kind,
                    sizeBytes = item.SizeBytes,
                    modifiedAt = item.ModifiedAt,
                });
            }
            if (requestId == _latestRequestId)
            {
                lock (_resultGate)
                {
                    _resultPaths.Clear();
                    foreach (var kv in newMap) _resultPaths[kv.Key] = kv.Value;
                }
            }
            else
            {
                // 旧代次迟到：页面会丢弃显示，宿主侧也绝不替换当前动作映射（F02）
                Log.Write($"search req={requestId} stale, map kept for req={_latestRequestId}");
            }
            Log.Write($"search q_len={query.Length} req={requestId} status={resp.Status} total={resp.Total}");
            PostToPage(new
            {
                type = "search-result",
                requestId,
                status = resp.Status,
                total = resp.Total,
                items,
                error = resp.Error,
                engine = resp.Engine,
            });
        }
        catch (OperationCanceledException)
        {
            Log.Write($"search req={requestId} timeout");
            PostToPage(new { type = "search-result", requestId, status = "error", total = 0,
                items = Array.Empty<object>(), error = "timeout" });
        }
        catch (Exception ex)
        {
            Log.Write($"search req={requestId} failed: {ex.Message}");
            PostToPage(new { type = "search-result", requestId, status = "unavailable", total = 0,
                items = Array.Empty<object>(), error = "adapter" });
        }
    }

    // ---------- 面板几何与背景材质 ----------

    private void InvalidateSearchMap(string origin)
    {
        _latestRequestId = -1;
        lock (_resultGate) _resultPaths.Clear();
        Log.Write($"search map invalidated ({origin})");
    }

    /// <summary>页面上报的面板自然尺寸（CSS px）。窗口即面板：位置/宽度由宿主决定，仅取高度驱动窗口伸缩。</summary>
    private void UpdatePanelRect(double x, double y, double width, double height)
    {
        if (height <= 0 || height > 3000 || _workAreaPhys.Width <= 0 || !IsVisible) return;
        if (Math.Abs(height - _lastPanelHeightCss) < 0.5) return;
        _lastPanelHeightCss = height;
        ApplyPanelBounds();
    }

    private double MaxPanelHeightCss() => _maxPanelHeightPhys / _targetScale;

    /// <summary>页面尚未上报高度时的首帧临时高度（layout 消息到达后修正）。</summary>
    private int ReportedPanelHeightPhys()
    {
        if (_lastPanelHeightCss <= 0) return (int)Math.Round(200 * _targetScale);
        int h = (int)Math.Round(_lastPanelHeightCss * _targetScale);
        return Math.Clamp(h, (int)Math.Round(120 * _targetScale), _maxPanelHeightPhys);
    }

    private void ApplyPanelBounds()
    {
        if (_hwnd == IntPtr.Zero) return;
        int w = _panelWidthPhys, h = ReportedPanelHeightPhys();
        // WPF Left/Top 按窗口当前 DPI 换算，跨屏（如 150%/200%）直接设 DIP 会错位，
        // 故统一物理像素 + SetWindowPos 定位
        Win32.SetWindowPos(_hwnd, IntPtr.Zero, _panelXPhys, _panelYPhys, w, h, Win32.SWP_NOACTIVATE);
        if (_useRegionCorners)
        {
            int rPx = (int)Math.Round(8 * _targetScale); // 与页面 8px 圆角对齐（设备像素）
            IntPtr rgn = Win32.CreateRoundRectRgn(0, 0, w + 1, h + 1, rPx * 2, rPx * 2);
            if (rgn != IntPtr.Zero) Win32.SetWindowRgn(_hwnd, rgn, true);
        }
    }

    // ---------- 窗口材质（DWM 亚克力 + 圆角） ----------

    private void EnableAcrylic()
    {
        try
        {
            var margins = new Win32.MARGINS(-1, -1, -1, -1);
            Win32.DwmExtendFrameIntoClientArea(_hwnd, ref margins);
            var policy = new Win32.ACCENT_POLICY
            {
                AccentState = Win32.ACCENT_ENABLE_ACRYLICBLURBEHIND,
                AccentFlags = 0,
                // ABGR：贴近面板基色的暗青灰 tint；系统关闭透明效果时 DWM 自动退为纯色
                GradientColor = unchecked((int)0x8C2C2011),
                AnimationId = 0,
            };
            int size = Marshal.SizeOf(policy);
            IntPtr ptr = Marshal.AllocHGlobal(size);
            Marshal.StructureToPtr(policy, ptr, false);
            var data = new Win32.WINDOWCOMPOSITIONATTRIBDATA
            {
                Attribute = Win32.WCA_ACCENT_POLICY,
                Data = ptr,
                SizeOfData = size,
            };
            int hr = Win32.SetWindowCompositionAttribute(_hwnd, ref data);
            Marshal.FreeHGlobal(ptr);
            // 该未公开 API 返回 BOOL 语义：非 0 为成功
            if (hr == 0) throw new InvalidOperationException("SetWindowCompositionAttribute returned 0");
            Log.Write("acrylic material active on main window");
        }
        catch (Exception ex)
        {
            // 降级：可读深色实底，不声称模糊
            Background = new System.Windows.Media.SolidColorBrush(
                System.Windows.Media.Color.FromArgb(242, 17, 32, 44));
            Log.Write("acrylic unavailable, solid fallback: " + ex.Message);
        }
    }

    private void TryRoundCorners()
    {
        // Win11 圆角交给 DWM；老系统无该属性时退回区域圆角（随尺寸变化重设）
        int pref = Win32.DWMWCP_ROUND;
        int hr = Win32.DwmSetWindowAttribute(_hwnd, Win32.DWMWA_WINDOW_CORNER_PREFERENCE, ref pref, sizeof(int));
        _useRegionCorners = hr != 0;
        Log.Write($"corner preference dwm={(hr == 0 ? "round" : "unavailable, region fallback")}");
    }

    // ---------- 文件动作 ----------

    private void HandleFileAction(string action, JsonElement root)
    {
        if (!root.TryGetProperty("id", out var idEl) || idEl.ValueKind != JsonValueKind.String)
        {
            Log.Write($"action {action}: missing/non-string id rejected");
            return;
        }
        string token = idEl.GetString() ?? "";
        string? path;
        lock (_resultGate)
        {
            _resultPaths.TryGetValue(token, out path);
        }
        if (path is null)
        {
            Log.Write($"action {action}: unknown result id '{token}' rejected");
            PostToPage(new { type = "action-result", id = token, action, ok = false, error = "stale_or_unknown" });
            return;
        }
        bool exists = File.Exists(path) || Directory.Exists(path);
        if (!exists)
        {
            Log.Write($"action {action}: target missing");
            PostToPage(new { type = "action-result", id = token, action, ok = false, error = "not_found" });
            return;
        }
        try
        {
            if (action == "open")
            {
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            }
            else
            {
                // explorer 要求 /select, 开关在引号外；Windows 路径本身不含引号字符
                Process.Start(new ProcessStartInfo("explorer.exe", "/select,\"" + path + "\""));
            }
            Log.Write($"action {action}: ok (id={token})");
            PostToPage(new { type = "action-result", id = token, action, ok = true, error = (string?)null });
        }
        catch (Exception ex)
        {
            Log.Write($"action {action}: failed: {ex.Message}");
            PostToPage(new { type = "action-result", id = token, action, ok = false, error = "launch_failed" });
        }
    }

    private void PostToPage(object message)
    {
        try
        {
            _webView.Dispatcher.BeginInvoke(() =>
            {
                try { _webView.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(message)); }
                catch (Exception ex) { Log.Write($"post failed: {ex.Message}"); }
            });
        }
        catch { /* 已退出 */ }
    }

    // ---------- 退出 ----------

    internal void Shutdown()
    {
        if (_exiting) return;
        _exiting = true;
        Log.Write("shutdown requested");
        Close();
    }

    private void OnClosed(object? sender, EventArgs e)
    {
        _exiting = true;
        if (_hotkeyRegistered) Win32.UnregisterHotKey(_hwnd, HotkeyId);
        _showEvent?.Set();
        _showEvent?.Dispose();
        if (_tray is not null) { _tray.Visible = false; _tray.Dispose(); }
        _search?.Dispose();
        _scene?.Dispose();
        _webView.Dispose();
        Log.Write("window closed; hotkey unregistered, tray removed, search/scene adapters stopped");
        System.Windows.Application.Current?.Shutdown();
    }
}
