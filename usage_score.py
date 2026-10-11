"""使用频次打分：真启动次数 × 指数衰减（半衰期 14 天），叠加 UserAssist 冷启动先验。

打分是纯函数，"当前时间"一律由参数传入，便于断言衰减数学。
自建日志（06）提供逐次启动事件；系统现成的 UserAssist 记录只提供
"总次数 + 最后执行时间"，故折算为 count × decay(age(last)) 的先验项。

融合在**桌面图标**层面做，而不是可执行路径层面：日志与先验各自先映射到
图标，再按 1/(1+该图标的日志启动次数) 让先验退位。这样先验键与日志键的
大小写/8.3 形式差异不会把同一应用裂成两条，任务栏 .lnk 先验也不会因为
日志只记 .exe 而永远不退位。日志为空的第一天排名仍非空（权重为 1）。

本模块不读窗口标题、不碰系统预取目录（见 ADR-0002 与 tests 中的常驻守卫）。
"""
import codecs
import json
import sys
import winreg
from datetime import datetime, timezone
from pathlib import Path

import usage_log as ul

HALF_LIFE_DAYS = 14.0

USERASSIST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
# UserAssist 值布局（实机确认）：dword0 是常量 145 的会话/版本字段，不是次数；
# 运行次数在 COUNT_OFFSET，FILETIME 在 FILETIME_OFFSET。勿把次数"修正"到 0:4。
COUNT_OFFSET = 4
FILETIME_OFFSET = 60
FILETIME_MIN_LEN = 68
FILETIME_EPOCH_OFFSET = 116444736000000000


class PriorEntry:
    __slots__ = ("count", "last")

    def __init__(self, count, last):
        self.count = count
        self.last = last


def decay(age_days):
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _age_days(now, then):
    return (now - then).total_seconds() / 86400.0


def score_starts(events, now):
    """[(ts, exe)] -> {exe(小写): 衰减加权和}。纯函数。"""
    scores = {}
    for ts, exe in events:
        key = exe.lower()
        scores[key] = scores.get(key, 0.0) + decay(_age_days(now, ts))
    return scores


def count_starts(events):
    """[(ts, exe)] -> {exe(小写): 启动次数}。纯函数。"""
    counts = {}
    for _ts, exe in events:
        key = exe.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def parse_userassist(rotated_name, data):
    """解析一条 UserAssist 值：ROT13 值名 + 二进制值（完整 72 字节，至少 68 可用）。

    返回 (路径, PriorEntry)；不可用返回 None。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < FILETIME_MIN_LEN:
        return None
    name = codecs.decode(rotated_name, "rot_13")
    count = int.from_bytes(data[COUNT_OFFSET:COUNT_OFFSET + 4], "little")
    filetime = int.from_bytes(data[FILETIME_OFFSET:FILETIME_OFFSET + 8], "little")
    if filetime == 0:
        return None  # 没有最后执行时间就无法衰减，先验无从谈起
    epoch_seconds = (filetime - FILETIME_EPOCH_OFFSET) / 1e7
    try:
        last = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return name, PriorEntry(count, last)


def read_userassist_prior():
    """读系统现成启动记录为 {路径(小写): PriorEntry}。

    子键 GUID 随系统版本变化，动态枚举而非硬编码；计数条目在 <GUID>\\Count
    下一层。只收 .exe 与 .lnk 结尾的条目。
    """
    prior = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, USERASSIST_KEY)
    except OSError:
        return prior
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                sub_name = winreg.EnumKey(root, i)
            except OSError:
                break
            try:
                key = winreg.OpenKey(root, sub_name + "\\Count")
            except OSError:
                continue
            with key:
                for j in range(winreg.QueryInfoKey(key)[1]):
                    try:
                        name, data, _ = winreg.EnumValue(key, j)
                    except OSError:
                        break
                    parsed = parse_userassist(name, data)
                    if not parsed:
                        continue
                    path, entry = parsed
                    low = path.lower()
                    if not (low.endswith(".exe") or low.endswith(".lnk")):
                        continue
                    existing = prior.get(low)
                    if existing is None or entry.count > existing.count:
                        prior[low] = entry
    return prior


def read_start_events(directory=None, now=None):
    """读取 06 的 start 日志为 [(ts, exe)]；坏行跳过，未来时间戳忽略。"""
    directory = Path(directory) if directory else ul.log_dir()
    now = now or datetime.now(timezone.utc)
    events = []
    for path in sorted(directory.glob("start-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                ts = datetime.fromisoformat(record["ts"])
                exe = record["exe"]
            except (ValueError, KeyError, TypeError):
                continue
            if ts <= now:
                events.append((ts, exe))
    return events


def map_to_icons(scores, items, resolve=None):
    """把路径分数挂到桌面快捷方式：{显示名: 分数}。

    .exe 经 .lnk 目标反查；.lnk 先验条目若能在磁盘上解析出目标，则目标必须
    与桌面快捷方式的目标一致才认（防止任务栏同名快捷方式指到别的 exe），
    解析不出（shell 别名路径）才退而按 stem 与显示名对齐。映射不上的路径
    不出现在结果里（日志本身不受影响）。
    """
    by_target = {}
    by_stem = {}
    for item in items:
        if item.kind == "shortcut":
            if item.target:
                by_target.setdefault(item.target.lower(), item.name)
            by_stem.setdefault(item.name.lower(), item.name)
    out = {}
    for path, score in scores.items():
        low = path.lower()
        name = None
        if low.endswith(".exe"):
            name = by_target.get(low)
        elif low.endswith(".lnk"):
            target = resolve(low) if resolve else None
            if target:
                name = by_target.get(target.lower())
            elif not (resolve and Path(low).exists()):
                name = by_stem.get(Path(low).stem.lower())
        if name:
            out[name] = out.get(name, 0.0) + score
    return out


def fuse_icons(prior, events, now, items, resolve=None):
    """先验与自建日志在图标层面融合为 {显示名: 分数}。纯函数（resolve 除外）。

    先验权重 1/(1+该图标的日志启动次数)：日志为空时权重为 1（排名来自先验），
    日志积累后权重趋于 0（先验退位）。
    """
    counts = count_starts(events)
    log_icon = map_to_icons(score_starts(events, now), items, resolve)
    log_icon_n = map_to_icons(counts, items, resolve)
    prior_scores = {
        path: entry.count * decay(_age_days(now, entry.last)) for path, entry in prior.items()
    }
    prior_icon = map_to_icons(prior_scores, items, resolve)
    fused = dict(log_icon)
    for name, prior_score in prior_icon.items():
        n = log_icon_n.get(name, 0)
        fused[name] = fused.get(name, 0.0) + prior_score / (1 + n)
    return {name: score for name, score in fused.items() if score > 0}


def ranking(items, directory=None, now=None):
    """端到端：桌面项清单 -> {显示名: 使用频次分数}。"""
    import desktop_icons as di

    now = now or datetime.now(timezone.utc)
    return fuse_icons(
        read_userassist_prior(),
        read_start_events(directory, now),
        now,
        items,
        resolve=di.resolve_link_target,
    )


def main(argv):
    import desktop_icons as di

    sys.stdout.reconfigure(encoding="utf-8")
    items = di.list_items()
    scores = ranking(items)
    if not scores:
        print("ranking: 空（先验与日志都无可映射项）")
        return 0
    for name, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        print(f"{score:8.3f}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
