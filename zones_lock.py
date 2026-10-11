"""编排互斥锁：手动 apply / 还原 / 看门狗增量编排不得并发写图标。

用目录的原子 mkdir 做跨进程锁（Windows 上 mkdir 已存在即失败）。holder 标记
（pid + 时间戳）放在锁目录**外面**的独立文件里：mkdir 成功后立刻写 holder，
等待方看到"有锁无 holder"时先宽限再判定，避免把刚 mkdir 还没写 holder 的活锁
当成半死锁打破。

陈旧检测用于进程被杀在持锁中的孤儿锁（实机踩过：Stop-Process 撞在看门狗编排
上）。STALE_SECONDS 必须大于一次编排的最坏时长（视图追平 15 轮 + 写 + 收敛
3 轮 ≈ 数十秒），否则会破掉活锁、造成锁明令禁止的双写交错。
"""
import os
import time
from pathlib import Path

import psutil

STALE_SECONDS = 300.0
HOLDER_GRACE = 0.5


class ArrangeBusy(Exception):
    """另一个编排者正持有锁。"""


def lock_path():
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return root / "qoder-deck" / "arrange.lock"


def holder_path():
    return lock_path().with_name("arrange.holder")


class ArrangeLock:
    def __init__(self, wait_seconds=5.0, poll=0.25):
        self.path = lock_path()
        self.holder = holder_path()
        self.wait_seconds = wait_seconds
        self.poll = poll

    def _stale(self):
        try:
            pid, stamp = self.holder.read_text(encoding="utf-8").split()
            age = time.time() - float(stamp)
        except (OSError, ValueError):
            time.sleep(HOLDER_GRACE)  # 可能正处在 mkdir 与写 holder 之间
            if not self.holder.exists():
                return True  # 宽限后仍无 holder = 半死锁
            return False
        if age > STALE_SECONDS:
            return True
        try:
            return not psutil.pid_exists(int(pid))
        except ValueError:
            return True

    def _break_stale(self):
        try:
            self.holder.unlink()
        except OSError:
            pass
        try:
            os.rmdir(self.path)
        except OSError:
            return False
        return True

    def __enter__(self):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                os.mkdir(self.path)
                break
            except FileExistsError:
                if self._stale() and self._break_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise ArrangeBusy(f"编排锁被占用：{self.path}")
                time.sleep(self.poll)
        self.holder.write_text(f"{os.getpid()} {time.time()}", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        try:
            self.holder.unlink()
        except OSError:
            pass
        try:
            os.rmdir(self.path)
        except OSError:
            pass
        return False
