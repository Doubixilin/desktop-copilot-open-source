"""/deck 的副驾权威状态投影与去重（06 票）。

控制器状态是本项目派出任务的权威来源；外部工具仍走既有 scanner。
合并规则（spec 第 9 节）：
- 身份 = (tool, 规范持久目录 namespace, sessionId)；只删精确同源重复，
  不按标题/cwd 猜测合并；
- 兼容旧四态 RUN/CONFIRM/DONE/IDLE：PREPARING/SUBMITTING/RUNNING/CANCELLING
  → RUN；WAITING_PERMISSION → CONFIRM；终态 → IDLE（细态走 run_state）；
- 白名单新增字段：source / run_state / last_event_at / can_control；
  不序列化 home、绝对 cwd、整份任务库或权限对象；
- SETTLED 显示"已回复"，不用 scanner 的"任务完成"推断；
- 活跃/待许可不受 10 分钟淘汰；终态投影保留 ≥10 分钟后淡出（本地记录不变）。
"""
from __future__ import annotations

import time
from typing import Any, Optional

#: 终态会话在桌面展示的时长（秒）；超时后不再投影（记录仍在副驾）
TERMINAL_VISIBLE_SECONDS = 600

#: 四态兼容映射
_COMPAT_STATE = {
    "PREPARING": "RUN",
    "SUBMITTING": "RUN",
    "RUNNING": "RUN",
    "CANCELLING": "RUN",
    "WAITING_PERMISSION": "CONFIRM",
    "SETTLED": "IDLE",
    "LIMITED": "IDLE",
    "CANCELLED": "IDLE",
    "FAILED": "IDLE",
    "INTERRUPTED": "IDLE",
}

#: run_state 的用户可见细态文案（前端可直接展示）
RUN_STATE_LABELS = {
    "SETTLED": "已回复",
    "LIMITED": "已到上限",
    "CANCELLED": "已停止",
    "FAILED": "失败",
    "INTERRUPTED": "中断",
}


def _owned_session(conv: dict, run: Optional[dict], now: float) -> Optional[dict]:
    """一个副驾对话 → 一条会话投影（无 run 或 run 过旧则返回 None）。"""
    if run is None:
        return None
    state = run["state"]
    if state in RUN_STATE_LABELS:  # 终态：超时淡出
        settled = run.get("settled_at") or run.get("last_event_at") or run["submitted_at"]
        if now - settled > TERMINAL_VISIBLE_SECONDS:
            return None
    preview = None
    if state == "SETTLED":
        preview = "已回复"  # 不使用"任务完成"推断
    elif state == "WAITING_PERMISSION":
        preview = "待确认"
    elif state == "FAILED":
        preview = "失败"
    elif state == "INTERRUPTED":
        preview = "中断"
    return {
        "tool": "dsh",
        "id": conv.get("dsh_session_id") or conv["conversation_id"],
        "project": conv.get("title"),  # 复用现有字段名：未认证时一并裁剪
        "title": conv.get("title"),
        "running": state in ("PREPARING", "SUBMITTING", "RUNNING", "CANCELLING"),
        "age": max(0, round(now - run.get("last_event_at", run["submitted_at"]))),
        "tasks_done": None,
        "tasks_total": None,
        "current_task": run.get("user_text", "")[:60] or None,
        "tasks": None,
        "preview": preview,
        "preview_role": "user",
        "tokens": None,
        "state": _COMPAT_STATE.get(state, "IDLE"),
        # ---- 白名单新增字段 ----
        "source": "desktop-copilot",
        "run_state": state,
        "last_event_at": run.get("last_event_at"),
        "can_control": True,  # 仅显示标记；网页没有控制端点
        # 内部去重元数据（merge 输出前剥离，绝不序列化 home）
        "_namespace": conv.get("dsh_home", ""),
    }


def owned_sessions(snapshot: dict[str, Any], now: Optional[float] = None) -> list[dict]:
    """控制器快照 → 会话投影列表。每个对话取最新 run。"""
    now = now if now is not None else time.time()
    runs_by_conv: dict[str, dict] = {}
    for run in snapshot.get("runs") or []:
        cid = run["conversation_id"]
        cur = runs_by_conv.get(cid)
        if cur is None or run["submitted_at"] >= cur["submitted_at"]:
            runs_by_conv[cid] = run
    out = []
    for conv in snapshot.get("conversations") or []:
        run = runs_by_conv.get(conv["conversation_id"])
        record = _owned_session(conv, run, now)
        if record is not None:
            out.append(record)
    return out


def _identity(tool: str, namespace: str, session_id: str) -> tuple[str, str, str]:
    return (tool, str(namespace), str(session_id))


def merge_sessions(
    scanner_sessions: list[dict],
    owned: list[dict],
    *,
    scanner_dsh_namespace: str = "",
) -> list[dict]:
    """合并 scanner 与 owned：精确身份去重，owned 权威优先。

    scanner 条目内部补 namespace 元数据参与去重，序列化前去掉，
    输出与旧结构完全兼容（新增字段仅在 owned 条目上）。
    """
    owned_ids = {
        _identity(s["tool"], s.get("_namespace", ""), s["id"]) for s in owned
    }
    merged: list[dict] = []
    for s in scanner_sessions:
        ns = scanner_dsh_namespace if s.get("tool") == "dsh" else s.get("tool", "")
        ident = _identity(s.get("tool", ""), ns, s.get("id", ""))
        if ident in owned_ids:
            continue  # 精确同源重复：owned 权威行保留，scanner 行删除
        merged.append({k: v for k, v in s.items() if not k.startswith("_")})
    merged.extend(
        {k: v for k, v in s.items() if not k.startswith("_")} for s in owned
    )
    merged.sort(key=lambda s: s.get("age", 0))
    return merged


def project_deck(
    scanner_sessions: list[dict],
    controller_snapshot: Optional[dict[str, Any]],
    now: Optional[float] = None,
    scanner_dsh_namespace: str = "",
) -> list[dict]:
    """deck_state 的投影入口：controller 不可用时原样返回 scanner 结果。"""
    if not controller_snapshot:
        return scanner_sessions
    owned = owned_sessions(controller_snapshot, now)
    return merge_sessions(
        scanner_sessions, owned, scanner_dsh_namespace=scanner_dsh_namespace
    )
