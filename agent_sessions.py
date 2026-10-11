"""七工具会话采集（AGENT DECK 会话列表的数据来源）。

会话列表的唯一接缝 collect_sessions(roots, now)：roots 为 {工具名: 数据根路径}，now 为墙钟秒。
/performance 的 Qoder 状态块不经此接缝，但复用 task_stats/project_name 两个助手。
每工具一个严格只读的扫描器；任一工具扫描失败静默跳过、只记日志（ADR 0003）。
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

RUNNING_WINDOW = 90.0
ACTIVE_WINDOW = 600.0
PREVIEW_MAX = 100
_DSH_MAX_BYTES = 16 << 20   # dsh zstd 解压上限（超限只丢尾部预览，防巨型文件撑爆内存）
TASKS_LIST_CAP = 10


def collect_sessions(roots, now):
    sessions = []
    for tool, root in roots.items():
        scanner = SCANNERS.get(tool)
        if scanner is None:
            continue
        try:
            sessions.extend(scanner(Path(root), now))
        except Exception as exc:
            print(f"agent-sessions: {tool} 扫描失败，跳过：{exc}", flush=True)
    sessions.sort(key=lambda s: s["age"])
    return sessions


def task_stats(session_id, root):
    done = total = 0
    current = None
    tasks_dir = root / "tasks" / session_id
    if tasks_dir.is_dir():
        for tf in tasks_dir.glob("*.json"):
            try:
                task = json.loads(tf.read_text(encoding="utf-8"))
            except Exception:
                continue
            total += 1
            status = task.get("status")
            if status == "completed":
                done += 1
            elif status == "in_progress" and current is None:
                current = task.get("subject")
    return done, total, current


def _read_tail(path, nbytes=131072):
    """读文件尾部最后 nbytes 字节并按 utf-8 宽容解码；读不到返回空串。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _read_head(path, nbytes=8192):
    """读文件头部前 nbytes 字节（codex session_meta 在首行）；读不到返回空串。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(nbytes).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _iter_tail_records(tail):
    """尾部倒序逐行解析 JSON 记录，跳过坏行。"""
    for line in reversed(tail.splitlines()):
        if '"type"' not in line and '"role"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            yield rec


def _record_role(rec):
    role = rec.get("type") or rec.get("role")
    return role if role in ("assistant", "user") else None


def _record_preview(rec):
    """单条记录的预览：优先最后一个非空 text 块，无 text 块才取 tool_use 工具名。"""
    message = rec.get("message") if isinstance(rec.get("message"), dict) else rec
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if not isinstance(content, list):
        return None
    blocks = [b for b in content if isinstance(b, dict)]
    for block in reversed(blocks):
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                return text
    for block in reversed(blocks):
        if block.get("type") == "tool_use" and block.get("name"):
            return f"tool:{block['name']}"
    return None


def _extract_preview(tail):
    """尾部倒扫取最近一条可预览的 assistant/user 记录，返回 (preview, role)。"""
    for rec in _iter_tail_records(tail):
        role = _record_role(rec)
        if role is None:
            continue
        preview = _record_preview(rec)
        if preview:
            if len(preview) > PREVIEW_MAX:
                preview = preview[:PREVIEW_MAX] + "…"
            return preview, role
    return None, None


def _last_role_kind(tail):
    """最近一条 assistant/user 记录的 (role, kind)。

    kind: tool = 最后动作是工具调用；text = 文本回复；user = 人工输入。
    """
    for rec in _iter_tail_records(tail):
        role = _record_role(rec)
        if role is None:
            continue
        if role == "user":
            return role, "user"
        content = (rec.get("message") or rec).get("content")
        if isinstance(content, list) and content:
            last = content[-1] if isinstance(content[-1], dict) else {}
            return role, "tool" if last.get("type") == "tool_use" else "text"
        return role, "text"
    return None, None


def _session_last(jsonl_path):
    return _last_role_kind(_read_tail(jsonl_path))


def _session_state(age, role, kind):
    if age <= RUNNING_WINDOW:
        return "RUN"
    if kind == "tool":
        return "CONFIRM"
    if role == "assistant":
        return "DONE"
    return "IDLE"


def project_name(jsonl_path):
    try:
        with open(jsonl_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="ignore")
        cwd = None
        for line in reversed(tail.splitlines()):
            if '"cwd"' not in line:
                continue
            try:
                cwd = json.loads(line).get("cwd")
            except Exception:
                continue
            if cwd:
                break
        if cwd:
            return Path(cwd).name
    except OSError:
        pass
    return jsonl_path.parent.name


def task_list(session_id, root, cap=TASKS_LIST_CAP):
    """会话任务清单（[{subject,status}]，≤cap 条），供聚焦详情渲染。"""
    tasks_dir = root / "tasks" / session_id
    if not tasks_dir.is_dir():
        return None
    items = []
    for tf in tasks_dir.glob("*.json"):
        try:
            task = json.loads(tf.read_text(encoding="utf-8"))
        except Exception:
            continue
        subject = task.get("subject")
        if subject:
            items.append({"subject": subject, "status": task.get("status")})
    if not items:
        return None
    items.sort(key=lambda t: t["subject"])
    return items[:cap]


def _scan_qoder_sessions(root, now):
    sessions = []
    projects_dir = root / "projects"
    if not projects_dir.is_dir():
        return sessions
    for proj in projects_dir.iterdir():
        if not proj.is_dir():
            continue
        for jf in proj.glob("*.jsonl"):
            try:
                mtime = jf.stat().st_mtime
            except OSError:
                continue
            age = now - mtime
            if age > ACTIVE_WINDOW:
                continue
            done, total, current = task_stats(jf.stem, root)
            tail = _read_tail(jf)
            role, kind = _last_role_kind(tail)
            preview, preview_role = _extract_preview(tail)
            sessions.append(
                _session(
                    "qoder",
                    jf.stem,
                    project_name(jf),
                    age,
                    _session_state(age, role, kind),
                    tasks=(done, total),
                    current_task=current,
                    tasks_list=task_list(jf.stem, root),
                    preview=preview,
                    preview_role=preview_role,
                )
            )
    return sessions


def _open_ro(db_path):
    # busy timeout 调短：库被写方独占时快速失败走静默跳过，不拖住 /deck 轮询
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)


def _dir_name(path_str):
    return Path(path_str).name if path_str else ""


def _session(
    tool,
    sid,
    project,
    age,
    state,
    running=None,
    tasks=None,
    title=None,
    current_task=None,
    tasks_list=None,
    preview=None,
    preview_role=None,
    tokens=None,
):
    return {
        "tool": tool,
        "id": sid,
        "project": project,
        "title": title,
        "running": (age <= RUNNING_WINDOW) if running is None else running,
        "age": round(age),
        "tasks_done": None if tasks is None else tasks[0],
        "tasks_total": None if tasks is None else tasks[1],
        "current_task": current_task,
        "tasks": tasks_list,
        "preview": preview,
        "preview_role": preview_role,
        "tokens": tokens,
        "state": state,
    }


def _hermes_leases(root):
    """活跃租约里的会话 id 集合；租约文件坏了只当没有，不连累整工具。"""
    try:
        data = json.loads((root / "runtime" / "active_sessions.json").read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {e.get("session_id") for e in data.get("entries", []) if e.get("session_id")}


# codex 会话记录里 thread_source 标注来源；guardian_review 等内部会话不入列
CODEX_SOURCES = (None, "user")
CODEX_TOOL_TYPES = ("function_call", "custom_tool_call")

# session_meta 首行可能被 base_instructions 撑到远超读取窗口，
# 故只按正则提取行首负载里的三个字段（均在 base_instructions 之前），
# 不做整行 JSON 解析。
_CODEX_SID_RE = re.compile(r'"session_id":\s*"([0-9a-f-]{36})"')
_CODEX_CWD_RE = re.compile(r'"cwd":\s*"((?:[^"\\]|\\.)*)"')
_CODEX_SOURCE_RE = re.compile(r'"thread_source":\s*"([a-z_]*)"')


def _codex_meta(head):
    """首行 session_meta 的 (session_id, cwd, thread_source)，解析失败退空。"""
    first = head.split("\n", 1)[0]
    sid_m = _CODEX_SID_RE.search(first)
    cwd_m = _CODEX_CWD_RE.search(first)
    src_m = _CODEX_SOURCE_RE.search(first)
    cwd = None
    if cwd_m:
        try:
            cwd = json.loads('"' + cwd_m.group(1) + '"')
        except Exception:
            cwd = None
    return {
        "session_id": sid_m.group(1) if sid_m else None,
        "cwd": cwd,
        "thread_source": src_m.group(1) if src_m else None,
    }


def _codex_last_role_kind(tail):
    """codex 尾扫：response_item 里最近一条 message/function_call 定 (role, kind)。

    message+user → 人工输入；message+assistant → 文本回复；
    function_call/custom_tool_call → 最后动作是工具调用。developer 等其余跳过。
    """
    for line in reversed(tail.splitlines()):
        if '"response_item"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        payload = rec.get("payload") or {}
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                return "user", "user"
            if role == "assistant":
                return "assistant", "text"
        elif ptype in CODEX_TOOL_TYPES:
            return "assistant", "tool"
    return None, None


def _codex_tokens(tail):
    """最后一个 token_count 事件的 total_token_usage（会话累计），无则 None。"""
    for line in reversed(tail.splitlines()):
        if '"total_token_usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        info = (rec.get("payload") or {}).get("info") or {}
        usage = info.get("total_token_usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            return {
                "input": usage.get("input_tokens"),
                "cached_input": usage.get("cached_input_tokens"),
                "output": usage.get("output_tokens"),
                "reasoning": usage.get("reasoning_output_tokens"),
                "total": usage.get("total_tokens"),
            }
    return None


def _scan_codex(root, now):
    sessions = []
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return sessions
    for jf in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        try:
            mtime = jf.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age > ACTIVE_WINDOW:
            continue
        meta = _codex_meta(_read_head(jf))
        if (meta or {}).get("thread_source") not in CODEX_SOURCES:
            continue
        sid = (meta or {}).get("session_id") or jf.stem[-36:]
        tail = _read_tail(jf)
        role, kind = _codex_last_role_kind(tail)
        sessions.append(
            _session(
                "codex",
                sid,
                _dir_name((meta or {}).get("cwd")),
                age,
                _session_state(age, role, kind),
                tokens=_codex_tokens(tail),
            )
        )
    return sessions


def _scan_dsh(root, now):
    """DSH（deepseek harness）扫描：sessions/<workspace-slug>/session-<uuid>/session.v3.jsonl.zstd。

    zstd 依赖缺失时整工具静默空（与坏源跳过同一原则）；delegationDepth>0 为
    委派子会话不入列（对齐 zcode subagent 先例）。会话记录无 token 计量字段。
    """
    try:
        import zstandard
    except ImportError:
        return []
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return []
    dctx = zstandard.ZstdDecompressor()
    out = []
    for zf in sessions_dir.glob("*/*/session.v3.jsonl.zstd"):
        try:
            mtime = zf.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age > ACTIVE_WINDOW:
            continue
        try:
            # 解压上限：会话文件无大小约束，整读进内存有隐患；超限只丢尾部预览
            with open(zf, "rb") as fh:
                reader = dctx.stream_reader(fh)
                chunks, remaining = [], _DSH_MAX_BYTES
                while remaining > 0:
                    chunk = reader.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                text = b"".join(chunks).decode("utf-8", errors="ignore")
        except Exception:
            continue
        lines = text.splitlines()
        if not lines:
            continue
        meta = None
        try:
            first = json.loads(lines[0])
            if first.get("type") == "session":
                meta = first
        except Exception:
            meta = None
        if meta and meta.get("delegationDepth"):
            continue
        sid = (meta or {}).get("id") or zf.parent.name
        cwd_name = _dir_name((meta or {}).get("cwd"))
        slug_name = zf.parent.parent.name.strip("-").rsplit("-", 1)[-1] or zf.parent.parent.name
        project = cwd_name or slug_name

        role = kind = None
        preview = preview_role = None
        tool_tail = False
        for line in reversed(lines):
            if ("assistant/message" not in line and "user/message" not in line
                    and '"tool/call"' not in line):
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rtype = rec.get("type")
            if rtype == "tool/call":
                # 倒扫先遇 tool/call 说明它是最新动作；更早的 message 只供预览
                if not tool_tail:
                    tool_tail = True
                continue
            if rtype not in ("assistant/message", "user/message"):
                continue
            data = rec.get("data") or {}
            blocks = (data.get("message") or data).get("content")
            texts = [
                (b.get("text") or "").strip()
                for b in (blocks if isinstance(blocks, list) else [])
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if role is None:
                if rtype == "user/message":
                    role, kind = "user", "user"
                else:
                    role, kind = "assistant", "text"
            if texts and preview is None:
                preview, preview_role = texts[-1], "user" if rtype == "user/message" else "assistant"
            break
        if tool_tail:
            role, kind = "assistant", "tool"
        if role is None:
            role, kind = "assistant", "text"
        if preview and len(preview) > PREVIEW_MAX:
            preview = preview[:PREVIEW_MAX] + "…"
        out.append(
            _session(
                "dsh",
                sid,
                project,
                age,
                _session_state(age, role, kind),
                preview=preview,
                preview_role=preview_role,
            )
        )
    return out


def _scan_hermes(root, now):
    leases = _hermes_leases(root)
    sessions = []
    con = _open_ro(root / "state.db")
    try:
        rows = con.execute(
            "SELECT id, cwd, title, last_activity_at, started_at, end_reason, archived FROM sessions"
        ).fetchall()
    finally:
        con.close()
    for sid, cwd, title, last_act, started, end_reason, archived in rows:
        if archived:
            continue
        act = last_act if last_act is not None else started
        if act is None:
            continue
        age = now - act
        if age > ACTIVE_WINDOW:
            continue
        running = sid in leases or (end_reason is None and age <= RUNNING_WINDOW)
        state = "RUN" if running else ("IDLE" if end_reason is None else "DONE")
        sessions.append(
            _session("hermes", sid, _dir_name(cwd), age, state, running=running, title=title)
        )
    return sessions


# zcode 的子代理会话噪音过大，验收时（票 07）决定不入列
ZCODE_SUBAGENT_PREFIX = "sess_subagent_"


def _scan_zcode(root, now):
    con = _open_ro(root / "cli" / "db" / "db.sqlite")
    try:
        rows = con.execute(
            "SELECT id, directory, title, time_updated, time_archived FROM session"
        ).fetchall()
        todo_rows = con.execute(
            "SELECT session_id, content, status FROM todo ORDER BY session_id, position"
        ).fetchall()
    finally:
        con.close()
    counts = {}
    lists = {}
    for sid, content, status in todo_rows:
        done, total = counts.get(sid, (0, 0))
        counts[sid] = (done + (status == "completed"), total + 1)
        lists.setdefault(sid, []).append({"subject": content, "status": status})
    sessions = []
    for sid, directory, title, upd, archived in rows:
        if archived or upd is None or sid.startswith(ZCODE_SUBAGENT_PREFIX):
            continue
        age = now - upd / 1000.0
        if age > ACTIVE_WINDOW:
            continue
        running = age <= RUNNING_WINDOW
        sessions.append(
            _session(
                "zcode",
                sid,
                _dir_name(directory),
                age,
                "RUN" if running else "DONE",
                tasks=counts.get(sid, (0, 0)),
                title=title,
                tasks_list=lists.get(sid)[:TASKS_LIST_CAP] if sid in lists else None,
            )
        )
    return sessions


def _scan_kimicode(root, now):
    sessions = []
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return sessions
    for ws in sessions_dir.iterdir():
        if not ws.is_dir():
            continue
        for sd in ws.iterdir():
            if not sd.is_dir():
                continue
            state_file = sd / "state.json"
            wire_file = sd / "agents" / "main" / "wire.jsonl"
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                mtimes = [state_file.stat().st_mtime]
                if wire_file.exists():
                    mtimes.append(wire_file.stat().st_mtime)
            except Exception:
                continue
            age = now - max(mtimes)
            if age > ACTIVE_WINDOW:
                continue
            running = age <= RUNNING_WINDOW
            preview, preview_role = (
                _extract_preview(_read_tail(wire_file)) if wire_file.exists() else (None, None)
            )
            sessions.append(
                _session(
                    "kimicode",
                    sd.name,
                    _dir_name(state.get("workDir")),
                    age,
                    "RUN" if running else "DONE",
                    title=state.get("title"),
                    preview=preview,
                    preview_role=preview_role,
                )
            )
    return sessions


def _scan_kimiwork(root, now):
    """降级扫描：只有状态与更新时间，没有标题/项目（正文锁在上游私有存储）。"""
    statuses = json.loads((root / "conversation-statuses.json").read_text(encoding="utf-8"))
    usage = json.loads((root / "conversation-context-usage.json").read_text(encoding="utf-8"))
    sessions = []
    for key, status in statuses.items():
        entry = usage.get(key)
        updated = (entry or {}).get("updatedAt")
        if not updated:
            continue
        age = now - datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
        if age > ACTIVE_WINDOW:
            continue
        running = age <= RUNNING_WINDOW
        state = "RUN" if running else ("DONE" if status == "completed" else "IDLE")
        sessions.append(_session("kimiwork", key.rsplit(":", 1)[-1], "", age, state))
    return sessions


SCANNERS = {
    "qoder": _scan_qoder_sessions,
    "hermes": _scan_hermes,
    "zcode": _scan_zcode,
    "kimicode": _scan_kimicode,
    "kimiwork": _scan_kimiwork,
    "codex": _scan_codex,
    "dsh": _scan_dsh,
}
