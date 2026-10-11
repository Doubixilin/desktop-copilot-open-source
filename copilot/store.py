"""副驾任务业务记录（SQLite）。

与既有 usage log 分离：本库保存用户可见的对话/轮次/事件/许可记录。
单一写入者（控制器线程）+ 锁保护；schema_version 逐版本迁移（迁移前
SQLite 一致性备份；旧程序遇新库明确拒绝启动）。
保留策略：全部活跃对话 + 最近 ``KEEP_TERMINAL_CONVERSATIONS`` 个终态对话；
只清理本地记录，绝不删除 DSH 原始会话。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import models

SCHEMA_VERSION = 2
KEEP_TERMINAL_CONVERSATIONS = 100

#: 结算作业状态机：pending → running → done / (retry→pending) / failed
JOB_STATES = ("pending", "running", "done", "failed")


class CopilotStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                current = int(row["value"])
                if current > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"copilot store schema_version {current} > {SCHEMA_VERSION}"
                        "（本版本程序过旧，拒绝启动——请用匹配版本程序或回退备份）"
                    )
                if current < SCHEMA_VERSION:
                    self._migrate(current)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    runtime_fingerprint TEXT NOT NULL,
                    dsh_home TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    dsh_session_id TEXT,
                    title TEXT DEFAULT '',
                    config_options_json TEXT DEFAULT '[]',
                    selected_config_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    last_activity_at REAL NOT NULL,
                    archived INTEGER DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stop_reason TEXT,
                    submitted_at REAL NOT NULL,
                    settled_at REAL,
                    last_event_at REAL NOT NULL,
                    error_code TEXT,
                    error_detail TEXT DEFAULT '',
                    runtime_epoch INTEGER DEFAULT 0,
                    prompt_written INTEGER DEFAULT 0,
                    user_text TEXT DEFAULT '',
                    context_refs_json TEXT DEFAULT '[]'
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_conv ON runs(conversation_id)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    runtime_epoch INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id, seq)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS permissions (
                    permission_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    runtime_epoch INTEGER NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_title TEXT DEFAULT '',
                    tool_kind TEXT DEFAULT '',
                    raw_input_json TEXT,
                    options_json TEXT DEFAULT '[]',
                    state TEXT DEFAULT 'pending',
                    resolved_option TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    done_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_state_due "
                "ON jobs(state, next_attempt_at)"
            )

    # ---- schema 迁移（票03：v2 新增 jobs 结算作业表） ----

    def _backup(self, tag: str) -> Optional[Path]:
        """迁移前 SQLite 一致性备份（backup API，WAL 安全；失败则中止迁移）。"""
        dst = self.db_path.with_name(f"{self.db_path.name}.bak-{tag}")
        dstconn = sqlite3.connect(str(dst))
        try:
            self._conn.backup(dstconn)
        finally:
            dstconn.close()
        return dst

    def _migrate(self, current: int) -> None:
        """逐版本迁移；开始前一致性备份。禁止一次迁移创建未使用结构。"""
        backup_path = self._backup(f"pre-v{SCHEMA_VERSION}")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            if version == 2:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        payload_json TEXT DEFAULT '{}',
                        state TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL,
                        last_error TEXT DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        done_at REAL
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_state_due "
                    "ON jobs(state, next_attempt_at)"
                )
            else:  # pragma: no cover - 未来版本占位
                raise RuntimeError(f"未知迁移步骤 v{version - 1}->v{version}")
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(version),),
            )
        if backup_path is not None:
            print(f"[store] migrated v{current}->v{SCHEMA_VERSION}, "
                  f"backup={backup_path.name}", flush=True)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- conversations ----
    def upsert_conversation(self, conv: models.Conversation) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, runtime_fingerprint, dsh_home, cwd,
                    dsh_session_id, title, config_options_json, selected_config_json,
                    created_at, last_activity_at, archived
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    dsh_session_id=excluded.dsh_session_id,
                    title=excluded.title,
                    config_options_json=excluded.config_options_json,
                    selected_config_json=excluded.selected_config_json,
                    last_activity_at=excluded.last_activity_at,
                    archived=excluded.archived
                """,
                (
                    conv.conversation_id,
                    conv.runtime_fingerprint,
                    conv.dsh_home,
                    conv.cwd,
                    conv.dsh_session_id,
                    conv.title,
                    json.dumps(conv.config_options, ensure_ascii=False),
                    json.dumps(conv.selected_config, ensure_ascii=False),
                    conv.created_at,
                    conv.last_activity_at,
                    int(conv.archived),
                ),
            )

    def get_conversation(self, conversation_id: str) -> Optional[models.Conversation]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return self._row_to_conversation(row) if row else None

    def list_conversations(self) -> list[models.Conversation]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversations ORDER BY last_activity_at DESC"
            ).fetchall()
        return [self._row_to_conversation(r) for r in rows]

    @staticmethod
    def _row_to_conversation(row: sqlite3.Row) -> models.Conversation:
        return models.Conversation(
            conversation_id=row["conversation_id"],
            runtime_fingerprint=row["runtime_fingerprint"],
            dsh_home=row["dsh_home"],
            cwd=row["cwd"],
            dsh_session_id=row["dsh_session_id"],
            title=row["title"] or "",
            config_options=json.loads(row["config_options_json"] or "[]"),
            selected_config=json.loads(row["selected_config_json"] or "{}"),
            created_at=row["created_at"],
            last_activity_at=row["last_activity_at"],
            archived=bool(row["archived"]),
        )

    # ---- runs ----
    def save_run(self, run: models.Run) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, conversation_id, state, stop_reason, submitted_at,
                    settled_at, last_event_at, error_code, error_detail,
                    runtime_epoch, prompt_written, user_text, context_refs_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state=excluded.state,
                    stop_reason=excluded.stop_reason,
                    settled_at=excluded.settled_at,
                    last_event_at=excluded.last_event_at,
                    error_code=excluded.error_code,
                    error_detail=excluded.error_detail,
                    prompt_written=excluded.prompt_written,
                    user_text=excluded.user_text
                """,
                (
                    run.run_id,
                    run.conversation_id,
                    run.state.value,
                    run.stop_reason,
                    run.submitted_at,
                    run.settled_at,
                    run.last_event_at,
                    run.error_code,
                    run.error_detail,
                    run.runtime_epoch,
                    int(run.prompt_written),
                    run.user_text,
                    json.dumps(run.context_refs, ensure_ascii=False),
                ),
            )

    def get_run(self, run_id: str) -> Optional[models.Run]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row else None

    def runs_for_conversation(self, conversation_id: str) -> list[models.Run]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE conversation_id=? ORDER BY submitted_at",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def all_runs(self) -> list[models.Run]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs").fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> models.Run:
        return models.Run(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            state=models.RunState(row["state"]),
            stop_reason=row["stop_reason"],
            submitted_at=row["submitted_at"],
            settled_at=row["settled_at"],
            last_event_at=row["last_event_at"],
            error_code=row["error_code"],
            error_detail=row["error_detail"] or "",
            runtime_epoch=row["runtime_epoch"] or 0,
            prompt_written=bool(row["prompt_written"]),
            user_text=row["user_text"] or "",
            context_refs=json.loads(row["context_refs_json"] or "[]"),
        )

    # ---- events ----
    def append_event(self, event: models.EventRecord) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO events (run_id, conversation_id, runtime_epoch, kind,
                                    payload_json, observed_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    event.run_id,
                    event.conversation_id,
                    event.runtime_epoch,
                    event.kind,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.observed_at,
                ),
            )
            return cur.lastrowid or 0

    def events_for_conversation(
        self, conversation_id: str, after_seq: int = 0, limit: int = 500,
        tail: bool = False,
    ) -> list[models.EventRecord]:
        """按 seq 升序返回事件。

        tail=True 时取最近 limit 条（长对话恢复加载最近窗口，R5）：
        最早的记录仍留在库中，由调用方决定如何提示截断。
        """
        with self._lock:
            if tail:
                rows = self._conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM events WHERE conversation_id=? AND seq>?
                        ORDER BY seq DESC LIMIT ?
                    ) ORDER BY seq
                    """,
                    (conversation_id, after_seq, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM events WHERE conversation_id=? AND seq>?
                    ORDER BY seq LIMIT ?
                    """,
                    (conversation_id, after_seq, limit),
                ).fetchall()
        return [
            models.EventRecord(
                seq=r["seq"],
                run_id=r["run_id"],
                conversation_id=r["conversation_id"],
                runtime_epoch=r["runtime_epoch"],
                kind=r["kind"],
                payload=json.loads(r["payload_json"]),
                observed_at=r["observed_at"],
            )
            for r in rows
        ]

    def count_events_before(self, conversation_id: str, seq: int) -> int:
        """指定 seq 之前该对话还有多少事件（截断提示用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM events WHERE conversation_id=? AND seq<?",
                (conversation_id, seq),
            ).fetchone()
        return row["c"]

    # ---- permissions ----
    def save_permission(self, perm: models.PermissionRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO permissions (
                    permission_id, run_id, conversation_id, runtime_epoch,
                    tool_call_id, tool_title, tool_kind, raw_input_json,
                    options_json, state, resolved_option, created_at, resolved_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(permission_id) DO UPDATE SET
                    state=excluded.state,
                    resolved_option=excluded.resolved_option,
                    resolved_at=excluded.resolved_at
                """,
                (
                    perm.permission_id,
                    perm.run_id,
                    perm.conversation_id,
                    perm.runtime_epoch,
                    perm.tool_call_id,
                    perm.tool_title,
                    perm.tool_kind,
                    json.dumps(perm.raw_input, ensure_ascii=False)
                    if perm.raw_input is not None
                    else None,
                    json.dumps(
                        [
                            {"option_id": o.option_id, "name": o.name, "kind": o.kind}
                            for o in perm.options
                        ],
                        ensure_ascii=False,
                    ),
                    perm.state,
                    perm.resolved_option,
                    perm.created_at,
                    perm.resolved_at,
                ),
            )

    def get_permission(self, permission_id: str) -> Optional[models.PermissionRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM permissions WHERE permission_id=?",
                (permission_id,),
            ).fetchone()
        return self._row_to_permission(row) if row else None

    def pending_permissions(self) -> list[models.PermissionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM permissions WHERE state='pending'"
            ).fetchall()
        return [self._row_to_permission(r) for r in rows]

    @staticmethod
    def _row_to_permission(row: sqlite3.Row) -> models.PermissionRecord:
        raw = json.loads(row["raw_input_json"]) if row["raw_input_json"] else None
        opts = [
            models.PermissionOptionView(
                option_id=o["option_id"], name=o["name"], kind=o["kind"]
            )
            for o in json.loads(row["options_json"] or "[]")
        ]
        return models.PermissionRecord(
            permission_id=row["permission_id"],
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            runtime_epoch=row["runtime_epoch"],
            tool_call_id=row["tool_call_id"],
            tool_title=row["tool_title"] or "",
            tool_kind=row["tool_kind"] or "",
            raw_input=raw,
            options=opts,
            state=row["state"],
            resolved_option=row["resolved_option"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    # ---- 维护 ----
    def mark_unsettled_interrupted(self) -> int:
        """启动恢复：未结算轮次标 INTERRUPTED，不重发。返回受影响行数。"""
        unsettled = [
            "PREPARING",
            "SUBMITTING",
            "RUNNING",
            "WAITING_PERMISSION",
            "CANCELLING",
        ]
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"""
                UPDATE runs SET state='INTERRUPTED',
                       error_code=COALESCE(error_code,'interrupted_by_restart'),
                       settled_at=?
                WHERE state IN ({",".join("?" * len(unsettled))})
                """,
                (models.now(), *unsettled),
            )
            # 运行时已重启，待决许可全部过期
            self._conn.execute(
                "UPDATE permissions SET state='expired' WHERE state='pending'"
            )
            return cur.rowcount or 0

    def prune(self, keep_terminal: int = KEEP_TERMINAL_CONVERSATIONS) -> int:
        """终态对话超量清理；活跃对话与全部事件上限见事件截断。"""
        with self._lock, self._conn:
            convs = self._conn.execute(
                "SELECT conversation_id FROM conversations ORDER BY last_activity_at DESC"
            ).fetchall()
            active_ids = {
                r["conversation_id"]
                for r in self._conn.execute(
                    """
                    SELECT DISTINCT conversation_id FROM runs
                    WHERE state NOT IN ('SETTLED','LIMITED','CANCELLED','FAILED','INTERRUPTED')
                    """
                ).fetchall()
            }
            terminal_order = [
                r["conversation_id"]
                for r in convs
                if r["conversation_id"] not in active_ids
            ]
            to_remove = terminal_order[keep_terminal:]
            for cid in to_remove:
                self._conn.execute(
                    "DELETE FROM events WHERE conversation_id=?", (cid,)
                )
                self._conn.execute(
                    "DELETE FROM permissions WHERE conversation_id=?", (cid,)
                )
                self._conn.execute(
                    "DELETE FROM runs WHERE conversation_id=?", (cid,)
                )
                self._conn.execute(
                    "DELETE FROM conversations WHERE conversation_id=?", (cid,)
                )
            return len(to_remove)

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for table in ("conversations", "runs", "events", "permissions", "jobs"):
                out[table] = self._conn.execute(
                    f"SELECT COUNT(*) c FROM {table}"
                ).fetchone()["c"]
        return out

    # ---- 结算作业（票03：归档等后台工作，失败可观察且有界重试） ----

    def enqueue_job(self, job_type: str, dedupe_key: str,
                    payload: Optional[dict] = None) -> Optional[int]:
        """幂等入队。同 dedupe_key 在 pending/running/done 则跳过；failed 复活。

        返回 job id；跳过返回 None。dedupe 语义按调用方（如 archive:<run_id>）。
        """
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id, state FROM jobs WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if row is not None:
                if row["state"] in ("pending", "running", "done"):
                    return None
                self._conn.execute(
                    "UPDATE jobs SET state='pending', attempts=0, "
                    "next_attempt_at=?, last_error='', updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                return int(row["id"])
            cur = self._conn.execute(
                """
                INSERT INTO jobs (type, dedupe_key, payload_json, state,
                                  attempts, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (job_type, dedupe_key,
                 json.dumps(payload or {}, ensure_ascii=False), now, now, now),
            )
            return int(cur.lastrowid or 0)

    def claim_due_jobs(self, limit: int = 5, *,
                       now: Optional[float] = None) -> list[dict]:
        """领取到期作业（pending 且 next_attempt_at<=now）并标记 running。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state='pending' AND next_attempt_at<=? "
                "ORDER BY next_attempt_at LIMIT ?",
                (now, limit),
            ).fetchall()
            out = []
            for r in rows:
                self._conn.execute(
                    "UPDATE jobs SET state='running', updated_at=? WHERE id=?",
                    (now, r["id"]),
                )
                out.append({
                    "id": r["id"], "type": r["type"], "dedupe_key": r["dedupe_key"],
                    "payload": json.loads(r["payload_json"] or "{}"),
                    "attempts": r["attempts"],
                })
            return out

    def finish_job(self, job_id: int, *, ok: bool, error: str = "",
                   max_attempts: int = 5, backoff_base: float = 30.0) -> str:
        """结算作业：done / retry(退避重试) / failed(超过次数上限)。"""
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT attempts FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return "missing"
            if ok:
                self._conn.execute(
                    "UPDATE jobs SET state='done', last_error='', done_at=?, "
                    "updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
                return "done"
            attempts = int(row["attempts"]) + 1
            if attempts >= max_attempts:
                self._conn.execute(
                    "UPDATE jobs SET state='failed', attempts=?, last_error=?, "
                    "updated_at=? WHERE id=?",
                    (attempts, error[:2000], now, job_id),
                )
                return "failed"
            self._conn.execute(
                "UPDATE jobs SET state='pending', attempts=?, last_error=?, "
                "next_attempt_at=?, updated_at=? WHERE id=?",
                (attempts, error[:2000],
                 now + backoff_base * (2 ** (attempts - 1)), now, job_id),
            )
            return "retry"

    def list_jobs(self, state: Optional[str] = None,
                  limit: int = 50) -> list[dict]:
        """作业可见性：失败/重试可观察（诊断面，不含敏感正文）。"""
        with self._lock:
            if state:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE state=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [{
            "id": r["id"], "type": r["type"], "dedupe_key": r["dedupe_key"],
            "state": r["state"], "attempts": r["attempts"],
            "next_attempt_at": r["next_attempt_at"],
            "last_error": r["last_error"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "done_at": r["done_at"],
        } for r in rows]

    def recover_stale_running(self) -> int:
        """F09 启动恢复：worker 崩溃遗留的 running 作业重置为 pending 立即到期。

        单进程单 worker：进程重启后不存在他人持有的 running，全部视为失效。
        """
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET state='pending', next_attempt_at=?, updated_at=? "
                "WHERE state='running'",
                (now, now),
            )
            return int(cur.rowcount or 0)

    # ---- 通用 meta 键值（运行时游标等；schema 键除外） ----

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
