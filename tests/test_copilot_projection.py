"""06 票：/deck 权威投影、去重与边界（C14/C15）。"""
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from copilot import projection  # noqa: E402

NOW = 1_000_000.0
DEFAULT_HOME = "C:/example/user/.dsh"


def snap(runs, convs):
    return {"runs": runs, "conversations": convs}


def conv(cid, sid=None, title="项目A", home=None):
    return {
        "conversation_id": cid,
        "dsh_session_id": sid,
        "title": title,
        "cwd": "D:/x",
        "dsh_home": home if home is not None else DEFAULT_HOME,
        "last_activity_at": NOW - 30,
    }


def run(cid, state, settled=False, event_age=10, text="整理文件"):
    return {
        "run_id": "r-" + cid,
        "conversation_id": cid,
        "state": state,
        "submitted_at": NOW - 60,
        "last_event_at": NOW - event_age,
        "settled_at": (NOW - event_age) if settled else None,
        "user_text": text,
    }


def scanner_dsh(sid, state="RUN"):
    return {"tool": "dsh", "id": sid, "state": state, "age": 30, "title": "外部"}


def scanner_codex(sid="cx1"):
    return {"tool": "codex", "id": sid, "state": "RUN", "age": 100, "title": "外 codex"}


# ---- 状态映射与细态 ----

@pytest.mark.parametrize("state,compat", [
    ("PREPARING", "RUN"), ("SUBMITTING", "RUN"), ("RUNNING", "RUN"),
    ("CANCELLING", "RUN"), ("WAITING_PERMISSION", "CONFIRM"),
    ("SETTLED", "IDLE"), ("LIMITED", "IDLE"), ("CANCELLED", "IDLE"),
    ("FAILED", "IDLE"), ("INTERRUPTED", "IDLE"),
])
def test_compat_state_mapping(state, compat):
    out = projection.owned_sessions(
        snap([run("c1", state)], [conv("c1", sid="s1")]), now=NOW
    )
    assert len(out) == 1
    assert out[0]["state"] == compat
    assert out[0]["run_state"] == state
    assert out[0]["source"] == "desktop-copilot"
    assert out[0]["can_control"] is True


def test_settled_shows_reply_not_done():
    out = projection.owned_sessions(
        snap([run("c1", "SETTLED", settled=True)], [conv("c1", sid="s1")]), now=NOW
    )
    assert out[0]["preview"] == "已回复"  # 不写"任务完成"


def test_whitelist_fields_and_no_leak():
    out = projection.owned_sessions(
        snap([run("c1", "RUNNING")], [conv("c1", sid="s1")]), now=NOW
    )
    keys = set(out[0].keys())
    assert {"source", "run_state", "last_event_at", "can_control"} <= keys
    # 不夹带 home/绝对路径/完整任务对象
    assert not any(k in keys for k in ("dsh_home", "cwd", "workspace_path"))
    assert "events" not in keys


# ---- 保留与淘汰 ----

def test_active_and_waiting_not_expired():
    out = projection.owned_sessions(
        snap(
            [run("c1", "RUNNING", event_age=3600), run("c2", "WAITING_PERMISSION", event_age=3600)],
            [conv("c1", sid="s1"), conv("c2", sid="s2")],
        ),
        now=NOW,
    )
    assert len(out) == 2  # 长时间运行/等许可不消失（C14）


def test_terminal_visible_10min_then_fades():
    fresh = projection.owned_sessions(
        snap([run("c1", "SETTLED", settled=True, event_age=100)],
             [conv("c1", sid="s1")]), now=NOW
    )
    assert len(fresh) == 1
    old = projection.owned_sessions(
        snap([run("c1", "SETTLED", settled=True, event_age=3600)],
             [conv("c1", sid="s1")]), now=NOW
    )
    assert old == []  # 超 10 分钟淡出（本地记录仍保留在副驾）


# ---- 去重（精确身份） ----

def test_exact_duplicate_removed_owned_wins():
    owned = projection.owned_sessions(
        snap([run("c1", "RUNNING")], [conv("c1", sid="s1")]), now=NOW
    )
    merged = projection.merge_sessions(
        [scanner_dsh("s1"), scanner_codex()], owned,
        scanner_dsh_namespace="C:/example/user/.dsh",
    )
    dsh_rows = [s for s in merged if s["tool"] == "dsh"]
    assert len(dsh_rows) == 1
    assert dsh_rows[0]["source"] == "desktop-copilot"
    assert any(s["tool"] == "codex" for s in merged)  # 外部会话并存
    # scanner 行不夹带内部 namespace 元数据
    assert all(not str(k).startswith("_") for s in merged for k in s)


def test_same_id_different_namespace_kept():
    owned = projection.owned_sessions(
        snap([run("c1", "RUNNING")],
             [conv("c1", sid="s1", home="D:/iso-home")]),
        now=NOW,
    )
    merged = projection.merge_sessions(
        [scanner_dsh("s1")], owned,
        scanner_dsh_namespace="C:/example/user/.dsh",  # copilot 独立 home ≠ 此 namespace
    )
    dsh_rows = [s for s in merged if s["tool"] == "dsh"]
    assert len(dsh_rows) == 2  # 同 id 不同 namespace 不合并


def test_controller_none_passthrough():
    assert projection.project_deck([scanner_codex()], None) == [scanner_codex()]


# ---- server 集成（真实 handler：裁剪与缓存） ----

def test_deck_projection_in_server(tmp_path, monkeypatch):
    """真实 deck_state：owned 并入 + 未认证裁剪不泄露 + 白名单字段保留。"""
    import server as qd_server
    from copilot import runtime_host

    monkeypatch.setenv("QD_COPILOT_DIR", str(tmp_path / "copilot-dir"))
    # 注入桩控制器（绕过配置文件）
    class _StubController:
        def snapshot(self):
            return snap(
                [run("c1", "RUNNING")],
                [{
                    "conversation_id": "c1", "dsh_session_id": "owned-s1",
                    "title": "秘密项目名", "cwd": "D:/secret",
                    "dsh_home": "D:/iso-home", "last_activity_at": NOW,
                }],
            )

    monkeypatch.setattr(runtime_host, "get_controller",
                        lambda: (_StubController(), None))
    # 重置 deck 缓存以便立即重建
    qd_server._deck_cache.ts = 0.0
    deck = qd_server.deck_state()
    owned_rows = [s for s in deck["sessions"] if s.get("source") == "desktop-copilot"]
    assert owned_rows and owned_rows[0]["run_state"] == "RUNNING"
    # 未认证裁剪语义沿用现有键集合：title/project/preview/current_task 掉，
    # 白名单四字段留（模拟 handler 的 key 过滤）
    drop = ("title", "project", "preview", "preview_role", "tasks", "current_task")
    trimmed = [{k: v for k, v in s.items() if k not in drop} for s in deck["sessions"]]
    owned_trimmed = [s for s in trimmed if s.get("source") == "desktop-copilot"]
    assert owned_trimmed
    assert {"source", "run_state", "last_event_at", "can_control"} <= set(owned_trimmed[0])
    assert "cwd" not in owned_trimmed[0] and "dsh_home" not in owned_trimmed[0]
