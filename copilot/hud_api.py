"""HUD 任务中枢 HTTP 面：server.py 路由背后的纯函数层（M5）。

输入一律是 (controller, hub) 鸭子类型 + 解析后的 payload——不碰 HTTP 细节，
单测直调。错误语义：ControllerError → {"error": {"code", "message"}}（路由层
转 400）；副驾未启用 → available=false + reason（200，UI 诚实显示）。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from . import models
from .scene_package import freeze_scene_package, render_scene_block
from .task_hub import TaskHub
from . import archive

#: 总览护栏：会话数 / 每会话事件尾 / 事件 payload 文本截断
MAX_CONVERSATIONS = 10

#: 数据目录注入点（server 启动时 set；hud_snapshot 用它读反馈态）
_data_dir_holder: list = []
MAX_EVENTS_PER_CONV = 30
EVENT_TEXT_CLIP = 2000

DEFAULT_WORKSPACE = "."  # 由路由层替换为 server 进程 cwd 的绝对路径

#: request_id → 受理映射（进程内，09 票改 submissions 表持久化）；上限 FIFO 防膨胀
MAX_REQUEST_MAP = 256
_REQUEST_RUNS: dict[str, dict] = {}
#: 查图→受理→登记全程串行（F02）：并发同 ID 只允许一次真实派单
_REQUEST_LOCK = threading.Lock()


def _submit_fingerprint(text: str, context_refs, scene, conv_id: str) -> str:
    """载荷指纹：同 request_id 重发必须同指纹，否则视为冲突（R10 语义先行）。

    必须始终用**客户端原始载荷**（含空 conversation_id）计算——受理后生成的
    会话身份不属于载荷（F01：新会话首投与重试指纹必须一致）。
    """
    import hashlib
    import json as _json

    material = {
        "text": text,
        "context_refs": list(context_refs or []),
        "scene": scene if isinstance(scene, dict) else None,
        "conversation_id": conv_id or "",
    }
    return hashlib.sha256(
        _json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def reset_request_runs() -> None:
    """测试隔离用：清空进程内受理映射。"""
    _REQUEST_RUNS.clear()


def _event_public(ev) -> dict[str, Any]:
    """EventRecord → 公开 dict（序列化+文本护栏一步完成）。"""
    payload = getattr(ev, "payload", None)
    if payload is None and isinstance(ev, dict):
        payload = ev.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    clipped = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > EVENT_TEXT_CLIP:
            clipped[k] = v[:EVENT_TEXT_CLIP] + "…[截断]"
        else:
            clipped[k] = v
    if isinstance(ev, dict):
        seq = ev.get("seq")
        kind = ev.get("kind")
        run_id = ev.get("run_id")
        observed_at = ev.get("observed_at")
    else:
        seq, kind, run_id, observed_at = ev.seq, ev.kind, ev.run_id, ev.observed_at
    return {
        "seq": seq,
        "run_id": run_id,
        "kind": kind,
        "payload": clipped,
        "observed_at": observed_at,
    }


def _clip_event(ev) -> dict[str, Any]:
    return _event_public(ev)


def set_data_dir(path) -> None:
    _data_dir_holder.clear()
    if path:
        _data_dir_holder.append(path)


def _data_dir_hint():
    from pathlib import Path

    return Path(_data_dir_holder[0]) if _data_dir_holder else None


def hud_snapshot(controller, hub: Optional[TaskHub]) -> dict[str, Any]:
    """总览：hub 任务池 + 会话（带事件尾）+ 活跃轮次 + 待决许可 + 统计。"""
    if controller is None:
        return {"available": False, "reason": "copilot 未启用或依赖缺失"}
    snap = controller.snapshot()
    conversations = []
    for conv in snap.get("conversations", [])[:MAX_CONVERSATIONS]:
        events = [
            _clip_event(e)
            for e in controller.events(conv["conversation_id"])[-MAX_EVENTS_PER_CONV:]
        ]
        item = {**conv, "events": events}
        # 末条 assistant 事件的反馈态（三期：UI 显示当前人审结论）
        for ev in reversed(events):
            if ev.get("kind") == "assistant" and ev.get("run_id"):
                fb = archive.get_feedback(_data_dir_hint(), ev["run_id"]) if _data_dir_hint() else None
                if fb:
                    item["feedback"] = fb
                break
        conversations.append(item)
    return {
        "available": True,
        "tasks": hub.snapshot() if hub is not None else {"tasks": [], "stats": {}, "agents": []},
        "conversations": conversations,
        "active_run": snap.get("active_run"),
        "pending_permissions": snap.get("pending_permissions", []),
        "runtime": snap.get("runtime", {}),
        "store_healthy": snap.get("store_healthy", True),
    }


def _submit_new(controller, text: str, conv_id: str, workspace: str, scene,
                context_refs, package_dir, request_id: str, fingerprint):
    """新受理：scene 冻结注入 → 建会话（惰性）→ controller.submit → 登记映射。

    供 request_id 路径在 _REQUEST_LOCK 内调用（映射登记因此原子），或无
    request_id 时直接调用（无映射可登记）。
    """
    if isinstance(scene, dict) and scene.get("materials"):
        pkg = freeze_scene_package(text, scene, package_dir=package_dir)
        text = render_scene_block(pkg) + "\n\n" + text
    try:
        if not conv_id:
            conv = controller.create_conversation(workspace)
            conv_id = conv.conversation_id
        run = controller.submit(conv_id, text, context_refs=list(context_refs))
    except Exception as exc:  # ControllerError 形状兼容
        code = getattr(exc, "code", "submit_failed")
        return {"error": {"code": code, "message": str(exc)}}
    if request_id:
        _REQUEST_RUNS[request_id] = {
            # F01：指纹基于客户端原始载荷（入口已算好），不用受理后的会话身份
            "fingerprint": fingerprint,
            "conversation_id": conv_id,
            "text": text,
            "context_refs": list(context_refs),
            "run_id": run.run_id,
        }
        while len(_REQUEST_RUNS) > MAX_REQUEST_MAP:
            _REQUEST_RUNS.pop(next(iter(_REQUEST_RUNS)))
    return {"conversation_id": conv_id, "run": run.to_public(),
            "request_id": request_id or None}


def submit_task(
    controller,
    payload: dict[str, Any],
    *,
    default_workspace: str = DEFAULT_WORKSPACE,
    package_dir=None,
) -> dict[str, Any]:
    """真发起：建会话（惰性）→ scene 冻结注入 → controller.submit。

    request_id 幂等（R09）：同 ID 同载荷重发返回原受理（controller 按 run_id
    去重）；同 ID 不同载荷返回 idempotency_conflict。进程内映射，持久受理
    表（崩溃对账）由 09 票落库。
    """
    if controller is None:
        return {"error": {"code": "unavailable", "message": "copilot 未启用"}}
    text = str(payload.get("text", "")).strip()
    if not text:
        return {"error": {"code": "empty_prompt", "message": "输入为空"}}
    conv_id = payload.get("conversation_id") or ""
    workspace = str(payload.get("workspace") or default_workspace)
    scene = payload.get("scene")
    context_refs = payload.get("context_refs") or []
    request_id = str(payload.get("request_id") or "").strip()
    # F01：指纹在创建会话之前、以客户端原始载荷计算——首投与重试同指纹
    fingerprint = _submit_fingerprint(text, context_refs, scene, conv_id) \
        if request_id else None

    if not request_id:
        return _submit_new(controller, text, conv_id, workspace, scene,
                           context_refs, package_dir, "", None)

    # F02：查图→受理→登记在锁内完成——并发同 ID 请求串行化，只执行一次派单
    with _REQUEST_LOCK:
        prev = _REQUEST_RUNS.get(request_id)
        if prev is not None:
            if prev["fingerprint"] != fingerprint:
                return {"error": {
                    "code": "idempotency_conflict",
                    "message": "同一 request_id 载荷不同，拒绝静默返回旧结果"}}
            try:
                run = controller.submit(
                    prev["conversation_id"], prev["text"],
                    context_refs=list(prev["context_refs"]),
                    run_id=prev["run_id"])
            except Exception as exc:
                code = getattr(exc, "code", "submit_failed")
                return {"error": {"code": code, "message": str(exc)}}
            return {"conversation_id": prev["conversation_id"],
                    "run": run.to_public(), "request_id": request_id,
                    "replayed": True}
        return _submit_new(controller, text, conv_id, workspace, scene,
                           context_refs, package_dir, request_id, fingerprint)


def answer_permission(controller, payload: dict[str, Any]) -> dict[str, Any]:
    if controller is None:
        return {"error": {"code": "unavailable", "message": "copilot 未启用"}}
    pid = str(payload.get("permission_id", ""))
    option = str(payload.get("option_id", ""))
    if not pid or not option:
        return {"error": {"code": "bad_request", "message": "缺少 permission_id/option_id"}}
    ok = controller.answer_permission(pid, option)
    return {"ok": ok}


def cancel_run(controller, payload: dict[str, Any]) -> dict[str, Any]:
    if controller is None:
        return {"error": {"code": "unavailable", "message": "copilot 未启用"}}
    run_id = str(payload.get("run_id", ""))
    if not run_id:
        return {"error": {"code": "bad_request", "message": "缺少 run_id"}}
    return {"ok": controller.cancel(run_id)}


def list_events(controller, conversation_id: str, after_seq: int = 0) -> dict[str, Any]:
    if controller is None:
        return {"error": {"code": "unavailable", "message": "copilot 未启用"}}
    if not conversation_id:
        return {"error": {"code": "bad_request", "message": "缺少 conversation_id"}}
    events = [
        _clip_event(e)
        for e in controller.events(conversation_id, after_seq=after_seq)
    ]
    return {"conversation_id": conversation_id, "events": events}


#: 归档批量：一次 tick / 一轮补偿最多处理的终态 run 数（有界）
ARCHIVE_TICK_BATCH = 10

_TERMINAL_STATES = {"SETTLED", "LIMITED", "CANCELLED", "FAILED", "INTERRUPTED"}

#: 归档正文事件的载荷字段上限护栏：异常超大 payload（如失控的工具输出）在
#: 归档侧仍设硬顶，避免单档无限膨胀；正常回复文本远低于此
ARCHIVE_EVENT_PAYLOAD_CHARS = 2_000_000


def _event_raw(ev) -> dict[str, Any]:
    """EventRecord → 完整 dict（归档路径专用：不裁剪正文）。

    展示裁剪（_event_public 的 2000 字护栏）只属于显示面；归档必须保真
    （票03 B06：4518 字输出不得归档成 2005 字还标完整）。
    """
    if isinstance(ev, dict):
        return {
            "seq": ev.get("seq"),
            "run_id": ev.get("run_id"),
            "kind": ev.get("kind"),
            "payload": ev.get("payload") or {},
            "observed_at": ev.get("observed_at"),
        }
    payload = getattr(ev, "payload", None) or {}
    if isinstance(payload, dict):
        payload = {
            k: (v[:ARCHIVE_EVENT_PAYLOAD_CHARS] + "…[归档硬顶截断]"
                if isinstance(v, str) and len(v) > ARCHIVE_EVENT_PAYLOAD_CHARS else v)
            for k, v in payload.items()
        }
    return {
        "seq": ev.seq,
        "run_id": ev.run_id,
        "kind": ev.kind,
        "payload": payload,
        "observed_at": ev.observed_at,
    }


def archive_one(data_dir, run, find_events, find_conversation):
    """归档单个终态 run（原始事件保真）；返回归档文件路径。

    已归档（run.json 存在）原样返回现有路径——去重由 archive_run 永不覆盖
    保证，按 run_id 幂等。
    """
    from pathlib import Path

    root = Path(data_dir)
    run_public = run.to_public()
    events = [_event_raw(e) for e in find_events(run.conversation_id, run.run_id)]
    record = archive.build_run_record(
        run_public,
        find_conversation(run.conversation_id) or
        {"conversation_id": run.conversation_id, "title": "", "cwd": ""},
        events,
        archive.find_scene_package(root, run_public),
    )
    return archive.archive_run(root, record)


def is_archived(data_dir, run) -> bool:
    from pathlib import Path

    return archive.run_dir(
        Path(data_dir), run.conversation_id, run.run_id, run.submitted_at
    ).joinpath("run.json").exists()


def archive_tick(data_dir, runs, find_events, find_conversation) -> int:
    """惰性归档一批终态 run；返回本次新归档数。

    runs: list[models.Run]；find_events(conversation_id, run_id) -> list[
    EventRecord]（序列化在本层完成，原始事件不裁剪）；find_conversation(
    conv_id) -> dict（title/cwd，找不到给空 dict 即可）。
    """
    terminal = [r for r in runs
                if getattr(getattr(r, "state", None), "value", "") in _TERMINAL_STATES]
    terminal.sort(key=lambda r: getattr(r, "settled_at", None) or 0)
    archived = 0
    for run in terminal:
        if archived >= ARCHIVE_TICK_BATCH:
            break
        if is_archived(data_dir, run):
            continue
        archive_one(data_dir, run, find_events, find_conversation)
        archived += 1
    return archived


def save_feedback(data_dir, payload: dict) -> dict:
    run_id = str(payload.get("run_id", ""))
    verdict = str(payload.get("verdict", ""))
    note = str(payload.get("note", "") or "")
    if not run_id:
        return {"error": {"code": "bad_request", "message": "缺少 run_id"}}
    return archive.save_feedback(data_dir, run_id, verdict, note)
