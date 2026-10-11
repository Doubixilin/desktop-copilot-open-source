using System;
using System.Threading;
using System.Windows;

namespace NativeHud;

internal static class Program
{
    internal const string MutexName = @"Local\AgentDeck.NativeHud.Singleton";
    internal const string ShowEventName = @"Local\AgentDeck.NativeHud.Show";

    [STAThread]
    public static int Main(string[] args)
    {
        Log.Init(args);
        Log.Write($"pid={Environment.ProcessId} args=[{string.Join(" ", args)}]");

        using var mutex = new Mutex(true, MutexName, out bool created);
        if (!created)
        {
            try
            {
                using var ev = EventWaitHandle.OpenExisting(ShowEventName);
                ev.Set();
                Log.Write("second instance: signaled existing instance to show, exiting");
            }
            catch (Exception ex)
            {
                Log.Write($"second instance: signal failed: {ex.Message}");
            }
            return 0;
        }

        var app = new Application();
        var window = new HudWindow(args);
        app.Run(window);
        Log.Write("shutdown complete");
        return 0;
    }
}
