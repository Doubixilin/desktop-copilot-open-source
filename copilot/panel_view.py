"""副驾原生视图（Tk）：宿主无关，运行在搜索面板窗口线程内。

职责边界（spec 第 5 节 / 04 票）：
- 只在 Tk 线程触碰控件；与控制器经 snapshot()/take_notifications()/events()
  交互，绝不直接碰 ACP connection。
- 草稿、选中项目、运行中任务在收起（ESC/外部失焦）后保留；收起不取消、
  不批准许可。
- 发送以按钮为准；Enter 换行；Ctrl+Enter 默认关闭（中文 IME 实机验收后
  才可开）。
- 失焦判断用顶层窗口内外焦点，点击面板内部按钮/滚动条/选择框不收起；
  打开目录选择对话框期间不触发收起。
"""
from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont, ttk
from tkinter import filedialog
from typing import Optional

from .models import UNKNOWN_TOOL_DETAIL

BG = "#000000"
WHITE = "#ffffff"
FG = "#d6d6d6"
DIM = "#616161"
LINE = "#2c2c2c"
SURFACE = "#14161a"
BUTTON = "#1d2229"
ACCENT = "#7ec8ff"   # 状态/系统行
WARN = "#ffd166"     # 待许可
ERROR = "#ff6b6b"    # 失败

TERMINAL_STATES = {"SETTLED", "LIMITED", "CANCELLED", "FAILED", "INTERRUPTED"}

TRANSCRIPT_MAX_CHARS = 240_000  # Text 控件总长上限（超出裁头，防无界增长）

STATE_TEXT = {
    "PREPARING": "正在连接…",
    "SUBMITTING": "正在发送…",
    "RUNNING": "正在执行",
    "WAITING_PERMISSION": "待你确认",
    "CANCELLING": "正在停止…",
    "SETTLED": "已回复",
    "LIMITED": "本轮已到上限",
    "CANCELLED": "已停止",
    "FAILED": "失败",
    "INTERRUPTED": "中断，结果待核实",
}

# 过程类事件默认折叠（elide）：瞬态状态/工具过程/回显/用量/说明；终态、
# 许可、用户与回复保持可见（人工验收反馈：过程行累积占篇幅）
_TRANSIENT_RUN_STATES = {"PREPARING", "SUBMITTING", "RUNNING", "CANCELLING"}
_PROC_KINDS = {"tool", "usage", "user_echo", "note", "config"}

# Markdown-lite 内联记号：**加粗**、`行内代码`、[文字](链接)
_MD_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))")


def _pick_family(parent, *preferred: str) -> str:
    """按优先级取本机可用的字族（人工验收反馈：想要 macOS/ZCode 风格
    UI 字体而非壁纸同款 Decima Mono）；全部缺失时退回最后一项由 Tk 兜底。"""
    try:
        families = set(parent.tk.call("font", "families"))
    except Exception:
        families = set()
    for name in preferred[:-1]:
        if name in families:
            return name
    return preferred[-1]


class CopilotView:
    """副驾 tab 的全部可视与交互；宿主窗口提供 parent 与字体。"""

    def __init__(self, parent: tk.Misc, font: tkfont.Font, small_font: tkfont.Font,
                 get_controller, on_collapse=None):
        self._get_controller = get_controller  # () -> (controller|None, reason|None)
        self._on_collapse = on_collapse
        self._small_font = small_font
        self.controller = None
        self._controller_error: Optional[str] = None

        self.project_dir: Optional[str] = None
        self.conversation_id: Optional[str] = None
        self._rendered_seq = 0
        self._last_active_run = None
        self._modal_guard = False  # 目录对话框等模态操作期间不判失焦
        # 慢操作（恢复会话/会话快照）在工作线程执行，结果经队列回 Tk 线程
        # （R8：不在 Tk 线程同步等待 runtime/HTTP）
        self._async_done: "queue.Queue[tuple]" = queue.Queue()
        self._async_busy: dict[str, bool] = {}

        self.frame = tk.Frame(parent, bg=BG)
        pad = round(4)
        # 副驾视图用 UI 字体（区别于壁纸同款 Decima Mono 的面板 chrome）：
        # 优先选含中文字形的 UI 字体。Segoe UI Variable Text 在本机的
        # Tk/ttk 标题行渲染为星号，不能依赖系统字体链接补齐中文。
        # cget("size") 保留创建时的负数像素值；actual("size") 会换算成
        # points（27px 在本机回报 20），直接拿来再设负值会越改越小。
        _base_px = max(12, abs(int(font.cget("size"))))
        _ui = _pick_family(parent, "Microsoft YaHei UI", "Microsoft YaHei",
                           "Segoe UI")
        _code = _pick_family(parent, "Cascadia Mono", "Consolas", "Courier New")
        ui_px = round(_base_px * 1.25)
        small_px = max(13, round(ui_px * 0.82))
        self.ui_font = tkfont.Font(root=parent, family=_ui, size=-ui_px)
        self.ui_bold_font = tkfont.Font(
            root=parent, family=_ui, size=-ui_px, weight="bold"
        )
        small_font = tkfont.Font(root=parent, family=_ui, size=-small_px)
        self._small_font = small_font
        self.code_font = tkfont.Font(root=parent, family=_code,
                                     size=-max(13, ui_px - 2))
        self._show_proc = False  # 过程行默认折叠
        self._md_in_fence = False
        self._md_pending = ""
        self._md_pending_start = None
        self._md_table_header = None
        self._md_table_active = None
        # ---- 项目/会话行 ----
        top = tk.Frame(self.frame, bg=BG)
        self.project_btn = tk.Button(
            top, text="选择项目…", fg=WHITE, bg=BUTTON, activebackground=LINE,
            activeforeground=WHITE, relief="solid", bd=1, font=small_font,
            command=self._pick_project, takefocus=0, cursor="hand2",
        )
        self.project_btn.pack(side="left")
        self.conv_label = tk.Label(top, text="未开始对话", fg=DIM, bg=BG,
                                   font=small_font, anchor="w")
        self.conv_label.pack(side="left", padx=(8, 0))
        self.new_chat_btn = tk.Button(
            top, text="新建对话", fg=WHITE, bg=BUTTON, activebackground=LINE,
            activeforeground=WHITE, relief="solid", bd=1, font=small_font,
            command=self._new_conversation, takefocus=0, cursor="hand2",
        )
        self.new_chat_btn.pack(side="right")

        # ---- 会话选择下拉（多会话切换）----
        convrow = tk.Frame(self.frame, bg=BG)
        combo_style = ttk.Style(parent)
        if combo_style.theme_use() != "clam":
            combo_style.theme_use("clam")
        combo_style.configure(
            "Copilot.TCombobox", fieldbackground=SURFACE, background=BUTTON,
            foreground=FG, arrowcolor=FG, bordercolor=LINE,
            lightcolor=LINE, darkcolor=LINE, padding=3,
        )
        combo_style.map(
            "Copilot.TCombobox",
            fieldbackground=[("readonly", SURFACE)],
            foreground=[("readonly", FG)],
            selectbackground=[("readonly", SURFACE)],
            selectforeground=[("readonly", FG)],
        )
        self.conv_combo = ttk.Combobox(
            convrow, state="readonly", font=small_font, takefocus=False,
            style="Copilot.TCombobox",
        )
        self.conv_combo.pack(side="left", fill="x", expand=True, pady=(2, 0))
        self.conv_combo.bind("<<ComboboxSelected>>", lambda e: self._switch_conversation())
        self.resume_btn = tk.Button(
            convrow, text="恢复会话", fg=WHITE, bg=BUTTON, activebackground=LINE,
            activeforeground=WHITE, relief="solid", bd=1, font=small_font,
            command=self._resume_conversation, takefocus=0, cursor="hand2",
        )
        self.resume_btn.pack(side="right", padx=(6, 0), pady=(2, 0))
        self.snapshot_btn = tk.Button(
            convrow, text="会话快照", fg=WHITE, bg=BUTTON, activebackground=LINE,
            activeforeground=WHITE, relief="solid", bd=1, font=small_font,
            command=self._append_sessions_snapshot, takefocus=0, cursor="hand2",
        )
        self.snapshot_btn.pack(side="right", pady=(2, 0))

        # ---- 转录区（只读 Text + 滚动条）----
        mid = tk.Frame(self.frame, bg=BG)

        # ---- 输入区 ----
        self.input_border = tk.Frame(self.frame, bg=LINE, padx=1, pady=1)
        self.input = tk.Text(
            self.input_border, fg=FG, bg=SURFACE, font=self.ui_font, relief="flat",
            wrap="word", height=3, insertbackground=FG, takefocus=True,
            selectbackground=LINE, selectforeground=WHITE, padx=8, pady=5,
        )
        self.input.pack(fill="both", expand=True)
        self.input.bind("<Escape>", self._esc)
        self.input.bind("<Return>", self._enter_newline)
        # Ctrl+Enter 默认不启用发送：需中文 IME 实机验收（04 票验收项）

        # ---- 按钮与状态行 ----
        bottom = tk.Frame(self.frame, bg=BG)
        self.send_btn = tk.Button(
            bottom, text="发送", fg=BG, bg=FG, activebackground=WHITE,
            activeforeground=BG, relief="flat", font=small_font,
            command=self._send, takefocus=0, cursor="hand2",
        )
        self.send_btn.pack(side="left")
        self.stop_btn = tk.Button(
            bottom, text="停止", fg=ERROR, bg=BUTTON, activebackground=LINE,
            activeforeground=ERROR, relief="solid", bd=1, font=small_font,
            command=self._stop, takefocus=0, cursor="hand2",
        )
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.proc_btn = tk.Button(
            bottom, text="▸ 过程", fg=FG, bg=BUTTON, activebackground=LINE,
            activeforeground=WHITE, relief="solid", bd=1, font=small_font,
            command=self._toggle_proc, takefocus=0, cursor="hand2",
        )
        self.proc_btn.pack(side="left", padx=(6, 0))
        self.status = tk.Label(bottom, text="", fg=DIM, bg=BG, font=small_font,
                               anchor="e")
        self.status.pack(side="right")

        # ---- 上下文引用 chips（05 票扩展，先支持文件路径）----
        self.chips_row = tk.Frame(self.frame, bg=BG)
        self._chips: list[str] = []

        # ---- 许可卡片（有待决许可时显示）----
        self.perm_card = tk.Frame(self.frame, bg=BG)
        self._perm_frame_rendered = False
        self._perm_rendered_id = None
        self._perm_rendered_sig = None

        # 布局（人工验收 B-1）：输入区/发送行固定窗口底部并**先于**转录区
        # 打包——pack 高度不足时丢弃的是打包最晚的从件，输入/发送因此永不
        # 因布局竞争被裁掉；空间不够时被压缩的只会是中部转录区。
        # 自底向上视觉顺序：发送行 → 输入区 → 许可卡片(动态) → chips → 转录区。
        top.pack(fill="x", pady=(pad, 0))
        convrow.pack(fill="x")
        bottom.pack(side="bottom", fill="x", pady=(0, pad))
        self.input_border.pack(side="bottom", fill="x", pady=(pad, 0))
        self.chips_row.pack(side="bottom", fill="x")
        mid.pack(fill="both", expand=True, pady=(pad, 0))
        self.transcript = tk.Text(
            mid, fg=FG, bg=BG, font=self.ui_font, relief="flat", wrap="word",
            state="disabled", height=10, takefocus=0,
            selectbackground=LINE, selectforeground=WHITE,
        )
        self.scroll = tk.Scrollbar(
            mid, command=self.transcript.yview, takefocus=0, width=8,
            bg="#24282e", activebackground="#39414b", troughcolor=SURFACE,
            relief="flat", bd=0, highlightthickness=0,
        )
        self.transcript.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.transcript.pack(side="left", fill="both", expand=True)
        # 对话区分度（人工验收反馈）：用户=白+粗体前缀，AI=浅灰；过程行
        # 折叠（proc elide）；MD-lite 加粗/代码块/标题单独 tag
        for tag, color in (
            ("user", WHITE), ("assistant", "#cfcfcf"), ("tool", DIM),
            ("note", DIM), ("state", DIM), ("warn", WARN), ("error", ERROR),
            ("dim", DIM), ("proc", DIM),
        ):
            self.transcript.tag_configure(tag, foreground=color)
        self.transcript.tag_configure("user", font=self.ui_bold_font)
        self.transcript.tag_configure(
            "md_bold", foreground="#eaeaea", font=self.ui_bold_font
        )
        self.transcript.tag_configure(
            "md_head", foreground=WHITE, font=self.ui_bold_font
        )
        self.transcript.tag_configure(
            "md_code", foreground=ACCENT, font=self.code_font, background="#141414"
        )
        for tag in ("tool", "note", "state", "dim", "proc", "warn"):
            self.transcript.tag_configure(tag, font=small_font)
        self.transcript.tag_configure("proc", elide=not self._show_proc)

        # 内部失焦守卫：面板内部控件间切换不收起
        for w in (self.input, self.transcript, self.conv_combo):
            w.bind("<FocusOut>", lambda e: self._focus_maybe_left(), add="+")
        self._collapse_check_job = None

    # ------------------------------------------------------------------
    # 视图生命周期
    # ------------------------------------------------------------------
    def show(self):
        self.frame.pack(fill="both", expand=True)
        self._ensure_controller()
        self.refresh(full=True)
        self.input.focus_set()
        # 首次进入时窗口重映射/扩高可能晚于 focus_set（B-1：焦点滞留搜索
        # 输入）；延迟复核一次，视图已隐藏则跳过
        self.frame.after(150, self._refocus_input)
        if getattr(self, "_poll_job", None) is None:
            self._schedule_poll()

    def _refocus_input(self):
        try:
            if self.frame.winfo_manager() and self.frame.winfo_ismapped():
                self.input.focus_set()
        except Exception:
            pass

    def hide(self):
        """收起：保留草稿/项目/任务（调用方负责窗口层切换）。"""
        self.frame.pack_forget()

    def destroy(self):
        if getattr(self, "_poll_job", None) is not None:
            try:
                self.frame.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        self.frame.destroy()

    def contains(self, widget) -> bool:
        """widget（控件或路径串）是否在本视图子树内；窗口层判定点击归属用。"""
        w = widget
        if isinstance(w, str):
            try:
                w = self.frame.nametowidget(w)
            except Exception:
                return False
        while w is not None:
            if w is self.frame:
                return True
            parent = w.winfo_parent()
            if not parent:
                return False
            try:
                w = w.nametowidget(parent)
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------
    # 控制器
    # ------------------------------------------------------------------
    def _ensure_controller(self):
        if self.controller is None and self._controller_error is None:
            controller, error = self._get_controller()
            if controller is None:
                self._controller_error = error or "copilot 不可用"
            else:
                self.controller = controller
        if self.controller is None:
            self._set_status(f"DSH NOT READY: {self._controller_error}", ERROR)
            self.send_btn.configure(state="disabled")
            return False
        self.send_btn.configure(state="normal")
        return True

    # ------------------------------------------------------------------
    # 轮询刷新
    # ------------------------------------------------------------------
    def _schedule_poll(self):
        self._poll_job = self.frame.after(150, self._poll)

    def _poll(self):
        try:
            self._drain_async_done()
            if self.controller is not None:
                self.controller.take_notifications()
                self.refresh(full=False)
            self._schedule_poll()
        except Exception:
            traceback.print_exc()
            self._schedule_poll()

    def _drain_async_done(self):
        """工作线程完成的结果回 Tk 线程处理（R8）。"""
        while True:
            try:
                kind, payload = self._async_done.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "resume":
                    self._resume_done(*payload)
                elif kind == "snapshot":
                    self._snapshot_done(*payload)
            except Exception:
                traceback.print_exc()

    def refresh(self, full: bool = False):
        if self.controller is None:
            return
        snap = self.controller.snapshot()
        if full:
            self._rendered_seq = 0
            self._reset_markdown_stream()
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.configure(state="disabled")
            self._sync_conversation_choice(snap)
        active = snap.get("active_run")
        self._render_new_events()
        self._render_perm_card(snap)
        self._render_controls(active, snap)
        if active and active["run_id"] != self._last_active_run:
            self._last_active_run = active["run_id"]

    # ---- 转录 ----
    def _render_new_events(self):
        if not self.conversation_id:
            return
        events = self.controller.events(self.conversation_id, after_seq=self._rendered_seq)
        if not events:
            return
        self.transcript.configure(state="normal")
        for ev in events:
            if ev.kind == "assistant":
                self._append_markdown_chunk(ev.payload.get("text", ""))
            else:
                self._finish_markdown_stream()
                text, tag = self._format_event(ev)
                if text:
                    if ev.kind == "user":
                        self._insert_turn_gap()
                    if self._is_proc_event(ev.kind, ev.payload):
                        tag = "proc"  # 过程行：暗色小字，默认折叠可切换
                    self.transcript.insert("end", text + "\n", tag)
            self._rendered_seq = max(self._rendered_seq, ev.seq)
        # 防无界增长：超限裁头
        if int(self.transcript.index("end-1c").split(".")[0]) > 4000:
            self.transcript.delete("1.0", f"{4000 // 2}.0")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    @staticmethod
    def _is_proc_event(kind: str, payload: dict) -> bool:
        """过程类事件（默认折叠）：瞬态状态/工具过程/回显/用量/说明性 note。

        终态（已回复/失败/中断/待确认）与许可事件保持可见——它们是结论
        与需要用户行动的行。
        """
        if kind in _PROC_KINDS:
            return True
        return kind == "state" and payload.get("state") in _TRANSIENT_RUN_STATES

    def _insert_turn_gap(self):
        # 用户消息前空一行，形成回合分隔（转录非空时才加）
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert("end", "\n", "dim")

    def _toggle_proc(self):
        self._show_proc = not self._show_proc
        self.transcript.configure(state="normal")
        self.transcript.tag_configure("proc", elide=not self._show_proc)
        self.transcript.configure(state="disabled")
        self.proc_btn.configure(text=("▾ 过程" if self._show_proc else "▸ 过程"))

    def _reset_markdown_stream(self):
        self._md_pending = ""
        self._md_pending_start = None
        self._md_table_header = None
        self._md_table_active = None
        self._md_in_fence = False

    def _append_markdown_chunk(self, text: str):
        """流式回复可把 Markdown 标记或表格行拆在任意 chunk 边界。"""
        if self._md_pending_start is not None:
            self.transcript.delete(self._md_pending_start, "end")
        self._md_pending += text or ""
        while "\n" in self._md_pending:
            line, self._md_pending = self._md_pending.split("\n", 1)
            self._render_md_line(line.rstrip("\r"))
        self._md_pending_start = self.transcript.index("end-1c")
        # 末行尚未结束时先显示原文；下个 chunk 只重画这一行，不重画整篇。
        if self._md_pending:
            self.transcript.insert("end", self._md_pending, "assistant")

    def _finish_markdown_stream(self):
        if self._md_pending_start is not None:
            self.transcript.delete(self._md_pending_start, "end")
        if self._md_table_header is not None:
            self._render_plain_md_line(self._md_table_header)
        if self._md_pending:
            self._render_md_line(self._md_pending)
        self._reset_markdown_stream()

    @staticmethod
    def _table_cells(line: str):
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            return None
        return [cell.strip().replace(r"\|", "|")
                for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]

    def _render_md_line(self, line: str):
        cells = None if self._md_in_fence else self._table_cells(line)
        if self._md_table_header is not None:
            headers = self._table_cells(self._md_table_header)
            if (cells and headers and len(cells) == len(headers)
                    and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)):
                self._md_table_active = headers
                self._md_table_header = None
                self.transcript.insert("end", "表格\n", "md_head")
                return
            self._render_plain_md_line(self._md_table_header)
            self._md_table_header = None
        if self._md_table_active is not None:
            if cells and len(cells) == len(self._md_table_active):
                for i, (header, value) in enumerate(zip(self._md_table_active, cells)):
                    if i:
                        self.transcript.insert("end", "  ·  ", "dim")
                    self.transcript.insert("end", f"{header}：", "md_bold")
                    self._insert_inline(value, "assistant")
                self.transcript.insert("end", "\n", "assistant")
                return
            self._md_table_active = None
        if cells and len(cells) >= 2:
            self._md_table_header = line
            return
        self._render_plain_md_line(line)

    def _render_plain_md_line(self, line: str):
        stripped = line.strip()
        if stripped.startswith("```"):
            self._md_in_fence = not self._md_in_fence
            return
        if self._md_in_fence:
            self.transcript.insert("end", line + "\n", "md_code")
            return
        if not stripped:
            self.transcript.insert("end", "\n", "assistant")
            return
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            self._insert_inline(heading.group(1), "md_head")
            self.transcript.insert("end", "\n", "md_head")
            return
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            self.transcript.insert("end", "· ", "dim")
            self._insert_inline(bullet.group(1), "assistant")
            self.transcript.insert("end", "\n", "assistant")
            return
        self._insert_inline(stripped, "assistant")
        self.transcript.insert("end", "\n", "assistant")

    def _insert_inline(self, line: str, base_tag: str):
        pos = 0
        for m in _MD_INLINE.finditer(line):
            if m.start() > pos:
                self.transcript.insert("end", line[pos:m.start()], base_tag)
            token = m.group(0)
            if token.startswith("**"):
                self.transcript.insert("end", token[2:-2], "md_bold")
            elif token.startswith("`"):
                self.transcript.insert("end", token[1:-1], "md_code")
            else:  # [文字](url)：只显示文字
                label = re.match(r"\[([^\]]+)\]", token)
                self.transcript.insert("end", label.group(1) if label else token,
                                       base_tag)
            pos = m.end()
        if pos < len(line):
            self.transcript.insert("end", line[pos:], base_tag)

    @staticmethod
    def _format_event(ev) -> tuple[str, str]:
        kind, p = ev.kind, ev.payload
        if kind == "user":
            refs = p.get("context_refs") or []
            suffix = f"  [{len(refs)} 个附件]" if refs else ""
            return f"你: {p.get('text', '')}{suffix}", "user"
        if kind == "user_echo":
            return f"已提交: {str(p.get('text', ''))[:120]}", "dim"
        if kind == "assistant":
            return str(p.get("text", "")), "assistant"
        if kind == "tool":
            parts = ["·"]
            if p.get("title"):
                parts.append(str(p["title"]))
            elif p.get("tool_kind"):
                parts.append(f"[{p['tool_kind']}]")
            if p.get("status"):
                parts.append(f"({p['status']})")
            if p.get("output"):
                out = str(p["output"]).strip().splitlines()
                if out:
                    parts.append("→ " + out[0][:120])
            return " ".join(parts), "tool"
        if kind == "usage":
            used, size = p.get("used"), p.get("size")
            if used is not None and size:
                return f"上下文 {used}/{size} tokens", "dim"
            return "", "dim"
        if kind == "state":
            st = p.get("state", "")
            label = STATE_TEXT.get(st, st)
            extra = []
            if p.get("stop_reason"):
                extra.append(f"原因 {p['stop_reason']}")
            if p.get("detail"):
                extra.append(str(p["detail"])[:160])
            if p.get("error_code") in ("store_write_failed", "store_unavailable"):
                return f"— 记录保存失败: {p.get('detail', '')[:160]}", "error"
            return f"— {label}" + (f"（{'；'.join(extra)}）" if extra else ""), "state"
        if kind == "note":
            return f"— {p.get('text', '')}", "note"
        if kind == "config":
            return f"— {p.get('text', '配置更新')}", "note"
        if kind == "permission_request":
            title = p.get("tool_title") or UNKNOWN_TOOL_DETAIL
            return f"⚠ 待确认: {title}", "warn"
        if kind == "permission_resolved":
            return "— 许可已回答", "dim"
        return "", "dim"

    # ---- 许可卡片 ----
    def _render_perm_card(self, snap):
        pending = snap.get("pending_permissions") or []
        target = pending[0] if pending else None
        if target is None:
            if self._perm_frame_rendered:
                for child in self.perm_card.winfo_children():
                    child.destroy()
                self.perm_card.pack_forget()
                self._perm_frame_rendered = False
                self._perm_rendered_id = None
                self._perm_rendered_sig = None
            return
        title = target.get("tool_title") or ""
        raw = target.get("raw_input")
        # 详情签名：同一许可且详情未变化时不重建（避免轮询销毁正在点击的
        # 按钮）；迟到的工具详情补齐时签名变化，重建以展示完整卡片（R3）
        sig = (
            target["permission_id"], title, raw is not None,
            tuple(o["option_id"] for o in target.get("options", [])),
        )
        if self._perm_frame_rendered and self._perm_rendered_sig == sig:
            return
        if self._perm_frame_rendered:
            for child in self.perm_card.winfo_children():
                child.destroy()
        self._perm_rendered_id = target["permission_id"]
        self._perm_rendered_sig = sig
        # side=bottom + after=input：卡片固定在输入区正上方（B-1 布局重排）
        self.perm_card.pack(side="bottom", fill="x", pady=(2, 0), after=self.input)
        self._perm_frame_rendered = True
        has_details = bool(title and title != UNKNOWN_TOOL_DETAIL) or raw is not None
        if not has_details:
            # 详情缺失：不给含糊的允许，仅保留拒绝/停止（R3）
            tk.Label(self.perm_card, text="待你确认（操作详情暂不可用）",
                     fg=WARN, bg=BG,
                     font=self._small_font_or_default(), anchor="w").pack(fill="x")
        else:
            tk.Label(self.perm_card, text=f"待你确认: {title}", fg=WARN, bg=BG,
                     font=self._small_font_or_default(), anchor="w").pack(fill="x")
        if isinstance(raw, dict):
            summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(raw.items())[:3])
            if summary:
                tk.Label(self.perm_card, text=f"参数: {summary}", fg=DIM, bg=BG,
                         font=self._small_font_or_default(), anchor="w").pack(fill="x")
        btns = tk.Frame(self.perm_card, bg=BG)
        btns.pack(fill="x")
        rendered = 0
        for opt in target.get("options", []):
            is_reject = "reject" in str(opt.get("kind", ""))
            if not has_details and not is_reject:
                continue  # 详情缺失时隐藏允许类选项
            cb = self._make_permission_callback(target, opt)
            tk.Button(
                btns, text=opt.get("name", opt.get("option_id", "?")),
                fg=(DIM if is_reject else BG), bg=(BG if is_reject else FG),
                activebackground=LINE, relief="flat", takefocus=0,
                font=self._small_font_or_default(), command=cb, cursor="hand2",
            ).pack(side="left", padx=(0, 6), pady=2)
            rendered += 1
        if not rendered:
            # 上游未提供可安全展示的选项：至少保留本地停止兜底
            tk.Button(
                btns, text="停止任务", fg=ERROR, bg=BG, activebackground=LINE,
                activeforeground=ERROR, relief="flat", takefocus=0,
                font=self._small_font_or_default(), command=self._stop,
                cursor="hand2",
            ).pack(side="left", pady=2)

    def _small_font_or_default(self):
        try:
            return self._small_font
        except AttributeError:
            return "TkDefaultFont"

    def _make_permission_callback(self, target, opt):
        permission_id = target["permission_id"]
        option_id = opt["option_id"]

        def cb():
            if self.controller is None:
                return
            ok = self.controller.answer_permission(permission_id, option_id)
            if not ok:
                self._set_status("该许可已失效（过期或已回答）", WARN)
            self.refresh()

        return cb

    # ---- 控件状态 ----
    def _render_controls(self, active, snap):
        conv = self._current_conversation(snap)
        if conv:
            title = conv.get("title") or "未命名"
            live = "·活跃" if conv.get("session_live") else ""
            self.conv_label.configure(text=f"{title}{live}", fg=FG)
        else:
            self.conv_label.configure(text="未开始对话", fg=DIM)
        if active:
            state = active.get("state", "")
            label = STATE_TEXT.get(state, state)
            elapsed = max(0, int(time.time() - active.get("submitted_at", time.time())))
            self._set_status(f"{label} {elapsed}s", ACCENT)
            self.send_btn.configure(state="disabled")
            self.stop_btn.pack(side="left", padx=(6, 0))
        else:
            self.stop_btn.pack_forget()
            if self.controller is not None and not snap.get("store_healthy", True):
                self._set_status("任务记录暂不可用：新派单已暂停", ERROR)
                self.send_btn.configure(state="disabled")
            elif self.controller is not None:
                self.send_btn.configure(state="normal")
        self._sync_conversation_choice(snap)

    def _set_status(self, text, color=DIM):
        self.status.configure(text=text, fg=color)

    # ---- 会话选择 ----
    def _sync_conversation_choice(self, snap):
        convs = snap.get("conversations") or []
        entries = []
        values = []
        for c in convs[:20]:
            mark = "▸ " if c["conversation_id"] == self.conversation_id else "  "
            entries.append(f"{mark}{c.get('title') or c['conversation_id'][:8]}")
            values.append(c["conversation_id"])
        if list(self.conv_combo.cget("values")) != entries:
            self.conv_combo.configure(values=entries)
        if self.conversation_id in values:
            idx = values.index(self.conversation_id)
            if self.conv_combo.current() != idx:
                self.conv_combo.current(idx)

    def _current_conversation(self, snap):
        for c in snap.get("conversations") or []:
            if c["conversation_id"] == self.conversation_id:
                return c
        return None

    def _switch_conversation(self):
        if self.controller is None:
            return
        idx = self.conv_combo.current()
        values = list(self.conv_combo.cget("values"))
        if idx < 0 or idx >= len(values):
            return
        snap = self.controller.snapshot()
        convs = snap.get("conversations") or []
        if idx < len(convs):
            target = convs[idx]
            self.conversation_id = target["conversation_id"]
            self.project_dir = target.get("cwd")
            if self.project_dir:
                self.project_btn.configure(
                    text=self._short(self.project_dir))
            self.refresh(full=True)

    def _new_conversation(self):
        if self.controller is None:
            return
        if not self.project_dir:
            self._set_status("先选择项目目录", WARN)
            return
        try:
            conv = self.controller.create_conversation(self.project_dir)
        except Exception as exc:
            self._set_status(f"新建对话失败: {exc}", ERROR)
            return
        self.conversation_id = conv.conversation_id
        self.refresh(full=True)

    def _resume_conversation(self):
        if self.controller is None or not self.conversation_id:
            return
        if self._async_busy.get("resume"):
            return  # 已在恢复中
        conv_id = self.conversation_id
        controller = self.controller
        self._async_busy["resume"] = True
        self._set_status("正在恢复会话…", ACCENT)

        def work():
            # resume 内部同步等待 runtime（最长 90s），必须在 Tk 线程之外
            # 执行，否则慢启动/挂起会冻结整个面板（R8）
            try:
                ok = controller.resume(conv_id)
                err = None
            except Exception as exc:
                ok, err = False, exc
            self._async_done.put(("resume", (conv_id, ok, err)))

        threading.Thread(target=work, name="copilot-resume", daemon=True).start()

    def _resume_done(self, conv_id, ok, exc):
        self._async_busy["resume"] = False
        if self.conversation_id != conv_id:
            return  # 用户已切换会话：结果不再当作当前状态展示
        if exc is not None:
            self._set_status(f"恢复失败: {getattr(exc, 'message', exc)}", ERROR)
        elif ok:
            self._set_status("已恢复会话（不回放历史）", ACCENT)
        else:
            self._set_status("恢复失败：会话不存在或目录不符", ERROR)

    # ---- 项目 ----
    def _pick_project(self):
        self._modal_guard = True
        try:
            chosen = filedialog.askdirectory(
                title="选择交给副驾的项目目录", mustexist=True
            )
        finally:
            self.frame.after(300, self._release_modal_guard)
        if not chosen:
            return
        if self.controller is None:
            return
        # 切换项目即新建对话（spec 5.2：不把新项目指令续进旧会话）
        try:
            conv = self.controller.create_conversation(chosen)
        except Exception as exc:
            self._set_status(f"新建对话失败: {exc}", ERROR)
            return
        self.project_dir = conv.cwd
        self.conversation_id = conv.conversation_id
        self.project_btn.configure(text=self._short(conv.cwd))
        self.refresh(full=True)

    def _release_modal_guard(self):
        self._modal_guard = False

    @staticmethod
    def _short(path: str, limit: int = 28) -> str:
        return path if len(path) <= limit else "…" + path[-(limit - 1):]

    # ---- 发送/停止 ----
    def _send(self):
        if self.controller is None:
            self._ensure_controller()
            if self.controller is None:
                return
        if not self.conversation_id:
            if not self.project_dir:
                self._set_status("先选择项目目录", WARN)
                return
            conv = self.controller.create_conversation(self.project_dir)
            self.conversation_id = conv.conversation_id
        text = self.input.get("1.0", "end").strip()
        if not text and not self._chips:
            return
        refs = [c for c in self._chips]
        try:
            run = self.controller.submit(self.conversation_id, text, context_refs=refs)
        except Exception as exc:
            self._set_status(f"发送失败: {getattr(exc, 'message', exc)}", ERROR)
            return
        self.input.delete("1.0", "end")  # 发送成功才清草稿
        self._set_status(f"已受理 {run.run_id[:12]}…", ACCENT)
        self.refresh()

    def _stop(self):
        if self.controller is None:
            return
        snap = self.controller.snapshot()
        active = snap.get("active_run")
        if active:
            self.controller.cancel(active["run_id"])
            self._set_status("已请求停止，等待确认…", ACCENT)
        self.refresh()

    # ---- chips（05 票：交给副驾 的文件引用入口）----
    def add_context_ref(self, path: str):
        if path not in self._chips:
            self._chips.append(path)
        self._render_chips()

    def _append_sessions_snapshot(self):
        """把桌面会话观察快照填入草稿（仅观察数据；发送仍需用户点发送）。

        HTTP GET 在工作线程执行，结果经队列回 Tk 线程（R8：不同步阻塞）。
        """
        from . import deck_client
        from .config import load_config

        if self._async_busy.get("snapshot"):
            return  # 已在获取中
        cfg, _ = load_config()
        base_url = cfg.deck_base_url if cfg else "http://127.0.0.1:5000"
        self._async_busy["snapshot"] = True
        self._set_status("正在获取会话快照…", ACCENT)

        def work():
            try:
                deck = deck_client.fetch_deck(base_url)
                self._async_done.put(
                    ("snapshot", (deck_client.format_sessions_snapshot(deck), None))
                )
            except Exception as exc:
                self._async_done.put(("snapshot", (None, exc)))

        threading.Thread(target=work, name="copilot-snapshot", daemon=True).start()

    def _snapshot_done(self, text, exc):
        self._async_busy["snapshot"] = False
        if exc is not None:
            self._set_status(f"会话快照不可用：{getattr(exc, 'message', exc)}", WARN)
            return
        current = self.input.get("1.0", "end").rstrip("\n")
        block = text + "\n\n"
        self.input.delete("1.0", "end")
        self.input.insert("1.0", block + current)
        self._set_status("已附会话快照到草稿（观察数据）", ACCENT)

    def _remove_chip(self, path: str):
        self._chips = [c for c in self._chips if c != path]
        self._render_chips()

    def _render_chips(self):
        for child in self.chips_row.winfo_children():
            child.destroy()
        for path in self._chips:
            chip = tk.Frame(self.chips_row, bg=LINE)
            chip.pack(side="left", padx=(0, 4), pady=2)
            tk.Label(chip, text=self._short(path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], 20),
                     fg=FG, bg=LINE, font=self._small_font_or_default()).pack(side="left")
            tk.Label(chip, text="×", fg=DIM, bg=LINE, cursor="hand2",
                     font=self._small_font_or_default()).pack(side="left")
            chip_last = chip.winfo_children()[-1]
            chip_last.bind(
                "<Button-1>", lambda e, p=path: self._remove_chip(p)
            )

    @property
    def context_refs(self) -> list[str]:
        return list(self._chips)

    # ---- 键盘与焦点 ----
    def _esc(self, _event=None):
        if self._on_collapse is not None:
            self._on_collapse()
        return "break"

    def _enter_newline(self, _event):
        return None  # 默认行为：换行（发送只走按钮）

    def _focus_maybe_left(self):
        if self._modal_guard:
            return
        if self._collapse_check_job is not None:
            try:
                self.frame.after_cancel(self._collapse_check_job)
            except Exception:
                pass
        # 延迟复核：内部控件间切换的瞬时 FocusOut
        self._collapse_check_job = self.frame.after(150, self._focus_check)

    def _focus_check(self):
        self._collapse_check_job = None
        if self._modal_guard:
            return
        focus = self.frame.winfo_toplevel().focus_get()
        inside = focus is not None and bool(
            focus.winfo_toplevel() is self.frame.winfo_toplevel()
        )
        if not inside and self._on_collapse is not None:
            self._on_collapse()
