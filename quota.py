"""订阅额度/余额只读采集（fused-desktop 票 03）。

三个来源，全部只读、进程内缓存 60s、任何失败静默返回 None（绝不显示 0）：
- codex：会话文件尾部随 token_count / custom_tool_call_output 携带的 rateLimits
  快照，本地正则解析（票 01 的红利发现；usedPercent + windowDurationMins + resetsAt）
- dsh：官方文档的 GET https://api.deepseek.com/user/balance（API key 计费余额，
  Authorization: Bearer <key>；key 依次取环境变量 QD_DEEPSEEK_API_KEY、
  ~/.dsh/.credentials.yaml 中的 api key 字段）
- 可配置订阅端点：%LOCALAPPDATA%/qoder-deck/quota.json 的 "dsh_subscription"：
  {"url": ..., "token_env": 可选, "json_path_remaining": 可选, "remaining_unit":
  "percent"|"fraction" 可选默认 percent}——DSH 订阅的 5 小时窗/周额度端点未
  公开文档化，等具体路径确认后回填；不配置则该源为 None。

key 与余额数据不落日志、不落仓库；本模块不写任何文件。
"""
import json
import math
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import agent_sessions

CACHE_TTL = 60.0
BALANCE_URL = "https://api.deepseek.com/user/balance"
BALANCE_TIMEOUT = 4.0

_cache = {"ts": 0.0, "data": {}}
# 缓存过期瞬间的并发请求只允许一次真实采集（审计 B10：single-flight）；
# 锁内双重检查：后到者等首个结果，直接读新鲜缓存
_cache_lock = threading.Lock()

# codex 快照字段（在多重转义的 JSON 文本里也直接子串可寻）
_USED_PCT_RE = re.compile(r'usedPercent[\\"]*:\s*[\\"]*(\d+)')
_WINDOW_MINS_RE = re.compile(r'windowDurationMins[\\"]*:\s*[\\"]*(\d+)')
_RESETS_AT_RE = re.compile(r'resetsAt[\\"]*:\s*[\\"]*(\d{9,12})')


def _window_label(minutes):
    if minutes and minutes <= 310:
        return "5h"
    if minutes and minutes <= 10090:
        return "weekly"
    return f"{minutes}m" if minutes else None


def codex_quota(root=None):
    """最近一个 codex 会话文件尾部的额度快照；无快照返回 None。"""
    root = Path(root) if root else Path.home() / ".codex"
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return None
    files = sorted(sessions_dir.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for jf in reversed(files[-5:]):
        tail = agent_sessions._read_tail(jf)
        if '"usedPercent' not in tail and "usedPercent" not in tail:
            continue
        used = _USED_PCT_RE.findall(tail)
        window = _WINDOW_MINS_RE.findall(tail)
        resets = _RESETS_AT_RE.findall(tail)
        if used:
            used_pct = int(used[-1])
            return {
                "window": _window_label(int(window[-1])) if window else None,
                "remaining_pct": 100 - used_pct,
                "resets_at": int(resets[-1]) if resets else None,
                "source": "codex-local-snapshot",
            }
    return None


def _dsh_key():
    env = os.environ.get("QD_DEEPSEEK_API_KEY")
    if env:
        return env
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    try:
        text = cred.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # 最小 yaml 探测：形如 api_key: sk-xxx / apiKey: "sk-xxx" 的行
    m = re.search(r'(?im)^\s*(?:api_?key)\s*:\s*["\']?(sk-[\w-]+)', text)
    return m.group(1) if m else None


def _get_json(url, token, timeout=BALANCE_TIMEOUT):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dsh_balance():
    """官方 /user/balance：{window:'balance', detail:…, source}；无 key/失败 None。"""
    token = _dsh_key()
    if not token:
        return None
    try:
        data = _get_json(BALANCE_URL, token)
    except Exception:
        return None
    infos = data.get("balance_infos") or []
    cny = next((i for i in infos if i.get("currency") == "CNY"), infos[0] if infos else None)
    if not cny:
        return None
    return {
        "window": "balance",
        "remaining_pct": None,
        "detail": {
            "is_available": bool(data.get("is_available")),
            "currency": cny.get("currency"),
            "total_balance": cny.get("total_balance"),
        },
        "source": "deepseek-user-balance",
    }


def _subscription_config():
    path = Path(os.environ.get("LOCALAPPDATA", "")) / "qoder-deck" / "quota.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    sub = cfg.get("dsh_subscription")
    return sub if isinstance(sub, dict) and sub.get("url") else None


# 单位契约（复核 F03）：字段名或配置必须显式表达单位，禁止按数值大小猜
# 比例。*_pct/*_percent 视为百分数（0.5 = 0.5%）；*_fraction 视为比例
# （0.005 = 0.5%）；点路径值的单位由订阅配置 remaining_unit 声明（默认
# percent）。裸 remaining/used/left 无法区分 0.5 是 50% 还是 0.5%，返回 None。
_PERCENT_KEYS = ("remaining_pct", "remaining_percent", "left_pct")
_PERCENT_USED_KEYS = ("used_percent", "usedPercent", "used_pct")
_FRACTION_KEYS = ("remaining_fraction", "left_fraction")
_FRACTION_USED_KEYS = ("used_fraction",)


def _extract_pct(data, path, unit="percent"):
    """提取剩余百分比；单位由字段名/配置显式声明，歧义输入返回 None。"""
    node = data
    for part in (path or "").split("."):
        if not part:
            continue
        node = node.get(part) if isinstance(node, dict) else None
    if path:
        kind = "fraction" if str(unit).lower() == "fraction" else "percent"
        is_used = False  # 点路径语义固定为"剩余"（json_path_remaining）
    elif isinstance(data, dict):
        kind = is_used = None
        for key in _PERCENT_KEYS + _PERCENT_USED_KEYS + _FRACTION_KEYS + _FRACTION_USED_KEYS:
            if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
                node = data[key]
                is_used = key in _PERCENT_USED_KEYS or key in _FRACTION_USED_KEYS
                kind = "fraction" if key in _FRACTION_KEYS or key in _FRACTION_USED_KEYS else "percent"
                break
    else:
        return None
    if not isinstance(node, (int, float)) or isinstance(node, bool):
        return None
    if kind is None:
        return None  # 裸 remaining/used/left：单位不可知
    value = float(node)
    if not math.isfinite(value):
        return None
    if kind == "fraction":
        value *= 100
    pct = 100 - value if is_used else value
    if not 0 <= pct <= 100:
        return None  # 越界视为坏数据，返回未知而不是猜测
    return round(pct, 1)


def dsh_subscription():
    """配置驱动的订阅额度端点（5h/周窗，端点路径待回填）。"""
    cfg = _subscription_config()
    if not cfg:
        return None
    token = os.environ.get(cfg.get("token_env") or "QD_DEEPSEEK_API_KEY") or _dsh_key()
    if not token:
        return None
    try:
        data = _get_json(cfg["url"], token)
    except Exception:
        return None
    pct = _extract_pct(data, cfg.get("json_path_remaining"),
                       unit=cfg.get("remaining_unit") or "percent")
    if pct is None:
        return None
    return {
        "window": cfg.get("window") or "5h",
        "remaining_pct": pct,
        "resets_at": data.get("resets_at") if isinstance(data, dict) else None,
        "source": "dsh-subscription-endpoint",
    }


def quota_state(now=None, codex_root=None):
    """全部额度源的当前值（/usage 的 quota 字段来源）。60s 缓存，并发 single-flight。"""
    now = time.monotonic() if now is None else now
    with _cache_lock:
        if now - _cache["ts"] < CACHE_TTL and _cache["data"]:
            return _cache["data"]
        data = {
            "codex": codex_quota(codex_root),
            "dsh": dsh_subscription() or dsh_balance(),
        }
        _cache.update(ts=now, data=data)
        return data
