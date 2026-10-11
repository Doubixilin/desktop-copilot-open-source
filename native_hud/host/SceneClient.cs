using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace NativeHud;

internal sealed record SceneInfo(int Pid, string Process, string Exe, string Title, string WinClass, double Ts);
internal sealed record SceneSnapshot(SceneInfo? Current, IReadOnlyList<SceneInfo> Timeline);
internal sealed record ProbeLine(string Text, IReadOnlyList<double>? Box);
internal sealed record ProbeMaterial(string Kind, string Source, int Chars, string Preview, string Text,
    IReadOnlyList<ProbeLine> Lines);
internal sealed record SceneProbe(string Status, string Scenario, SceneInfo? Identity,
    IReadOnlyList<ProbeMaterial> Materials, string Coverage, string Note, string Tier);
internal sealed record CaptureResult(ProbeMaterial? Material, string Note);
internal sealed record ClipboardSnapshot(IReadOnlyList<string> Files, bool HasText, bool HasHtml,
    double? AgeS, string Note);

/// <summary>
/// 宿主管理的 Python stdio 现场感知适配（scene_adapter.py）。
/// 两类 stdout 行："event" 行 → OnUpdate 推送；带 id 的行 → snapshot 应答。
/// 启动失败抛异常，由 HudWindow 降级（现场行隐藏，不影响其余功能）。
/// </summary>
internal sealed class SceneClient : IDisposable
{
    private readonly Process _proc;
    private readonly StreamWriter _stdin;
    private readonly object _writeGate = new();
    private readonly ConcurrentDictionary<long, TaskCompletionSource<SceneSnapshot>> _pending = new();
    private readonly ConcurrentDictionary<long, TaskCompletionSource<SceneProbe>> _probes = new();
    private readonly ConcurrentDictionary<long, TaskCompletionSource<CaptureResult>> _captures = new();
    private readonly ConcurrentDictionary<long, TaskCompletionSource<ClipboardSnapshot>> _clips = new();
    private long _nextId;

    public event Action<SceneInfo>? OnUpdate;

    public SceneClient()
    {
        string adapterPath = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory, "..", "..", "..", "..", "scene_adapter.py"));
        if (!File.Exists(adapterPath))
            throw new FileNotFoundException("scene adapter not found", adapterPath);

        var psi = new ProcessStartInfo
        {
            FileName = "python",
            WorkingDirectory = Path.GetDirectoryName(adapterPath)!,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            // 必须无 BOM：与 SearchClient 同因（适配端 json.loads 拒绝 BOM 头）
            StandardInputEncoding = new System.Text.UTF8Encoding(false),
        };
        psi.ArgumentList.Add("-B");
        psi.ArgumentList.Add("-u");
        psi.ArgumentList.Add(adapterPath);
        _proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start python");
        _stdin = _proc.StandardInput;
        Log.Write($"scene adapter pid={_proc.Id} path={adapterPath}");

        var reader = new Thread(ReadLoop) { IsBackground = true };
        reader.Start();
        var errReader = new Thread(() =>
        {
            try
            {
                string? line;
                while ((line = _proc.StandardError.ReadLine()) is not null)
                    Log.Write("scene stderr: " + line);
            }
            catch { }
        }) { IsBackground = true };
        errReader.Start();
    }

    public async Task<SceneSnapshot> SnapshotAsync(CancellationToken ct)
    {
        long id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<SceneSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;
        string line = JsonSerializer.Serialize(new { id, op = "snapshot" });
        try
        {
            lock (_writeGate)
            {
                _stdin.WriteLine(line);
                _stdin.Flush();
            }
        }
        catch (Exception ex)
        {
            _pending.TryRemove(id, out _);
            return new SceneSnapshot(null, Array.Empty<SceneInfo>());
        }
        await using (ct.Register(() => tcs.TrySetCanceled(ct)))
        {
            return await tcs.Task;
        }
    }

    /// <summary>L1 内容探测：宿主传召唤前的前台窗口（prevFg）。2.5s 超时由调用方注入。
    /// tier=fast 跳过 UIA（M6 感知分档：身份+标题解析+Win32 弹窗枚举，&lt;50ms）。</summary>
    public async Task<SceneProbe> ProbeAsync(long hwnd, CancellationToken ct, string tier = "full")
    {
        long id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<SceneProbe>(TaskCreationOptions.RunContinuationsAsynchronously);
        _probes[id] = tcs;
        string line = JsonSerializer.Serialize(new { id, op = "probe", hwnd, tier });
        try
        {
            lock (_writeGate)
            {
                _stdin.WriteLine(line);
                _stdin.Flush();
            }
            Log.Write($"scene -> probe id={id} hwnd=0x{hwnd:X} tier={tier}");
        }
        catch (Exception ex)
        {
            _probes.TryRemove(id, out _);
            return new SceneProbe("error", "generic", null, Array.Empty<ProbeMaterial>(), "none", "adapter_write:" + ex.Message, tier);
        }
        await using (ct.Register(() => tcs.TrySetCanceled(ct)))
        {
            return await tcs.Task;
        }
    }

    /// <summary>L2 兜底：截窗+OCR（独立 worker 子进程，适配器侧保证）。超时由调用方注入。
    /// 携带 L1 材料时适配器侧先做融合去重（M6），重复内容不再出境。</summary>
    public async Task<CaptureResult> CaptureAsync(long hwnd, IReadOnlyList<ProbeMaterial>? materials, CancellationToken ct)
    {
        long id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<CaptureResult>(TaskCreationOptions.RunContinuationsAsynchronously);
        _captures[id] = tcs;
        object? mats = materials is null ? null : materials.Take(8).Select(m => new
        {
            kind = m.Kind,
            text = m.Text,
            lines = m.Lines.Count > 0 ? m.Lines.Take(120).Select(l => new { text = l.Text, box = l.Box }).ToArray() : null,
        }).ToArray();
        string line = JsonSerializer.Serialize(new { id, op = "capture", hwnd, materials = mats });
        try
        {
            lock (_writeGate)
            {
                _stdin.WriteLine(line);
                _stdin.Flush();
            }
            Log.Write($"scene -> capture id={id} hwnd=0x{hwnd:X} l1_materials={materials?.Count ?? 0}");
        }
        catch (Exception ex)
        {
            _captures.TryRemove(id, out _);
            return new CaptureResult(null, "adapter_write:" + ex.Message);
        }
        await using (ct.Register(() => tcs.TrySetCanceled(ct)))
        {
            return await tcs.Task;
        }
    }

    /// <summary>L3 剪贴板快照（M7）：文件列表+格式标志；文本内容适配器侧不读取出境。</summary>
    public async Task<ClipboardSnapshot> ClipboardAsync(CancellationToken ct)
    {
        long id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<ClipboardSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        _clips[id] = tcs;
        string line = JsonSerializer.Serialize(new { id, op = "clipboard" });
        try
        {
            lock (_writeGate)
            {
                _stdin.WriteLine(line);
                _stdin.Flush();
            }
        }
        catch (Exception ex)
        {
            _clips.TryRemove(id, out _);
            return new ClipboardSnapshot(Array.Empty<string>(), false, false, null, "adapter_write:" + ex.Message);
        }
        await using (ct.Register(() => tcs.TrySetCanceled(ct)))
        {
            return await tcs.Task;
        }
    }

    private void ReadLoop()
    {
        try
        {
            string? line;
            while ((line = _proc.StandardOutput.ReadLine()) is not null)
            {
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    var root = doc.RootElement;
                    if (root.TryGetProperty("event", out var ev) &&
                        ev.ValueKind == JsonValueKind.String && ev.GetString() == "foreground")
                    {
                        var cur = ParseInfo(root.TryGetProperty("current", out var c) ? c : default);
                        if (cur is not null)
                            OnUpdate?.Invoke(cur);
                        continue;
                    }
                    if (root.TryGetProperty("id", out var idEl) && idEl.TryGetInt64(out var id) && id >= 0)
                    {
                        var snap = ParseSnapshot(root);
                        if (_pending.TryRemove(id, out var tcs))
                            tcs.TrySetResult(snap);
                        if (_probes.TryRemove(id, out var pts))
                            pts.TrySetResult(ParseProbe(root));
                        if (_captures.TryRemove(id, out var ctsk))
                            ctsk.TrySetResult(ParseCapture(root));
                        if (_clips.TryRemove(id, out var cltsk))
                            cltsk.TrySetResult(ParseClipboard(root));
                    }
                }
                catch (Exception ex)
                {
                    Log.Write("scene: malformed line: " + ex.Message);
                }
            }
        }
        catch (Exception ex)
        {
            Log.Write("scene read loop ended: " + ex.Message);
        }
        foreach (var kv in _pending)
        {
            if (_pending.TryRemove(kv.Key, out var tcs))
                tcs.TrySetResult(new SceneSnapshot(null, Array.Empty<SceneInfo>()));
        }
        foreach (var kv in _probes)
        {
            if (_probes.TryRemove(kv.Key, out var pts))
                pts.TrySetResult(new SceneProbe("error", "generic", null, Array.Empty<ProbeMaterial>(), "none", "adapter_exit", "full"));
        }
        foreach (var kv in _captures)
        {
            if (_captures.TryRemove(kv.Key, out var ctsk))
                ctsk.TrySetResult(new CaptureResult(null, "adapter_exit"));
        }
        foreach (var kv in _clips)
        {
            if (_clips.TryRemove(kv.Key, out var cltsk))
                cltsk.TrySetResult(new ClipboardSnapshot(Array.Empty<string>(), false, false, null, "adapter_exit"));
        }
        Log.Write("scene adapter stdout closed");
    }

    private static SceneProbe ParseProbe(JsonElement root)
    {
        string status = root.TryGetProperty("status", out var st) ? st.GetString() ?? "error" : "error";
        string scenario = root.TryGetProperty("scenario", out var sc) ? sc.GetString() ?? "generic" : "generic";
        string coverage = root.TryGetProperty("coverage", out var cv) ? cv.GetString() ?? "none" : "none";
        string note = root.TryGetProperty("note", out var nt) ? nt.GetString() ?? "" : "";
        string tier = root.TryGetProperty("tier", out var tr) ? tr.GetString() ?? "full" : "full";
        var identity = root.TryGetProperty("identity", out var idEl) &&
                       idEl.ValueKind == JsonValueKind.Object ? ParseInfo(idEl) : null;
        var materials = new List<ProbeMaterial>();
        if (root.TryGetProperty("materials", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var el in arr.EnumerateArray())
            {
                if (el.ValueKind != JsonValueKind.Object) continue;
                var m = ParseMaterial(el);
                if (m is not null) materials.Add(m);
            }
        }
        return new SceneProbe(status, scenario, identity, materials, coverage, note, tier);
    }

    private static SceneInfo? ParseInfo(JsonElement el)
    {
        if (el.ValueKind is not JsonValueKind.Object && el.ValueKind is not JsonValueKind.Undefined)
            return null;
        if (el.ValueKind is JsonValueKind.Undefined || el.ValueKind is JsonValueKind.Null)
            return null;
        if (!el.TryGetProperty("pid", out var pidEl) || !pidEl.TryGetInt32(out var pid))
            return null;
        string title = el.TryGetProperty("title", out var t) ? t.GetString() ?? "" : "";
        if (title.Length == 0 || title.Length > 512) return null;
        return new SceneInfo(
            pid,
            el.TryGetProperty("process", out var p) ? p.GetString() ?? "" : "",
            el.TryGetProperty("exe", out var x) ? x.GetString() ?? "" : "",
            title,
            el.TryGetProperty("class", out var c) ? c.GetString() ?? "" : "",
            el.TryGetProperty("ts", out var ts) && ts.TryGetDouble(out var tsv) ? tsv : 0);
    }

    private static SceneSnapshot ParseSnapshot(JsonElement root)
    {
        var current = root.TryGetProperty("current", out var cur) ? ParseInfo(cur) : null;
        var timeline = new List<SceneInfo>();
        if (root.TryGetProperty("timeline", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var el in arr.EnumerateArray())
            {
                var info = ParseInfo(el);
                if (info is not null) timeline.Add(info);
            }
        }
        return new SceneSnapshot(current, timeline);
    }

    private static CaptureResult ParseCapture(JsonElement root)
    {
        string status = root.TryGetProperty("status", out var st) ? st.GetString() ?? "error" : "error";
        string note = root.TryGetProperty("note", out var nt) ? nt.GetString() ?? "" : "";
        if (status != "ok")
            return new CaptureResult(null, note);
        ProbeMaterial? material = null;
        if (root.TryGetProperty("material", out var mEl) && mEl.ValueKind == JsonValueKind.Object)
        {
            var m = ParseMaterial(mEl);
            if (m is not null && m.Chars > 0)
                material = m;
        }
        return new CaptureResult(material, note);
    }

    private static ClipboardSnapshot ParseClipboard(JsonElement root)
    {
        string note = root.TryGetProperty("note", out var nt) ? nt.GetString() ?? "" : "";
        if (!root.TryGetProperty("clipboard", out var c) || c.ValueKind != JsonValueKind.Object)
            return new ClipboardSnapshot(Array.Empty<string>(), false, false, null, note);
        var files = new List<string>();
        if (c.TryGetProperty("files", out var f) && f.ValueKind == JsonValueKind.Array)
        {
            foreach (var el in f.EnumerateArray())
            {
                var s = el.GetString();
                if (!string.IsNullOrEmpty(s) && files.Count < 20) files.Add(s);
            }
        }
        bool hasText = c.TryGetProperty("has_text", out var ht) && ht.ValueKind == JsonValueKind.True;
        bool hasHtml = c.TryGetProperty("has_html", out var hh) && hh.ValueKind == JsonValueKind.True;
        double? age = c.TryGetProperty("age_s", out var ag) &&
                      ag.ValueKind == JsonValueKind.Number && ag.TryGetDouble(out var agv) ? agv : null;
        return new ClipboardSnapshot(files, hasText, hasHtml, age, note);
    }

    private static ProbeMaterial? ParseMaterial(JsonElement el)
    {
        string kind = el.TryGetProperty("kind", out var k) ? k.GetString() ?? "" : "";
        if (kind.Length == 0 || kind.Length > 40) return null;
        var lines = new List<ProbeLine>();
        if (el.TryGetProperty("lines", out var lnArr) && lnArr.ValueKind == JsonValueKind.Array)
        {
            foreach (var ln in lnArr.EnumerateArray())
            {
                if (lines.Count >= 120 || ln.ValueKind != JsonValueKind.Object) continue;
                string ltext = ln.TryGetProperty("text", out var lt) ? lt.GetString() ?? "" : "";
                if (ltext.Length == 0 || ltext.Length > 200) continue;
                double[]? lbox = null;
                if (ln.TryGetProperty("box", out var bx) && bx.ValueKind == JsonValueKind.Array &&
                    bx.GetArrayLength() == 4)
                {
                    var vals = bx.EnumerateArray().Select(v => v.TryGetDouble(out var d) ? d : 0).ToArray();
                    lbox = vals;
                }
                lines.Add(new ProbeLine(ltext, lbox));
            }
        }
        return new ProbeMaterial(
            kind,
            el.TryGetProperty("source", out var src) ? src.GetString() ?? "" : "",
            el.TryGetProperty("chars", out var ch) && ch.TryGetInt32(out var chv) ? chv : 0,
            el.TryGetProperty("preview", out var pv) ? pv.GetString() ?? "" : "",
            el.TryGetProperty("text", out var tx) ? (tx.GetString() ?? "").Length <= 2100 ? tx.GetString() ?? "" : "" : "",
            lines);
    }

    public void Dispose()
    {
        try
        {
            if (!_proc.HasExited)
            {
                try { _stdin.Close(); } catch { }
                if (!_proc.WaitForExit(3000)) _proc.Kill(entireProcessTree: true);
            }
        }
        catch (Exception ex)
        {
            Log.Write("scene dispose: " + ex.Message);
        }
        _proc.Dispose();
    }
}
