using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace NativeHud;

internal sealed record SearchItem(string Path, string Name, string Kind, long SizeBytes, string ModifiedAt);
internal sealed record SearchOutcome(string Status, int Total, IReadOnlyList<SearchItem> Items, string? Error, string Engine);

/// <summary>
/// 宿主管理的 Python stdio 搜索适配：每请求一行 JSON，响应按 id 匹配。
/// 复用仓库 listary_engine 的引擎选择/解析/错误分类；宿主不另起 HTTP 服务。
/// </summary>
internal sealed class SearchClient : IDisposable
{
    private readonly Process _proc;
    private readonly StreamWriter _stdin;
    private readonly object _writeGate = new();
    private readonly ConcurrentDictionary<long, TaskCompletionSource<SearchOutcome>> _pending = new();
    private long _nextId;

    public SearchClient()
    {
        string adapterPath = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory, "..", "..", "..", "..", "search_adapter.py"));
        if (!File.Exists(adapterPath))
            throw new FileNotFoundException("search adapter not found", adapterPath);

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
            // 必须无 BOM：Encoding.UTF8 会在首行写入 EF BB BF，适配端 json.loads 直接失败
            StandardInputEncoding = new System.Text.UTF8Encoding(false),
        };
        psi.ArgumentList.Add("-B");
        psi.ArgumentList.Add("-u");
        psi.ArgumentList.Add(adapterPath);
        _proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start python");
        _stdin = _proc.StandardInput;
        Log.Write($"search adapter pid={_proc.Id} path={adapterPath}");

        var reader = new Thread(ReadLoop) { IsBackground = true };
        reader.Start();
        var errReader = new Thread(() =>
        {
            try
            {
                string? line;
                while ((line = _proc.StandardError.ReadLine()) is not null)
                    Log.Write("adapter stderr: " + line);
            }
            catch { }
        }) { IsBackground = true };
        errReader.Start();
    }

    public async Task<SearchOutcome> SearchAsync(string query, int limit, int offset, CancellationToken ct)
    {
        long id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<SearchOutcome>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;
        string line = JsonSerializer.Serialize(new { id, op = "search", query, limit, offset });
        try
        {
            lock (_writeGate)
            {
                _stdin.WriteLine(line);
                _stdin.Flush();
            }
            // 隐私边界：不记录查询词与结果路径（对齐 ADR-0002）
            Log.Write($"adapter -> req={id} q_len={query.Length}");
        }
        catch (Exception ex)
        {
            _pending.TryRemove(id, out _);
            return new SearchOutcome("unavailable", 0, Array.Empty<SearchItem>(), "adapter_write:" + ex.Message, "");
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
                Log.Write($"adapter <- ({line.Length} chars)");
                SearchOutcome outcome;
                long id = -1;
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    var root = doc.RootElement;
                    id = root.TryGetProperty("id", out var idEl) && idEl.TryGetInt64(out var v) ? v : -1;
                    outcome = ParseOutcome(root);
                }
                catch (Exception ex)
                {
                    Log.Write("adapter: malformed response line: " + ex.Message);
                    continue;
                }
                if (id >= 0 && _pending.TryRemove(id, out var tcs))
                    tcs.TrySetResult(outcome);
            }
        }
        catch (Exception ex)
        {
            Log.Write("adapter read loop ended: " + ex.Message);
        }
        foreach (var kv in _pending)
        {
            if (_pending.TryRemove(kv.Key, out var tcs))
                tcs.TrySetResult(new SearchOutcome("unavailable", 0, Array.Empty<SearchItem>(), "adapter_exit", ""));
        }
        Log.Write("adapter stdout closed");
    }

    private static SearchOutcome ParseOutcome(JsonElement root)
    {        string status = root.TryGetProperty("status", out var s) ? s.GetString() ?? "error" : "error";
        if (status is not ("ok" or "empty" or "unavailable" or "error")) status = "error";
        int total = root.TryGetProperty("total", out var t) && t.TryGetInt32(out var tv) ? tv : 0;
        string? error = root.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
        string engine = root.TryGetProperty("engine", out var en) && en.ValueKind == JsonValueKind.String ? en.GetString() ?? "" : "";
        var items = new List<SearchItem>();
        if (root.TryGetProperty("items", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var el in arr.EnumerateArray())
            {
                if (el.ValueKind != JsonValueKind.Object) continue;
                string path = el.TryGetProperty("path", out var p) ? p.GetString() ?? "" : "";
                if (path.Length == 0 || path.Length > 1024) continue;
                items.Add(new SearchItem(
                    path,
                    el.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
                    el.TryGetProperty("kind", out var k) ? k.GetString() ?? "file" : "file",
                    el.TryGetProperty("size_bytes", out var sz) && sz.TryGetInt64(out var szv) ? szv : 0,
                    el.TryGetProperty("modified_at", out var m) ? m.GetString() ?? "" : ""));
            }
        }
        return new SearchOutcome(status, total, items, error, engine);
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
            Log.Write("adapter dispose: " + ex.Message);
        }
        _proc.Dispose();
    }
}
