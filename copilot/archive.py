"""任务档案库：settled run 的版本化归档 + 完整性判定 + 检索/证据/反馈（三期 M1）。

纪律（.scratch/closure-m1/spec.md）：
- 档案是**自包含导出物**（SQLite 仍是源）：run.json 带全部 assistant 文本、
  工具事件摘要、scene 包关联（从 user_text 的"现场包 pkg-xxx"标记反查）与
  content_sha256；
- 版本 = run_id：一次归档永不覆盖，重新归档返回现有路径；
- 完整性"未发现 ≠ 已排除"：无输出/执行错误/非正常结束/材料降级都进 caveats；
- 反馈四态（accepted/modified/rejected/needs_review）独立落 feedback.json，
  未经核定不改变 run 记录本身。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

ARCHIVE_SCHEMA = 1

FEEDBACK_VERDICTS = ("accepted", "modified", "rejected", "needs_review")

#: 检索/证据的文本上限（导出物给 AI 消费，不是全文转储）
SEARCH_SNIPPET_CHARS = 200
GET_TEXT_BUDGET = 8000

_PKG_MARK = re.compile(r"现场包 (pkg-[0-9a-f]+)")

#: 非正常结束态（RunState 终态中排除 SETTLED/LIMITED）
_ABNORMAL_STATES = {"CANCELLED", "FAILED", "INTERRUPTED"}


def archive_root(data_dir: Path) -> Path:
    return Path(data_dir) / "archive"


def run_dir(data_dir: Path, conversation_id: str, run_id: str,
            submitted_at: float) -> Path:
    ym = time.strftime("%Y%m", time.localtime(submitted_at or time.time()))
    return archive_root(data_dir) / ym / conversation_id / run_id


def scene_packages_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "scene_packages"


# ---- 完整性校验（纯逻辑） ----

def validate_output(run_public: dict[str, Any], output: dict[str, Any],
                    scene_meta: Optional[dict]) -> dict[str, Any]:
    """输出完整性判定：caveats 显式收集一切缺口。

    显式布尔字段（票03）：不把"有文字"等同于完整——截断/异常结束/材料缺口
    分别可判，不藏在 caveat 字符串里。
    """
    caveats: list[str] = []
    texts = output.get("assistant_texts") or []
    state = str(run_public.get("state", ""))
    # F12：先算全部缺口状态，再据此生成 caveats/completeness——已知截断
    # 必须进 caveats 并压成 partial，不得出现 truncated=true 却 ok
    text_truncated = any(
        isinstance(t, str) and t.endswith(("…[截断]", "…[归档硬顶截断]"))
        for t in texts
    )
    if text_truncated:
        caveats.append("归档正文含截断标记（完整性受损）")
    if not texts:
        caveats.append("无助手输出（可能被取消、失败或中断）")
    if run_public.get("error_code"):
        caveats.append(f"执行错误：{run_public['error_code']}")
    abnormal_end = state in _ABNORMAL_STATES
    if abnormal_end:
        caveats.append(f"非正常结束：{state}")
    materials_gap = False
    if scene_meta is not None:
        cov = scene_meta.get("coverage") or "none"
        if cov != "full":
            materials_gap = True
            caveats.append(f"现场材料覆盖：{cov}")
    completeness = "ok" if not caveats else ("partial" if texts else "empty")
    return {
        "completeness": completeness,
        "caveats": caveats,
        "assistant_events": len(texts),
        "assistant_chars": sum(len(t) for t in texts),
        "tool_events": len(output.get("tool_events") or []),
        # 归档路径不裁剪正文；True 仅出现在带硬顶标记的异常档
        "text_truncated": text_truncated,
        "abnormal_end": abnormal_end,
        "materials_gap": materials_gap,
    }


# ---- 记录构建（纯逻辑，输入为公开 dict） ----

def build_run_record(run_public: dict[str, Any], conversation: dict[str, Any],
                     events: list[dict[str, Any]],
                     scene_package: Optional[dict[str, Any]]) -> dict[str, Any]:
    assistant_texts = []
    tool_events = []
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if kind == "assistant":
            text = payload.get("text") or payload.get("content") or ""
            if text:
                assistant_texts.append(text)
        elif kind == "tool":
            tool_events.append({
                "title": payload.get("title") or payload.get("name")
                or payload.get("tool") or "工具调用",
                "status": payload.get("status"),
            })
    output = {
        "assistant_texts": assistant_texts,
        "assistant_chars": sum(len(t) for t in assistant_texts),
        "tool_events": tool_events,
        "final_text": assistant_texts[-1] if assistant_texts else None,
    }
    scene_meta = None
    if scene_package:
        scene_meta = {
            "package_id": scene_package.get("package_id"),
            "scenario": scene_package.get("scene", {}).get("scenario"),
            "coverage": scene_package.get("scene", {}).get("coverage"),
            "materials_count": scene_package.get("integrity", {}).get("materials_count"),
            "caveats": scene_package.get("integrity", {}).get("caveats", []),
            "package_file": scene_package.get("package_id") + ".json",
        }
    record = {
        "schema": ARCHIVE_SCHEMA,
        "run_id": run_public.get("run_id"),
        "conversation_id": run_public.get("conversation_id"),
        "conversation_title": conversation.get("title", ""),
        "cwd": conversation.get("cwd", ""),
        "agent": "dsh(acp)",
        "state": run_public.get("state"),
        "stop_reason": run_public.get("stop_reason"),
        "error_code": run_public.get("error_code"),
        "submitted_at": run_public.get("submitted_at"),
        "settled_at": run_public.get("settled_at"),
        "user_text": run_public.get("user_text", ""),
        "output": output,
        "scene": scene_meta,
        "integrity": validate_output(run_public, output, scene_meta),
        "archived_at": time.time(),
    }
    record["content_sha256"] = _content_hash(record)
    return record


def _content_hash(record: dict[str, Any]) -> str:
    material = {k: v for k, v in record.items() if k != "content_sha256"}
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ---- 落盘 / 读取（永不覆盖） ----

def archive_run(data_dir: Path, record: dict[str, Any]) -> Path:
    """写入档案；目录已存在即已归档，原样返回（版本=run_id，一次成档）。"""
    d = run_dir(data_dir, record["conversation_id"], record["run_id"],
                record.get("submitted_at") or 0)
    target = d / "run.json"
    if target.exists():
        return target
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "run.json.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, target)
    return target


# ---- 版本化修复（票03）：旧档保留，新版本 + index 指针 ----

INDEX_FILE = "run.index.json"


def current_run_file(d: Path) -> Optional[Path]:
    """run 目录 → 当前有效版本文件（index 指针优先，缺省 run.json）。"""
    idx = d / INDEX_FILE
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            f = d / str(data.get("current_file") or "")
            if f.exists():
                return f
        except (OSError, json.JSONDecodeError):
            pass
    primary = d / "run.json"
    return primary if primary.exists() else None


def archive_run_new_version(data_dir: Path, record: dict[str, Any],
                            reason: str) -> Path:
    """修复已存在归档：写 run.vN.json 新版本并更新 index；旧版本一律保留。

    不直接修写唯一真实档案（票03 边界）；无既有档案时退化为首次归档。
    """
    d = run_dir(data_dir, record["conversation_id"], record["run_id"],
                record.get("submitted_at") or 0)
    if not (d / "run.json").exists():
        return archive_run(data_dir, record)
    version = 2
    while (d / f"run.v{version}.json").exists():
        version += 1
    name = f"run.v{version}.json"
    record = {
        **record,
        "repair": {"reason": reason[:500], "version": version,
                   "created_at": time.time()},
    }
    record["content_sha256"] = _content_hash(record)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, d / name)
    versions = [{"version": 1, "file": "run.json"}]
    for v in range(2, version + 1):
        versions.append({"version": v, "file": f"run.v{v}.json"})
    index = {
        "run_id": record["run_id"],
        "current_version": version,
        "current_file": name,
        "versions": versions,
        "reason": reason[:500],
        "updated_at": time.time(),
    }
    itmp = d / (INDEX_FILE + ".tmp")
    itmp.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(itmp, d / INDEX_FILE)
    return d / name


def needs_rebuild(existing: dict[str, Any], rebuilt: dict[str, Any]) -> Optional[str]:
    """既有归档 vs 库内原始事件重建：可修复的保真缺口 → 修复原因。

    库内事件始终为原始正文（裁剪只发生在旧展示路径）；据此可判定历史
    裁剪归档并重建（票03 B06）。
    """
    old = existing.get("output") or {}
    new = rebuilt.get("output") or {}
    old_chars = old.get("assistant_chars") or 0
    new_chars = new.get("assistant_chars") or 0
    if new_chars > old_chars:
        return (f"归档正文少于原始事件（{old_chars}→{new_chars} 字，"
                f"历史展示裁剪归档）")
    for t in old.get("assistant_texts") or []:
        if isinstance(t, str) and t.endswith("…[截断]"):
            return "归档正文含展示裁剪标记"
    return None


def load_current_record(d: Path) -> Optional[dict[str, Any]]:
    f = current_run_file(d)
    if f is None:
        return None
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) and rec.get("schema") == ARCHIVE_SCHEMA else None


def find_scene_package(data_dir: Path,
                       run_public: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 user_text 的现场块标记反查 M3 包文件。"""
    mark = _PKG_MARK.search(run_public.get("user_text") or "")
    if not mark:
        return None
    path = scene_packages_dir(data_dir) / (mark.group(1) + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---- 检索 / 证据（给 AI 消费的只读面） ----

def _iter_records(data_dir: Path):
    root = archive_root(data_dir)
    if not root.is_dir():
        return
    # 修复过的 run 目录以 index 指向的当前版本为准（票03），旧版本仅存档不检索
    candidates = []
    for run_json in root.rglob("run.json"):
        cur = current_run_file(run_json.parent)
        if cur is not None:
            candidates.append(cur)
    for f in sorted(set(candidates),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("schema") == ARCHIVE_SCHEMA:
            yield rec


def context_search(data_dir: Path, query: str, limit: int = 10) -> dict[str, Any]:
    q = (query or "").strip().lower()
    hits = []
    for rec in _iter_records(data_dir):
        if len(hits) >= max(1, min(limit, 50)):
            break
        if not q:
            snippet_src = rec.get("user_text", "")
        else:
            fields = [
                rec.get("user_text", ""),
                rec.get("output", {}).get("final_text") or "",
                rec.get("conversation_title", ""),
            ]
            if not any(q in str(f).lower() for f in fields):
                continue
            snippet_src = next(f for f in fields if q in str(f).lower())
        snippet = " ".join(str(snippet_src).split())[:SEARCH_SNIPPET_CHARS]
        hits.append({
            "run_id": rec.get("run_id"),
            "conversation_title": rec.get("conversation_title"),
            "state": rec.get("state"),
            "submitted_at": rec.get("submitted_at"),
            "completeness": rec.get("integrity", {}).get("completeness"),
            "snippet": snippet,
        })
    return {
        "archive_root": str(archive_root(data_dir)),
        "query": query,
        "hit_count": len(hits),
        "hits": hits,
        "note": "检索范围为已归档任务；空结果不代表相关任务不存在（未归档的"
                "在途/新任务不在此列）。",
    }


def context_get(data_dir: Path, run_id: str) -> dict[str, Any]:
    rec = _find_record(data_dir, run_id)
    if rec is None:
        return {"error": {
            "code": "not_found",
            "message": f"档案中没有 run_id={run_id}（未归档或不存在）。"}}
    out = dict(rec)
    feedback = get_feedback(data_dir, run_id)
    out["feedback"] = feedback
    # 文本预算：给 AI 消费的不是全文转储
    texts = out.get("output", {}).get("assistant_texts")
    if isinstance(texts, list):
        joined = 0
        clipped = []
        for t in texts:
            if joined + len(t) > GET_TEXT_BUDGET:
                clipped.append(str(t)[: max(0, GET_TEXT_BUDGET - joined)] + "…[截断]")
                break
            clipped.append(t)
            joined += len(t)
        out["output"] = dict(out["output"], assistant_texts=clipped)
    return out


def context_evidence(data_dir: Path, run_id: str) -> dict[str, Any]:
    rec = _find_record(data_dir, run_id)
    if rec is None:
        return {"error": {"code": "not_found",
                          "message": f"档案中没有 run_id={run_id}。"}}
    scene = rec.get("scene") or {}
    materials = []
    pkg = _load_package_file(data_dir, scene.get("package_id"))
    if pkg:
        materials = [
            {k: m.get(k) for k in ("kind", "source", "tool", "chars", "sha256")}
            | {"text": str(m.get("text", ""))[:SEARCH_SNIPPET_CHARS]}
            for m in pkg.get("materials", [])
        ]
    return {
        "run_id": run_id,
        "integrity": rec.get("integrity"),
        "scene": scene or None,
        "materials": materials,
        "tool_events": rec.get("output", {}).get("tool_events", []),
        "content_sha256": rec.get("content_sha256"),
        "note": "材料 text 截断至摘要长度；全文与哈希校验见 scene 包文件。",
    }


def _load_package_file(data_dir: Path, package_id: Optional[str]) -> Optional[dict]:
    if not package_id:
        return None
    try:
        data = json.loads(
            (scene_packages_dir(data_dir) / f"{package_id}.json").read_text(
                encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _find_record(data_dir: Path, run_id: str) -> Optional[dict[str, Any]]:
    if not run_id:
        return None
    for rec in _iter_records(data_dir):
        if rec.get("run_id") == run_id:
            return rec
    return None


# ---- 四态人审反馈 ----

def save_feedback(data_dir: Path, run_id: str, verdict: str,
                  note: str = "") -> dict[str, Any]:
    if verdict not in FEEDBACK_VERDICTS:
        return {"error": {"code": "bad_verdict",
                          "message": f"verdict 必须是 {FEEDBACK_VERDICTS}"}}
    rec = _find_record(data_dir, run_id)
    if rec is None:
        return {"error": {"code": "not_found",
                          "message": f"档案中没有 run_id={run_id}，无法反馈。"}}
    d = run_dir(data_dir, rec["conversation_id"], run_id,
                rec.get("submitted_at") or 0)
    feedback = {
        "run_id": run_id,
        "verdict": verdict,
        "note": (note or "")[:2000],
        "updated_at": time.time(),
    }
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "feedback.json.tmp"
    tmp.write_text(json.dumps(feedback, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, d / "feedback.json")
    return {"ok": True, "feedback": feedback}


def get_feedback(data_dir: Path, run_id: str) -> Optional[dict[str, Any]]:
    rec = _find_record(data_dir, run_id)
    if rec is None:
        return None
    path = run_dir(data_dir, rec["conversation_id"], run_id,
                   rec.get("submitted_at") or 0) / "feedback.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("run_id") == run_id else None
