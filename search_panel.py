"""搜索面板：数据服务自有的常驻无边框窗口（ADR-0003，ticket 01/02）。

待机态伪装成 DECK 右窄栏顶部的静态视觉（黑底、Decima Mono Cyr、
#2c2c2c 分隔线），与壁纸黑底无缝融合；点击转活动态，原生输入框可打字
（中文输入法可用）；ESC/失焦退回待机态并清空查询词。

引擎链路（ticket 02）：输入经防抖（listary_engine.Debouncer，约 200ms）
后由工作线程调用 Listary 本地 HTTP API，结果经队列回传 Tk 线程自绘为
约 8 行（名称 + 截断父路径 + 总数）；引擎不可达显示 ENGINE OFFLINE 并
自动重试，限流静默退避。查询词不写入任何日志与追踪文件（ADR-0002 精神，
QD_PANEL_TRACE 只记事件与长度，不记内容）。

窗口绘制 SEARCH 面板头与输入行两层视觉，壁纸侧只为它让出固定高度的
空位（几何常量见 PANEL，/deck 以 panel 字段暴露给壁纸）。
"""

import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

import listary_engine as engine

# ---- 几何（物理像素；与 DECK 布局常量同源，rem = 屏宽/160，DECK 以 100vw/160 等比）----
# 设计基准 2560×1440 @100%：x 对齐 DECK #right 内容区左缘
# （2560 − 2.6rem − 36rem + 1px 左边框 + 2.4rem 内边距 = 1982），
# y = 2.2rem + 顶栏 4.6rem + 行距 1.6rem = 8.4rem = 134。
# 屏宽实测推导，任何分辨率下都与 DECK 右栏保持对齐。
# FONT_SCALE 与 DECK 壁纸的 --font-scale（WE 属性 fontsize，默认 1.4）同值才对齐；
# 改 WE 滑杆时用 QD_FONT_SCALE 环境变量同步面板，服务默认 1.4。
FONT_SCALE = float(os.environ.get("QD_FONT_SCALE", "1.4"))


def _init_dpi_and_geometry():
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    width = ctypes.windll.user32.GetSystemMetrics(0)     # SM_CXSCREEN
    rem = width / 160.0 * FONT_SCALE
    panel = {
        "x": round(width - 36.2 * rem + 1),
        "y": round(8.4 * rem),
        "w": round(33.6 * rem),
        "h": round(4 * rem),
    }
    return rem, panel


_REM, PANEL = _init_dpi_and_geometry()
_measured = {"h": None}  # 窗口实测高度（面板线程写入，panel_rect 读取；GIL 下原子）

# ---- 壁纸归属：面板只为经典版（myprojects/qoder-deck）显示，HUD 版自动隐藏 ----
_WE_ROOTS = (r"D:\SteamLibrary", r"D:\Games", r"C:\Program Files (x86)\Steam")


def current_wallpaper_path():
    """WE config.json 里 Monitor0 的当前壁纸文件路径；读不到返回空串。

    config 结构：/<用户名>/general/wallpaperconfig/selectedwallpapers/Monitor0/file，
    用户名键不固定，扫描取第一个含 wallpaperconfig 的顶层键。
    """
    for root in _WE_ROOTS:
        try:
            cfg = json.loads((Path(root) / "steamapps" / "common" / "wallpaper_engine" /
                              "config.json").read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for val in cfg.values():
            if not isinstance(val, dict):
                continue
            sel = (val.get("general", {}).get("wallpaperconfig") or {}).get("selectedwallpapers") or {}
            return str((sel.get("Monitor0") or {}).get("file", ""))
    return ""


def wallpaper_is_classic():
    """当前壁纸是否经典版（myprojects/qoder-deck）；读不到视为否（隐藏）。"""
    p = current_wallpaper_path().replace("\\", "/").lower()
    return "myprojects" in p and "qoder-deck" in p

# ---- 视觉（与 DECK index.html 同源）----
def _find_font():
    """Public snapshot uses installed system fonts; no third-party font files."""
    return None


FONT_PATH = None
FONT_FAMILY = "Consolas"
FONT_SIZE_PX = max(12, round(1.0625 * _REM))  # ≈ DECK body 1.05rem，随 rem 等比
BG = "#000000"
WHITE = "#ffffff"   # 面板头（同 .panel-head）
FG = "#d6d6d6"      # 输入文本（同 body 色）
DIM = "#616161"     # 待机提示 / 占位（≈ #d6d6d6 @45% 压在黑底上）
LINE = "#2c2c2c"    # 分隔线
HEAD_TEXT = "SEARCH"
COPILOT_TEXT = "COPILOT"
HINT_TEXT = "CLICK TO SEARCH_"
PLACEHOLDER_TEXT = "TYPE TO SEARCH FILES_"
NO_RESULTS_TEXT = "NO RESULTS"
OFFLINE_TEXT = "ENGINE OFFLINE"
NAME_CHARS = round(26 * FONT_SCALE)   # 结果行名称截断（保留头部），随窗口等比
PATH_CHARS = round(22 * FONT_SCALE)   # 结果行父路径截断（保留尾部，辨识段在结尾）
ROW_H = round(22 * FONT_SCALE)        # 结果行高（与输入行区分：列表更紧凑）
INPUT_ROW_H = round(30 * FONT_SCALE)  # 输入行高
BOX_PAD_Y = round(13 * FONT_SCALE)    # 输入行上边距
SMALL_PAD = round(6 * FONT_SCALE)     # 无结果/离线行边距
TOTAL_PAD = round(4 * FONT_SCALE)     # TOTAL 行边距
SEP_PAD = round(2 * FONT_SCALE)       # 分隔线边距
CTRL_MASK = 0x0004  # Tk event.state 的 Control 位（X11/Win32 一致）


class PanelStateMachine:
    """待机态 ⇄ 活动态的转移规则（纯逻辑，离线可测）。

    事件：click（点击面板）、esc（按 Esc）、blur（焦点移出）。
    esc 与 blur 是同一转移的两种触发，保留两个事件名以对齐操作集
    词汇；退回待机态时清空查询词——下次激活从空查询开始。
    """

    IDLE = "IDLE"
    ACTIVE = "ACTIVE"

    def __init__(self):
        self.state = self.IDLE
        self.query = ""

    def set_query(self, text):
        if self.state == self.ACTIVE:
            self.query = text

    def on_event(self, event):
        if self.state == self.IDLE:
            if event == "click":
                self.state = self.ACTIVE
        elif event in ("esc", "blur"):
            self.state = self.IDLE
            self.query = ""
        return self.state


def panel_rect():
    """窗口矩形（/deck 的 panel 字段来源）。窗口未运行时返回静态默认。"""
    return dict(PANEL, h=_measured["h"] or PANEL["h"])


class SearchPanelApp:
    """Tk 窗口层：只在本线程内触碰 Tk（线程安全性由调用方保证）。"""

    def __init__(self):
        self.machine = PanelStateMachine()
        self._debouncer = engine.Debouncer()
        self._results_q = queue.Queue()
        self._search_gen = 0        # 单飞行代际：新查询作废旧响应
        self._results_query = None  # 当前结果所属查询（复核 F04：动作前核对）
        self._rate_attempt = 0      # 限流退避次数（成功清零）
        self._sel = engine.SelectionModel()  # 选中项（结果刷新重置回首项）
        self._items = []            # 当前渲染的结果（动作按索引取路径）
        self._rows = []             # 行控件 [(frame, name_label, path_label)]
        self._view = "idle"         # idle | files | copilot（files 复用状态机）
        self._copilot_view = None   # CopilotView（首次进入副驾时构造，常驻保草稿）
        # 副驾可用性与面板模式在窗口线程外只读一次；配置损坏时退回旧行为
        self._copilot_reason = ""
        try:
            from copilot import runtime_host
            available, reason = runtime_host.copilot_tab_available()
            self._copilot_enabled = available
            self._copilot_reason = reason
            self._panel_mode = runtime_host.panel_mode()
        except Exception:
            self._copilot_enabled = False
            self._panel_mode = "auto"

    def _trace(self, tag):
        # QD_PANEL_TRACE=<路径> 时追加事件流水（验收电池/排障用）。
        # 只记事件与查询长度，绝不记查询内容（ADR-0002 精神）。
        path = os.environ.get("QD_PANEL_TRACE")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.time():.3f} {tag} state={self.machine.state} "
                         f"qlen={len(self.machine.query)} "
                         f"hwnd={getattr(self, 'root', None) and self.root.winfo_id()}\n")

    def run(self):
        self._load_deck_font()
        root = tk.Tk()
        root.overrideredirect(True)  # 无边框：视觉即壁纸的一部分
        root.configure(bg=BG)
        root.geometry(f"{PANEL['w']}x{PANEL['h']}+{PANEL['x']}+{PANEL['y']}")
        self.root = root
        self.font = tkfont.Font(family=FONT_FAMILY, size=-FONT_SIZE_PX)
        self.small_font = tkfont.Font(family=FONT_FAMILY, size=-max(11, FONT_SIZE_PX - 4))

        # 布局：面板头（副驾可用时为 SEARCH|COPILOT tab 行）/ 输入行 / 分隔线
        head_row = tk.Frame(root, bg=BG)
        head_row.pack(fill="x")
        self.head = tk.Label(head_row, text=HEAD_TEXT, fg=WHITE, bg=BG,
                             font=self.font, anchor="w")
        self.head.pack(side="left")
        self._head_row = head_row
        self.copilot_head = None
        if self._copilot_enabled:
            self.copilot_head = tk.Label(
                head_row, text=COPILOT_TEXT, fg=DIM, bg=BG,
                font=self.font, anchor="e", cursor="hand2"
            )
            self.copilot_head.pack(side="right")
            self.copilot_head.bind("<Button-1>", lambda e: self._open_copilot())
        self.box = tk.Frame(root, bg=BG, height=INPUT_ROW_H)
        self.box.pack_propagate(False)
        self.box.pack(fill="x", pady=(BOX_PAD_Y, 0))
        self.hint = tk.Label(self.box, text=HINT_TEXT, fg=DIM, bg=BG,
                             font=self.font, anchor="w")
        self.hint.pack(fill="both", expand=True)
        self.var = tk.StringVar(root)
        # 文本变化走 StringVar 追踪（而非 KeyRelease）：中文输入法合成提交
        # 对 var 生效但不一定逐键触发 KeyRelease
        self.var.trace_add("write", lambda *_: self._on_text_changed())
        self.entry = tk.Entry(self.box, textvariable=self.var, fg=FG, bg=BG, font=self.font,
                              insertbackground=FG, relief="flat",
                              selectbackground=LINE, selectforeground=WHITE,
                              highlightthickness=0, takefocus=True)
        self.placeholder = tk.Label(self.box, text=PLACEHOLDER_TEXT, fg=DIM, bg=BG,
                                    font=self.font, anchor="w")
        self.sep = tk.Frame(root, bg=LINE, height=1)
        self.sep.pack(fill="x", pady=(SEP_PAD, 0))
        # 结果区：渲染在分隔线下方，窗口随内容向下展开（活动态临时盖住
        # 壁纸会话列表，ESC 即收起——搜索面板的既有交互惯例）
        self.results_host = tk.Frame(root, bg=BG)
        self.results_host.pack(fill="x")

        # 事件：点击（任意部位，含分隔线与占位）激活；Esc/失焦退待机。
        # Tk 子控件的 bindtags 含顶层窗口，root 绑定对副驾 tab 及副驾视图
        # 内部控件同样触发，故先经 _on_search_area_click 辨明点击归属（R1：
        # 副驾内部点击不冒泡为搜索激活/退出副驾）
        for w in (root, self.head, self.box, self.hint, self.sep, self.placeholder):
            w.bind("<Button-1>", self._on_search_area_click, add="+")
        self.entry.bind("<Button-1>", self._on_search_area_click, add="+")
        self.entry.bind("<Escape>", lambda e: self._deactivate("esc"))
        self.entry.bind("<FocusOut>", lambda e: self._deactivate("blur"))
        # 结果操作集（ticket 03）：键→动作的判一出口在 engine.decide_action，
        # 这里只翻译 Tk 键名；"break" 吞掉 ↑↓ 移动光标的默认行为
        self.entry.bind("<Up>", lambda e: self._on_key("up", e))
        self.entry.bind("<Down>", lambda e: self._on_key("down", e))
        self.entry.bind("<Return>", lambda e: self._on_key("return", e))

        root.update_idletasks()
        _measured["h"] = max(PANEL["h"], root.winfo_reqheight())
        self._base_h = _measured["h"]
        root.after(50, self._tick)  # 防抖到期检查 + 结果队列回传（Tk 线程内）

        crash_after = os.environ.get("QD_PANEL_CRASH")
        if crash_after:
            # 验收钩子：到点销毁窗口退出 mainloop，再在线程体内抛错，
            # 使窗口线程真实终结（Tk 回调里的异常会被吞掉，杀不死线程）
            root.after(int(float(crash_after) * 1000), self._crash_for_test)
        autoactivate = os.environ.get("QD_PANEL_AUTOACTIVATE")
        if autoactivate:
            # 验收钩子：程序化激活（不依赖外部鼠标；截图取证/电池用）
            root.after(int(float(autoactivate) * 1000), self._activate)

        root.mainloop()
        if self._crash_requested:
            raise RuntimeError("search-panel: QD_PANEL_CRASH boom")

    # ---- 内部 ----
    _crash_requested = False

    @staticmethod
    def _load_deck_font():
        # Public snapshot relies on the installed system font.
        return

    def _on_search_area_click(self, event):
        # root 绑定经由 bindtags 对全部子控件触发：只有真正落在搜索构件上
        # 的点击才激活搜索；副驾 tab 自身与副驾视图内部的点击（输入、许可、
        # 滚动等）交给其自身处理，不冒泡为视图切换（R1）
        if self._view == "copilot":
            if event.widget is getattr(self, "copilot_head", None):
                return  # 已在副驾 tab，重复点击无操作
            if self._copilot_view is not None and self._copilot_view.contains(
                    event.widget):
                return  # 副驾视图内部
            self._close_copilot(to_files=True)  # SEARCH tab / 头部空白：切回文件视图
            return
        self._activate()

    def _activate(self):
        # 活动态下重复点击 = 摆放光标，不重置查询（状态机对 click 幂等）
        self._trace("click")
        if self._view == "copilot":
            self._close_copilot(to_files=True)
            return
        self.machine.on_event("click")
        self._view = "files"
        self._render_tabs()
        self._render_active()
        self.entry.focus_set()

    # ---- 副驾视图切换（04 票）：收起保留草稿/项目/任务，不取消不批准 ----

    def _open_copilot(self):
        if self._view == "copilot":
            return
        if self._view == "files":
            # 离开文件视图：作废在途搜索（不触碰状态机语义）
            self._search_gen += 1
            self._debouncer.reset()
            self._rate_attempt = 0
            self.machine.on_event("blur")
            self.entry.delete(0, "end")
            self._clear_results()
        self._view = "copilot"
        if self._copilot_view is None:
            from copilot.panel_view import CopilotView
            from copilot import runtime_host
            if not hasattr(self, "small_font"):
                self.small_font = tkfont.Font(
                    family=self.font.actual("family"),
                    size=-max(11, abs(self.font.actual("size")) - 4),
                )
            self._copilot_view = CopilotView(
                self.root, self.font, self.small_font,
                get_controller=runtime_host.get_controller,
                on_collapse=lambda: self._close_copilot(to_files=False),
            )
        # 隐藏文件视图构件（副驾视图独占窗口）
        self.box.pack_forget()
        self.sep.pack_forget()
        self.results_host.pack_forget()
        self._render_tabs()
        self._apply_copilot_height()
        self._copilot_view.show()
        self._trace_copilot_layout("open")
        self.root.after(200, lambda: self._trace_copilot_layout("open+200ms"))

    def _trace_copilot_layout(self, phase):
        if self._view != "copilot" or self._copilot_view is None:
            return
        view = self._copilot_view
        if not hasattr(view, "input"):
            return
        self._trace(
            f"copilot {phase} root={self.root.winfo_width()}x{self.root.winfo_height()} "
            f"mapped={view.input.winfo_ismapped()} "
            f"input_y={view.input.winfo_y()} input_h={view.input.winfo_height()}"
        )

    def _close_copilot(self, to_files: bool):
        if self._view != "copilot":
            return
        self._view = "idle"  # 先复位，_activate 不再走关闭分支
        if self._copilot_view is not None:
            self._copilot_view.hide()
        # 恢复文件视图构件的原始堆叠顺序
        self.box.pack(fill="x", pady=(BOX_PAD_Y, 0))
        self.sep.pack(fill="x", pady=(SEP_PAD, 0))
        self.results_host.pack(fill="x")
        self._apply_height()
        if to_files:
            self._activate()
        else:
            self._render_tabs()
            self._render_idle()

    def _apply_copilot_height(self):
        # 副驾需要可读高宽（spec 5.1：可展开到可读宽高）
        h = round(30 * _REM)
        self.root.geometry(f"{PANEL['w']}x{h}+{PANEL['x']}+{PANEL['y']}")

    def _render_tabs(self):
        # 头部 tab 高亮：当前视图白、另一侧暗（副驾不可用时无 tab，直通）
        copilot_head = getattr(self, "copilot_head", None)
        if copilot_head is None:
            return
        if self._view == "copilot":
            self.head.configure(fg=DIM)
            copilot_head.configure(fg=WHITE)
        else:
            self.head.configure(fg=WHITE)
            copilot_head.configure(fg=DIM)

    def _deactivate(self, event):
        # 切换副驾时搜索 Entry 的 FocusOut 会迟于 tab 点击抵达。此时再缩回
        # 文件待机高度会裁掉副驾输入区，表现为首次进入需再点一次。
        if self._view == "copilot":
            return
        self._trace(event)
        self._search_gen += 1      # 作废在途响应
        self._debouncer.reset()
        self._rate_attempt = 0
        self.machine.on_event(event)  # 转移本身已清空查询词
        self._view = "idle"
        self._render_tabs()
        self.entry.delete(0, "end")   # 触发 var 追踪，但状态已 IDLE 不再喂防抖
        self._clear_results()
        self._apply_height()
        self._render_idle()

    def _on_text_changed(self):
        text = self.var.get()
        self.machine.set_query(text)
        self._render_active()  # 空输入显示占位、有输入显示文本
        if self.machine.state == PanelStateMachine.ACTIVE:
            # 每次变更即作废在途响应与旧结果的可执行性（复核 F04）：非空修改
            # （old→new）在防抖窗口内同样必须失效，不能等下一次请求才递增
            self._search_gen += 1
            self._debouncer.feed(text, time.monotonic())
            if not text:
                self._clear_results()
                self._apply_height()

    # ---- 引擎链路（防抖到期 → 工作线程请求 → 队列回传 → 渲染）----

    def _tick(self):
        try:
            due = self._debouncer.due(time.monotonic())
            if due:
                self._start_search(due)
            self._drain_results()
            self._wallpaper_ticks = getattr(self, "_wallpaper_ticks", 0) + 1
            if self._panel_mode == "copilot" and self._wallpaper_ticks % 20 == 0:
                # Win+D 会把无边框 Tk 窗口最小化；单纯检查壁纸宿主不够。
                # 只在窗口实际不可见时恢复，不周期性抢占其它应用焦点。
                if self.root.state() == "iconic" or not self.root.winfo_viewable():
                    self.root.deiconify()
                    self.root.lift()
                    self._trace("copilot restored")
            if self._wallpaper_ticks % 100 == 0:   # 50ms × 100 = 5s 一查
                if self._panel_mode == "hidden":
                    if self.root.winfo_viewable():
                        self.root.withdraw()
                elif self._panel_mode == "auto":
                    classic = wallpaper_is_classic()
                    if classic != getattr(self, "_shown_for_classic", True):
                        self._shown_for_classic = classic
                        if classic:
                            self.root.deiconify()
                        else:
                            self.root.withdraw()
                        self._trace(f"wallpaper classic={classic}")
        finally:
            self.root.after(50, self._tick)

    def _start_search(self, query):
        self._trace(f"search len={len(query)}")
        self._search_gen += 1
        gen = self._search_gen

        def work():
            try:
                payload = engine.http_search(query)
                self._results_q.put((gen, query, payload, None))
            except Exception as exc:  # 连接失败/超时：交给错误分类
                self._results_q.put((gen, query, None, exc))

        threading.Thread(target=work, name="search-panel-query", daemon=True).start()

    def _schedule_rate_backoff(self):
        # 限流静默退避：保持现有展示，按退避曲线重试当前词
        self._rate_attempt += 1
        delay = engine.backoff_delay(self._rate_attempt)
        self.root.after(int(delay * 1000), self._retry_now)

    def _schedule_offline_retry(self):
        self.root.after(int(engine.OFFLINE_RETRY_S * 1000), self._retry_now)

    def _retry_now(self):
        # 重试入口：仍活动且有词才重发（feed_due 绕过防抖但保留同词去重）
        if self.machine.state != PanelStateMachine.ACTIVE:
            return
        self._trace("retry")
        self._debouncer.feed_due(self.var.get())

    def _drain_results(self):
        items = []
        while True:
            try:
                items.append(self._results_q.get_nowait())
            except queue.Empty:
                break
        if not items:
            return
        # 到达顺序 ≠ 查询顺序：同批多代响应取代际最新者，过期判断在选出之后
        # （先按到达序留末项会把最新代际挤掉，审计 B05）
        gen, query, payload, exc = max(items, key=lambda t: t[0])
        if gen != self._search_gen or query != self.machine.query:
            return  # 过期响应（用户已清空/继续输入/已退待机；清空路径同样递增代际）
        if exc is not None:
            kind = engine.classify_failure(exc)      # 连接失败 → offline
        else:
            kind = engine.classify_failure(payload)  # ok → None；错误载荷 → 分类
        if kind is None:
            self._rate_attempt = 0
            model = engine.parse_response(payload)
            self._trace(f"results total={model.total} n={len(model.items)}")
            self._render_results(model)
            # 结果归属的查询（复核 F04）：执行动作时核对，旧结果不得当作
            # 当前查询执行；None 表示未经真实响应链路渲染（无在途校验意义）
            self._results_query = query
        elif kind == "rate_limited":
            # 仅限流走静默退避重试（spec：TOO_MANY_REQUESTS 静默退避）
            self._trace(kind)
            self._schedule_rate_backoff()
        elif kind == "error":
            # 引擎在线但响应异常：不冒充离线也不自动重试，等下次输入
            self._trace(kind)
        else:  # offline：引擎不可达/未就绪
            self._trace("offline")
            self._render_offline()
            self._schedule_offline_retry()

    # ---- 结果操作集（ticket 03）----

    def _on_key(self, key, event):
        action = engine.decide_action(key, ctrl=bool(event.state & CTRL_MASK))
        if action:
            self._on_action(action)
            return "break"
        return None

    def _on_action(self, action):
        if action in ("prev", "next"):
            self._move_selection(-1 if action == "prev" else 1)
            return
        if self._results_stale():
            self._trace("stale-results-blocked")
            return  # 旧查询的结果不得当作当前查询执行（复核 F04）
        idx = self._sel.index
        if idx < 0 or idx >= len(self._items):
            return
        self._act(self._items[idx].path, reveal=(action == "reveal"))

    def _results_stale(self):
        """当前显示的结果是否属于另一个查询（防抖窗口内的旧结果）。"""
        return (self._results_query is not None
                and self._results_query != self.machine.query)

    def _move_selection(self, delta):
        # 列表恒 ≤8 行且窗口随行数展开，选中项天然可见，无需滚动
        self._sel.move(delta)
        self._apply_selection()

    def _act(self, path, reveal):
        # 动作由数据服务进程直接执行（ADR-0003：能力在数据服务内）。
        # startfile 走 ShellExecute；explorer 亦按仓库先例带 CREATE_NO_WINDOW，
        # 双保险保证不闪控制台
        self._trace("reveal" if reveal else "open")
        try:
            if reveal:
                subprocess.Popen(["explorer", "/select,", path],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.startfile(path)  # noqa: S606 — 面板本义就是打开用户选中的路径
        except Exception:
            traceback.print_exc()
        # 动作完成即收起：走 ESC 同款待机转移（打开的窗口随即接管焦点）
        self._deactivate("esc")

    def _apply_selection(self):
        for i, (row, name_lbl, path_lbl) in enumerate(self._rows):
            if i == self._sel.index:  # 反白选中（同壁纸 st-CONFIRM/OFFLINE 语言）
                row.configure(bg=FG)
                name_lbl.configure(bg=FG, fg=BG)
                path_lbl.configure(bg=FG, fg=BG)
            else:
                row.configure(bg=BG)
                name_lbl.configure(bg=BG, fg=WHITE)
                path_lbl.configure(bg=BG, fg=DIM)

    # ---- 结果渲染 ----

    def _render_results(self, model):
        host = self.results_host
        self._clear_results()
        if not model.items:
            tk.Label(host, text=NO_RESULTS_TEXT, fg=DIM, bg=BG,
                     font=self.font, anchor="w").pack(fill="x", pady=(SMALL_PAD, 0))
        else:
            shown = model.items[: engine.DEFAULT_LIMIT]
            self._items = shown
            for i, item in enumerate(shown):
                row = tk.Frame(host, bg=BG, height=ROW_H)
                row.pack_propagate(False)
                row.pack(fill="x")
                name, parent = engine.display_parts(item.path)
                name_lbl = tk.Label(row, text=engine.elide_right(name, NAME_CHARS),
                                    fg=WHITE, bg=BG, font=self.font, anchor="w")
                name_lbl.pack(side="left")
                path_lbl = tk.Label(row, text=engine.elide_left(parent, PATH_CHARS),
                                    fg=DIM, bg=BG, font=self.font, anchor="e")
                path_lbl.pack(side="right")
                self._rows.append((row, name_lbl, path_lbl))
                for w in (row, name_lbl, path_lbl):
                    w.bind("<Button-1>", self._row_click_handler(i), add="+")
                    w.bind("<Button-3>", self._row_context_handler(i), add="+")
            self._sel.set_count(len(shown))  # 结果刷新：选中重置回首项
            self._apply_selection()
        tk.Label(host, text=f"TOTAL {model.total}", fg=DIM, bg=BG,
                 font=self.font, anchor="w").pack(fill="x", pady=(TOTAL_PAD, 0))
        self._apply_height()

    def _on_row_click(self, idx):
        if self._results_stale():
            return  # 行点击与 Enter 同一执行边界（复核 F04）
        if 0 <= idx < len(self._items):
            self._act(self._items[idx].path, reveal=False)

    def _row_click_handler(self, idx):
        # 返回 "break" 阻止点击行时再触发窗口级激活绑定
        def handler(_event):
            self._on_row_click(idx)
            return "break"
        return handler

    def _row_context_handler(self, idx):
        """结果行右键：交给副驾（独立动作，不改 Enter 打开/定位）。"""
        def handler(_event):
            if self._results_stale():
                return "break"
            if not (0 <= idx < len(self._items)) or not self._copilot_enabled:
                return "break"
            menu = tk.Menu(self.root, tearoff=0, bg=BG, fg=FG,
                           activebackground=LINE, activeforeground=WHITE)
            menu.add_command(label="交给副驾（作为文件引用）",
                             command=lambda: self._handoff_to_copilot(
                                 self._items[idx].path))
            try:
                menu.tk_popup(_event.x_root, _event.y_root)
            finally:
                menu.grab_release()
            return "break"
        return handler

    def _handoff_to_copilot(self, path):
        """文件搜索结果 → 副驾草稿附件；只填草稿，不发送。"""
        if not self._copilot_enabled or self._copilot_view is None:
            return
        self._trace("handoff-copilot")
        self._open_copilot()
        self._copilot_view.add_context_ref(path)

    def _render_offline(self):
        self._clear_results()
        badge = tk.Label(self.results_host, text=OFFLINE_TEXT, fg=BG, bg=FG,
                         font=self.font, anchor="w", padx=round(6 * FONT_SCALE))
        badge.pack(fill="x", pady=(SMALL_PAD, 0))
        self._apply_height()

    def _clear_results(self):
        for child in self.results_host.winfo_children():
            child.destroy()
        self._items = []
        self._rows = []
        self._sel.set_count(0)
        self._results_query = None

    def _apply_height(self):
        self.results_host.update_idletasks()
        h = self._base_h + self.results_host.winfo_reqheight()
        self.root.geometry(f"{PANEL['w']}x{h}+{PANEL['x']}+{PANEL['y']}")

    def _render_active(self):
        # 用几何管理器归属判断（winfo_manager），不依赖窗口是否已映射：
        # pack 与映射之间有时间窗，按 ismapped 判断会让组件卡在堆叠里
        if self.hint.winfo_manager():
            self.hint.pack_forget()
        if not self.entry.winfo_manager():
            self.entry.pack(fill="both", expand=True)
        show_placeholder = not self.entry.get()
        if show_placeholder and not self.placeholder.winfo_manager():
            self.placeholder.place(in_=self.box, relwidth=1, relheight=1)
        elif not show_placeholder and self.placeholder.winfo_manager():
            self.placeholder.place_forget()

    def _render_idle(self):
        if self.entry.winfo_manager():
            self.entry.pack_forget()
        if self.placeholder.winfo_manager():
            self.placeholder.place_forget()
        if not self.hint.winfo_manager():
            self.hint.pack(fill="both", expand=True)

    def _crash_for_test(self):
        # 验收用：人为让窗口线程终结，验证数据服务本体不受影响（ticket 01）
        self._crash_requested = True
        self.root.destroy()


def run_app():
    """窗口入口：任何异常只终结本线程，绝不外溢到数据服务。"""
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # 物理像素几何（100% 缩放下恒等）
    except Exception:
        pass
    try:
        SearchPanelApp().run()
    except Exception:
        traceback.print_exc()
        print("search-panel: 窗口线程退出，数据服务不受影响", flush=True)


def start_thread():
    t = threading.Thread(target=run_app, name="search-panel", daemon=True)
    t.start()
    return t
