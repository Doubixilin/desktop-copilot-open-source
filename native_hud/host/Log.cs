using System;
using System.IO;

namespace NativeHud;

/// <summary>极简诊断日志：时间戳 + 单行文本，供原生验收留证。不写查询词以外的敏感内容。</summary>
internal static class Log
{
    private static readonly object Gate = new();
    private static string _path = System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "AgentDeckNativeHud", "host.log");

    public static string Path => _path;

    public static void Init(string[] args)
    {
        for (int i = 0; i + 1 < args.Length; i++)
        {
            if (args[i] == "--log") _path = args[i + 1];
        }
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(_path)!);
        Write("log init");
    }

    public static void Write(string message)
    {
        var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {message}";
        lock (Gate)
        {
            try { File.AppendAllText(_path, line + Environment.NewLine); } catch { }
        }
        try { Console.Error.WriteLine(line); } catch { }
    }
}
