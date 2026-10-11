"""应用使用日志：只记进程可执行路径与时间戳，绝不记窗口标题。

用户的窗口标题里常带案号、当事人名、项目名（见 ADR-0002），而标题对
"哪个应用常用"这个判断毫无贡献，所以采集路径上根本不获取标题文本——
不是"获取了但不写"。tests/test_usage_log.py 里有常驻隐私断言盯着这一点。

两类事件各写各的按天 JSONL 文件（start- / focus- 前缀），每条记录只有
ts 与 exe 两个字段；07 票的"真启动次数"读 start 文件。90 天滚动清理。
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

RETENTION_DAYS = 90
PRUNE_INTERVAL = 3600

KIND_START = "start"
KIND_FOCUS = "focus"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def log_dir():
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return root / "qoder-deck" / "usage"


def detect_starts(previous, current):
    """按 pid 差分识别真启动：新出现的 pid 所属可执行路径各计一次。

    用 pid 而非路径集合，才能数到"同一路径的第二个实例"与"退出后重启"。
    """
    return sorted({current[pid] for pid in current.keys() - previous.keys()})


def detect_focus(last, current):
    """前台切换到不同可执行路径时返回它，否则 None。"""
    if current is None or current == last:
        return None
    return current


def _day_file(directory, kind, ts):
    return Path(directory) / f"{kind}-{ts.strftime('%Y%m%d')}.jsonl"


_append_failures = 0


def append(directory, kind, exe, ts):
    """追加一条 {ts, exe} 记录。写入失败不拖死服务，但节流留痕（第 1 次与每 300 次）。"""
    global _append_failures
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        with _day_file(directory, kind, ts).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts.isoformat(timespec="seconds"), "exe": exe}, ensure_ascii=False) + "\n")
    except OSError as exc:
        _append_failures += 1
        if _append_failures == 1 or _append_failures % 300 == 0:
            print(f"usage-log: 写盘失败（第 {_append_failures} 次）：{exc}", flush=True)


def prune(directory, days=RETENTION_DAYS, now=None):
    """删除超过保留期的按天文件；当天文件永不被删。"""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    for path in Path(directory).glob("*-*.jsonl"):
        try:
            day = datetime.strptime(path.name.split("-")[-1].split(".")[0], "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def foreground_exe():
    """前台窗口所属进程的可执行路径。刻意不读取窗口标题。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    finally:
        kernel32.CloseHandle(handle)


def running_pid_exes():
    """pid -> 可执行路径。只取 exe，不取任何其他进程信息。"""
    out = {}
    for proc in psutil.process_iter(["exe"]):
        exe = proc.info.get("exe")
        if exe:
            out[proc.pid] = exe
    return out


class Collector:
    """一次 collect() = 一轮前台轮询 + pid 差分，返回并落盘本轮事件。"""

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else log_dir()
        self.previous = None
        self.last_focus = None

    def prune(self, now=None):
        prune(self.directory, now=now)

    def collect(self, now=None):
        now = now or datetime.now(timezone.utc)
        events = []
        current = running_pid_exes()
        if self.previous is not None:
            events.extend((KIND_START, exe) for exe in detect_starts(self.previous, current))
        self.previous = current
        switched = detect_focus(self.last_focus, foreground_exe())
        if switched:
            events.append((KIND_FOCUS, switched))
            self.last_focus = switched
        for kind, exe in events:
            append(self.directory, kind, exe, now)
        return events


def run_loop(collector, interval=2.0):
    """采集主循环：独立于数据服务的 1Hz 采样，失败只报诊死不拖垮服务。"""
    collector.prune()
    last_prune = time.monotonic()
    failures = 0
    while True:
        try:
            collector.collect()
            failures = 0
        except Exception as exc:
            failures += 1
            if failures == 1 or failures % 300 == 0:
                print(f"usage-log: 采集失败（第 {failures} 次）：{exc}", flush=True)
        if time.monotonic() - last_prune > PRUNE_INTERVAL:
            collector.prune()
            last_prune = time.monotonic()
        time.sleep(interval)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    interval = float(argv[0]) if argv else 2.0
    collector = Collector()
    print(f"usage-log: 采集到 {collector.directory}，每 {interval}s 一轮；Ctrl-C 退出")
    try:
        run_loop(collector, interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
