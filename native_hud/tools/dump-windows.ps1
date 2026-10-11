# R2-01/02 实机几何核对：页面面板矩形 vs 背板窗口矩形（内屏 200%，物理像素）。
# 用法：HUD 以 --screen primary + CDP 9223 运行时执行。
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
public class WinEnum {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    public struct RECT { public int L, T, R, B; }
    public static List<string> Dump(uint pid) {
        var outp = new List<string>();
        EnumWindows((h, l) => {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p != pid || !IsWindowVisible(h)) return true;
            var b = new StringBuilder(128); GetWindowText(h, b, 128);
            RECT r; GetWindowRect(h, out r);
            outp.Add(string.Format("{0} [{1},{2} {3}x{4}] '{5}'", h, r.L, r.T, r.R - r.L, r.B - r.T, b));
            return true;
        }, IntPtr.Zero);
        return outp;
    }
}
"@
$proc = Get-Process NativeHud -ErrorAction SilentlyContinue
if (-not $proc) { Write-Output 'no NativeHud'; exit 1 }
[WinEnum]::Dump($proc.Id) | Where-Object { $_ -notmatch 'Chrome|MSCTFIME|Default IME' }
