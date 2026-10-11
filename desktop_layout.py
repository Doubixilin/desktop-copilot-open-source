"""桌面布局快照与还原。

快照 = 全部图标的 显示名 -> (x, y)，JSON 存于本地应用数据目录下本服务专属
子目录，不落在代码目录内。出厂态快照必须在任何一次写入之前生成，且一旦存在
永不覆盖；每次编排前另写一份带时间标识的运行前快照。

还原只写图标坐标：不移动、重命名或删除任何磁盘文件。
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import desktop_icons as di
import zones_lock

FACTORY_NAME = "factory.json"
RUN_PREFIX = "run-"


def data_dir():
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return root / "qoder-deck" / "layout"


_tag_lock = threading.Lock()
_last_mono = -1


def _now_tag():
    # 微秒入名：同一秒内多次快照不得互相覆盖。Windows 系统时钟粒度可粗至
    # ~15ms，纯墙上时钟连续取戳会撞名，故在锁下拼一段进程内单调纳秒值，
    # 保证同进程内文件名严格递增（文件名序即时间序）。
    global _last_mono
    with _tag_lock:
        mono = time.perf_counter_ns()
        if mono <= _last_mono:
            mono = _last_mono + 1
        _last_mono = mono
        wall = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return f"{wall}{mono:020d}Z"


def _next_run_path(directory):
    """运行前快照的落盘路径；同名时追加序号后缀。

    Windows 的 datetime.now() 钟粒度约 15ms，_now_tag 并不真正微秒唯一；
    冲突时用 _002 起的零宽序号，'_'(0x5F) 排在 '.'(0x2E) 之后，按名排序仍是时间序。
    """
    stem = f"{RUN_PREFIX}{_now_tag()}"
    path = directory / f"{stem}.json"
    seq = 2
    while path.exists():
        path = directory / f"{stem}_{seq:03d}.json"
        seq += 1
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def take_snapshot(items, kind, directory=None):
    """把当前图标坐标落盘。kind 为 'factory' 或 'run'。返回写入路径。"""
    directory = Path(directory) if directory else data_dir()
    path = directory / FACTORY_NAME if kind == "factory" else _next_run_path(directory)
    payload = {
        "kind": kind,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": [{"name": i.name, "x": i.x, "y": i.y} for i in items],
    }
    _write(path, payload)
    return path


def ensure_factory(items, directory=None):
    """出厂态快照：仅当尚不存在时写入，存在则永不覆盖。"""
    directory = Path(directory) if directory else data_dir()
    path = directory / FACTORY_NAME
    if path.exists():
        return path
    return take_snapshot(items, "factory", directory)


def run_snapshots(directory=None):
    directory = Path(directory) if directory else data_dir()
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{RUN_PREFIX}*.json"), key=lambda p: p.name)


def load_snapshot(which, directory=None):
    """which: 'factory' | 'last' | 具体文件名。"""
    directory = Path(directory) if directory else data_dir()
    if which == "factory":
        path = directory / FACTORY_NAME
    elif which == "last":
        runs = run_snapshots(directory)
        if not runs:
            raise FileNotFoundError("没有任何运行前快照")
        path = runs[-1]
    else:
        path = directory / which
    if not path.exists():
        raise FileNotFoundError(f"快照不存在: {path.name}")
    return _read(path)


def restore_moves(current, snapshot_items):
    """纯函数：算出还原所需的落位动作。

    返回 (moves, missing, extra, ambiguous)：
      moves     = [(显示名, 目标x, 目标y)]，仅当前桌面上唯一存在的项
      missing   = 快照里有、当前桌面已不存在的显示名
      extra     = 当前桌面有、快照里没有的显示名（还原时保持不动）
      ambiguous = 当前桌面上同名的项多于一个；快照按显示名存储，无法判定
                  该还原哪一个，故跳过并上报，绝不静默丢弃或猜一个
    """
    counts = di.name_counts(current)
    have = {i.name: i for i in current}
    wanted = {e["name"]: e for e in snapshot_items}
    ambiguous = sorted(n for n in wanted if counts.get(n, 0) > 1)
    skipped = set(ambiguous)
    moves = [
        (n, wanted[n]["x"], wanted[n]["y"])
        for n in wanted
        if n in have and n not in skipped
    ]
    missing = [n for n in wanted if n not in have]
    extra = [n for n in have if n not in wanted]
    return moves, missing, extra, ambiguous


def apply_moves(moves):
    """把落位动作写进桌面图标视图。调用前必须已完成 ensure_factory。"""
    with di.IconView() as view:
        view.write_named_moves(moves)


def restore(which, directory=None):
    """还原到指定快照。返回 (moved, missing, extra, ambiguous) 计数/名单四元组。"""
    with di.IconView() as view:
        current = view.bare_items()
    snapshot = load_snapshot(which, directory)
    with zones_lock.ArrangeLock(wait_seconds=20):
        ensure_factory(current, directory)
        moves, missing, extra, ambiguous = restore_moves(current, snapshot["items"])
        apply_moves(moves)
    return len(moves), missing, extra, ambiguous


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    command = argv[0] if argv else "list"
    try:
        if command == "list":
            directory = data_dir()
            factory = directory / FACTORY_NAME
            print(f"factory: {'yes' if factory.exists() else 'NO'}")
            for path in run_snapshots():
                print(f"  {path.name}")
            return 0
        if command == "snapshot":
            with di.IconView() as view:
                items = view.bare_items()
            factory = ensure_factory(items)
            path = take_snapshot(items, "run")
            print(f"factory: {factory}")
            print(f"run:     {path}")
            return 0
        if command == "restore":
            which = argv[1] if len(argv) > 1 else "last"
            moved, missing, extra, ambiguous = restore(which)
            print(f"moved={moved} missing={len(missing)} untouched_extra={len(extra)} ambiguous={len(ambiguous)}")
            for name in missing:
                print(f"  missing: {name}")
            for name in ambiguous:
                print(f"  ambiguous (same-named icons, skipped): {name}")
            return 0
    except di.DesktopViewUnavailable as exc:
        print(f"desktop-layout: {exc}", file=sys.stderr)
        return 2
    except di.WriteAborted as exc:
        print(f"desktop-layout: 还原中止：已完成 {exc.done} 项，失败于 {exc.name}：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"desktop-layout: {exc}", file=sys.stderr)
        return 2
    print(f"desktop-layout: 未知命令 {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
