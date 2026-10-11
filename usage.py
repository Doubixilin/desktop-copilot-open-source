"""/usage 聚合：各 agent 今日 token 消耗 + 额度出口（fused-desktop 票 04）。

per_agent.today_tokens 为"今日本地 token 消耗"，按事件时间过滤；basis 标注
完整性（复核 F06/R2-01）："exact"=全部源完整读取且事件全部可用；
"partial"=存在读不了/解析不了的源——**数字恒为下限**（codex 的坏事件行
使其会话整体退出聚合，差分依赖基线、坏基线会高估；kimicode 只跳过坏行、
只会少计）；None=无可用源（无法统计，绝不把统计失败表示成精确零）。各源口径：
- zcode：model_usage 按日窗 SQL 聚合（computed_total_tokens，按 model_id 分桶）
- codex：rollout 的 token_count 事件按行级 timestamp 过滤；total_token_usage
  是会话累计，今日 = 当前累计 − 日切前最后一次累计（今天新建的会话基线 0）
- kimicode：wire.jsonl 的 token_counting.turn_recorded 行按行级 time(epoch ms)
  过滤后求和
- hermes：messages.token_count 按日窗 SQL 求和
- qoder / kimiwork / dsh：null（本地无用量数据；dsh 待票 02 探明后并入）
quota 来自 quota.quota_state()。全部只读；聚合结果由调用方缓存。
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import quota

_TURN_RE = re.compile(r'"type":\s*"token_counting\.turn_recorded"')


def _day_start(now=None):
    dt = datetime.fromtimestamp(now) if now is not None else datetime.now()
    return datetime(dt.year, dt.month, dt.day).timestamp()


def _utc_epoch(iso):
    """codex 事件行的 UTC ISO 时间戳 → epoch 秒；无法解析返回 None。"""
    if not isinstance(iso, str):
        return None
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _valid_count(value):
    """token 计数/时间的严格校验：非负整数（复核 R3-01）。

    bool 是 int 的子类、int() 会静默截断小数/接受负数——都不能当校验器；
    只有类型与取值都合法才算有效计数。
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _open_ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)


def zcode_usage(root, day_start):
    """{today_tokens, models, basis}；库缺失/损坏返回 None。"""
    db = Path(root) / "cli" / "db" / "db.sqlite"
    if not db.is_file():
        return None
    try:
        con = _open_ro(db)
        try:
            rows = con.execute(
                "SELECT model_id, SUM(computed_total_tokens) FROM model_usage"
                " WHERE started_at >= ? GROUP BY model_id",
                (day_start * 1000,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    if not rows:
        return {"today_tokens": 0, "models": {}, "basis": "exact"}
    models = {m or "unknown": int(t or 0) for m, t in rows}
    return {"today_tokens": sum(models.values()), "models": models, "basis": "exact"}


def codex_usage(root, day_start):
    """codex 今日 token：事件级时间过滤 + 跨日差分（审计 B07）。

    total_token_usage 是会话累计值：今日消耗 = 当前累计 − 日切前最后一次
    累计（今天新建的会话基线 0）。只读 mtime 在今日之后的文件；事件行按
    行级 timestamp（UTC ISO）与本地 day_start 比较。
    完整性（复核 F06/R2-01/R3-01）：候选事件行（含 '"token_count"' 的行）
    的 JSON 结构（顶层与嵌套都必须是对象）、时间戳、计数字段（**非负整数，
    bool/小数/负数无效**）任一无效，该**会话整体退出聚合**并标记
    partial——跨日差分依赖基线，坏基线从 0 相减会高估而不是下限。因此
    partial 的数字恒为下限（被排除会话的 token 未计入）；一个可用的
    会话都没有时返回 None（无法统计，绝不冒充精确零）。
    """
    sessions_dir = Path(root) / "sessions"
    if not sessions_dir.is_dir():
        return None
    total = 0
    seen = False      # 今日活跃文件存在
    read_ok = 0       # 成功读出**且事件全部可用**的会话数
    incomplete = False
    for jf in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        try:
            if jf.stat().st_mtime < day_start:
                continue
        except OSError:
            continue
        seen = True
        try:
            text = jf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            incomplete = True
            continue
        usable = True
        base = 0   # 日切前最后一次会话累计
        cur = 0    # 今日最后一次会话累计
        for line in text.splitlines():
            if '"token_count"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                usable = False   # 候选记录损坏：该会话退出聚合（见 docstring）
                continue
            if not isinstance(obj, dict):
                usable = False   # 顶层不是事件对象（如数组）：结构无效（R3-01）
                continue
            ts = _utc_epoch(obj.get("timestamp"))
            try:
                raw = obj["payload"]["info"]["total_token_usage"]["total_tokens"]
            except (KeyError, TypeError):
                usable = False   # 嵌套结构不完整/类型不对
                continue
            if not _valid_count(raw):
                usable = False   # 布尔/小数/负数：不是有效计数，不猜不转
                continue
            if ts is None:
                usable = False   # 无法归属日期：既不能当基线也不能当今日
                continue
            if ts >= day_start:
                cur = raw
            elif raw > base:
                base = raw
        if not usable:
            incomplete = True
            continue
        read_ok += 1
        if cur > base:
            total += cur - base
    if not seen or read_ok == 0:
        return None
    return {"today_tokens": total, "models": {},
            "basis": "partial" if incomplete else "exact"}


def kimicode_usage(root, day_start):
    """kimicode 今日 token：turn_recorded 行按行级 time(epoch ms) 过滤（审计 B07）。

    完整性（复核 R2-01）：turn 记录必须是**可完整解析的 JSON** 且 time/tokens
    为整数——正则命中但截断/缺字段的行不计入并降级 partial（kimicode 是
    当日 turn 求和、无差分，跳过坏行只会少计，partial 恒为下限）；读不了
    或一个可用会话都没有返回 None，不冒充 exact。
    """
    sessions_dir = Path(root) / "sessions"
    if not sessions_dir.is_dir():
        return None
    total = 0
    seen = False
    read_ok = 0
    incomplete = False
    for wire in sessions_dir.glob("*/*/agents/main/wire.jsonl"):
        try:
            if wire.stat().st_mtime < day_start:
                continue
        except OSError:
            continue
        seen = True
        try:
            text = wire.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            incomplete = True
            continue
        read_ok += 1
        for line in text.splitlines():
            if not _TURN_RE.search(line):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                incomplete = True   # 截断/损坏的 turn 记录：不计入并降级
                continue
            turn_time = obj.get("time") if isinstance(obj, dict) else None
            tokens = obj.get("tokens") if isinstance(obj, dict) else None
            if not _valid_count(turn_time) or not _valid_count(tokens):
                incomplete = True   # 非非负整数（含负值/布尔/小数）：无法安全归属
                continue
            if turn_time / 1000.0 >= day_start:
                total += tokens
    if not seen or read_ok == 0:
        return None
    return {"today_tokens": total, "models": {},
            "basis": "partial" if incomplete else "exact"}


def hermes_usage(root, day_start):
    db = Path(root) / "state.db"
    if not db.is_file():
        return None
    try:
        con = _open_ro(db)
        try:
            (total,) = con.execute(
                "SELECT SUM(token_count) FROM messages WHERE timestamp >= ?",
                (day_start,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return {"today_tokens": int(total or 0), "models": {}, "basis": "exact"}


def usage_state(roots, now=None, codex_root=None):
    day_start = _day_start(now)
    per_agent = {}
    for tool, fn in (
        ("zcode", zcode_usage),
        ("codex", codex_usage),
        ("kimicode", kimicode_usage),
        ("hermes", hermes_usage),
    ):
        root = roots.get(tool)
        per_agent[tool] = fn(root, day_start) if root else None
    per_agent.setdefault("qoder", None)
    per_agent.setdefault("kimiwork", None)
    per_agent.setdefault("dsh", None)
    return {
        "per_agent": per_agent,
        "quota": quota.quota_state(codex_root=codex_root),
        "day_start": day_start,
    }
