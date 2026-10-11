"""副驾领域模型：对话、轮次、事件、许可。纯 Python，无 SDK 依赖。

标识规则（spec 第 6 节）：
- ``conversation_id`` 本地 UUID，与 DSH sessionId 严格分离；
- ``run_id`` 一次明确发送，用于本地防重复（不宣称跨进程 exactly-once）；
- ``runtime_epoch`` 每次运行时重启递增——旧事件/旧许可不作用于新运行时；
- ``seq`` 单调递增事件序号（按会话有序）。
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


class RunState(str, enum.Enum):
    PREPARING = "PREPARING"
    SUBMITTING = "SUBMITTING"
    RUNNING = "RUNNING"
    WAITING_PERMISSION = "WAITING_PERMISSION"
    CANCELLING = "CANCELLING"
    SETTLED = "SETTLED"
    LIMITED = "LIMITED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


#: 终态集合；终态后 Run 不可再转移
TERMINAL_STATES = frozenset(
    {
        RunState.SETTLED,
        RunState.LIMITED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.INTERRUPTED,
    }
)

#: stopReason -> RunState（spec 6.2；refusal/未知保留原值并归 SETTLED 展示"本轮结束：原因"）
STOP_REASON_STATES = {
    "end_turn": RunState.SETTLED,
    "max_tokens": RunState.LIMITED,
    "max_turn_requests": RunState.LIMITED,
    "cancelled": RunState.CANCELLED,
}


def state_for_stop_reason(reason: str) -> RunState:
    return STOP_REASON_STATES.get(reason, RunState.SETTLED)


#: 状态的用户可见文案（不得把"已回复"说成"任务完成"）
STATE_LABELS = {
    RunState.PREPARING: "正在连接",
    RunState.SUBMITTING: "正在发送",
    RunState.RUNNING: "正在执行",
    RunState.WAITING_PERMISSION: "待你确认",
    RunState.CANCELLING: "正在停止",
    RunState.SETTLED: "已回复",
    RunState.LIMITED: "已到本轮上限",
    RunState.CANCELLED: "已停止",
    RunState.FAILED: "执行失败",
    RunState.INTERRUPTED: "连接中断，结果待核实",
}

STATE_LABELS_ZH_DONE = "已回复"  # end_turn 文案；LIMITED/CANCELLED 等单独展示


@dataclass
class Conversation:
    conversation_id: str
    runtime_fingerprint: str
    dsh_home: str
    cwd: str
    created_at: float
    last_activity_at: float
    dsh_session_id: Optional[str] = None
    title: str = ""
    # new/resume 返回的 opaque 配置选项（原样保存，不拼接 provider/model id）
    config_options: list[dict[str, Any]] = field(default_factory=list)
    # 用户在本会话上显式选择的配置（config_id -> option value）
    selected_config: dict[str, Any] = field(default_factory=dict)
    # 会话在当前 runtime epoch 下是否已建立/恢复（未恢复时 submit 需 new/resume）
    session_live: bool = False
    archived: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "cwd": self.cwd,
            "dsh_home": self.dsh_home,
            "title": self.title,
            "dsh_session_id": self.dsh_session_id,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "session_live": self.session_live,
            "config_options": self.config_options,
            "selected_config": self.selected_config,
        }


@dataclass
class Run:
    run_id: str
    conversation_id: str
    state: RunState
    submitted_at: float
    last_event_at: float
    user_text: str = ""
    context_refs: list[str] = field(default_factory=list)
    stop_reason: Optional[str] = None
    settled_at: Optional[float] = None
    error_code: Optional[str] = None
    error_detail: str = ""
    runtime_epoch: int = 0
    prompt_written: bool = False  # False=PREPARING 段取消无需发 ACP cancel

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "state": self.state.value,
            "state_label": STATE_LABELS[self.state],
            "submitted_at": self.submitted_at,
            "last_event_at": self.last_event_at,
            "settled_at": self.settled_at,
            "stop_reason": self.stop_reason,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "prompt_written": self.prompt_written,
            "user_text": self.user_text,
            "context_refs": list(self.context_refs),
        }


@dataclass
class EventRecord:
    seq: int
    run_id: str
    conversation_id: str
    runtime_epoch: int
    kind: str  # 见 EVENT_KINDS
    payload: dict[str, Any]
    observed_at: float


EVENT_KINDS = frozenset(
    {
        "user",  # 本地用户输入（提交时）
        "user_echo",  # runtime 回显的用户消息块
        "assistant",  # 已提交 assistant 文本（可能多次追加）
        "tool",  # 工具生命周期（start/update，含状态）
        "usage",  # 上下文用量
        "config",  # 配置变化
        "permission_request",
        "permission_resolved",
        "state",  # Run 状态转移记录
        "note",  # 系统说明（截断、存储失败等）
    }
)

#: 许可卡片在缺少工具详情时的占位文案；展示层以此识别"不允许含糊允许"
UNKNOWN_TOOL_DETAIL = "操作详情暂不可用"

#: thought 块默认不展示不落库（spec 第 7 节），仅计数
@dataclass
class PermissionOptionView:
    option_id: str
    name: str
    kind: str


@dataclass
class PermissionRecord:
    permission_id: str
    run_id: str
    conversation_id: str
    runtime_epoch: int
    tool_call_id: str
    created_at: float
    tool_title: str = ""
    tool_kind: str = ""
    raw_input: Optional[dict[str, Any]] = None
    options: list[PermissionOptionView] = field(default_factory=list)
    state: str = "pending"  # pending / resolved / expired
    resolved_option: Optional[str] = None  # option_id 或 "__cancelled__"
    resolved_at: Optional[float] = None

    @property
    def pending(self) -> bool:
        return self.state == "pending"

    def to_public(self) -> dict[str, Any]:
        return {
            "permission_id": self.permission_id,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "tool_call_id": self.tool_call_id,
            "tool_title": self.tool_title,
            "tool_kind": self.tool_kind,
            "raw_input": self.raw_input,
            "options": [
                {"option_id": o.option_id, "name": o.name, "kind": o.kind}
                for o in self.options
            ],
            "state": self.state,
            "resolved_option": self.resolved_option,
            "created_at": self.created_at,
        }


# ---- 展示/存储上限（本地记录边界，不是模型 token 限额；spec 第 8 节）----
MAX_ASSISTANT_CHARS = 64 * 1024  # 单条 assistant 可见文本
MAX_TOOL_OUTPUT_CHARS = 4 * 1024  # 单个工具输出摘要
MAX_RUN_VISIBLE_CHARS = 1024 * 1024  # 单轮可见内容合计
MAX_EVENT_TEXT_CHARS = 32 * 1024  # 单事件 payload 文本字段硬上限


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (
        text[:limit] + f"…[已截断，原长 {len(text)} 字符]",
        True,
    )
