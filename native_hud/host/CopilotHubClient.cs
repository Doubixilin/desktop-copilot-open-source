using System;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;

namespace NativeHud;

/// <summary>
/// 任务中枢 HTTP 客户端（server.py /copilot/hud*，M5）。
/// 裸 JSON 进出——解析留给页面（CSP 锁定的自家园），宿主只关心状态码。
/// 令牌：--copilot-token 参数 > %LOCALAPPDATA%/qoder-deck/api-token（与 server 同源）。
/// </summary>
internal sealed class CopilotHubClient : IDisposable
{
    private readonly HttpClient _http;
    public readonly string BaseUrl;

    public CopilotHubClient(string baseUrl, string? token)
    {
        BaseUrl = baseUrl.TrimEnd('/');
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(4) };
        if (!string.IsNullOrEmpty(token))
            _http.DefaultRequestHeaders.Add("X-QD-Token", token);
    }

    public static string? LoadDefaultToken()
    {
        try
        {
            string path = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "qoder-deck", "api-token");
            var t = File.ReadAllText(path, Encoding.UTF8).Trim();
            return t.Length > 0 ? t : null;
        }
        catch { return null; }
    }

    /// <summary>GET；(状态码, 正文)。连接失败返回 (-1, 原因)。</summary>
    public async Task<(int, string)> GetAsync(string path, string? queryToken = null)
    {
        try
        {
            string url = BaseUrl + path + (string.IsNullOrEmpty(queryToken) ? "" : $"?token={Uri.EscapeDataString(queryToken)}");
            using var resp = await _http.GetAsync(url);
            return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync());
        }
        catch (Exception ex)
        {
            return (-1, ex.Message);
        }
    }

    /// <summary>POST JSON；(状态码, 正文)。</summary>
    public async Task<(int, string)> PostAsync(string path, string jsonBody)
    {
        try
        {
            using var content = new StringContent(jsonBody, Encoding.UTF8, "application/json");
            using var resp = await _http.PostAsync(BaseUrl + path, content);
            return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync());
        }
        catch (Exception ex)
        {
            return (-1, ex.Message);
        }
    }

    public void Dispose() => _http.Dispose();
}
