"""现场感知 L0 适配：前台窗口身份流（宿主管理的 stdio 子进程，JSON Lines）。

协议（与 search_adapter 同构；宿主：host/SceneClient.cs）：
  请求：{"id": <int>, "op": "snapshot"}   当前现场 + 时间线
        {"id": <int>, "op": "ping"}
        {"id": <int>, "op": "probe", "hwnd": <int>, "tier": "fast"|"full"}
                                            L1 内容探测（宿主传召唤前前台窗口；
                                            场景识别+受限材料采集，见 scene_probe）。
                                            M6 分档：fast 跳过 UIA（缺省 full）
        {"id": <int>, "op": "capture", "hwnd": <int>, "materials": [...]}
                                            L2 视觉兜底（截窗+本地 OCR，仅文本
                                            出境，图片不落盘；见 scene_capture）。
                                            携带 L1 材料时先融合去重（scene_fusion）
        {"id": <int>, "op": "clipboard"}    L3 剪贴板快照（M7：文件列表+格式标志；
                                            文本内容永不读取出境——密码管理器红线）
  响应：{"id": <int>, "status": "ok", "current": {...}|null,
         "timeline": [...]}                （timeline 新→旧，≤20 条）
  非请求事件（适配器主动推送，宿主按 "event" 字段识别）：
        {"event": "foreground", "current": {...}}
        {"event": "clipboard", "formats": [...], "ts": ...}   （仅元数据）
  current：{"pid", "process", "exe", "title", "class", "ts"}

设计（.scratch/scene-m1/spec.md 决策）：
- 事件源：SetWinEventHook(EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT) 专用
  线程 + 消息泵；回调只入队 hwnd（不做重活），采集/去重/输出在写线程。
- M1 不挂 NAMECHANGE：同窗口标题变化不刷新（已知边界，M2 增强）。
- 适配器只上报不自滤（HUD 自身成为前台时保持上一个真实现场——过滤在宿主）。
- 只读 Win32 查询 + psutil 进程缓存；不启动 server.py，不触网、不写盘。
  退出：宿主关闭 stdin → PostThreadMessage(WM_QUIT) 拆钩后干净返回。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # 兄弟模块(scene_probe/scene_capture)在任意启动方式下可导入

import psutil  # noqa: E402

import comtypes  # noqa: E402  主线程在导入时取 STA；探线程转 MTA（见 probe_loop）
import scene_capture  # noqa: E402
import scene_fusion  # noqa: E402
import scene_probe  # noqa: E402

# 与宿主的 stdin/stdout 固定 UTF-8（Windows 控制台默认 ANSI 代码页会毁掉中文标题）
for _stream in (sys.stdin, sys.stdout):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TITLE_MAX = 120
CLASS_MAX = 64
TIMELINE_MAX = 50
SNAPSHOT_TIMELINE_MAX = 20
PROC_CACHE_MAX = 256

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012

# ---- 剪贴板（M7）：事件只记格式元数据；文本内容永不读取出境（密码管理器红线） ----
WM_CLIPBOARDUPDATE = 0x031D
CF_UNICODETEXT = 13
CF_HDROP = 15
HWND_MESSAGE = -3
CLIP_FILES_MAX = 20

_write_lock = threading.Lock()
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# 64 位句柄/返回值防截断
_user32.GetClipboardData.restype = wt.HANDLE
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.CreateWindowExW.restype = wt.HWND
_shell32.DragQueryFileW.restype = wt.UINT
_shell32.DragQueryFileW.argtypes = [wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT]

_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD
)
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t
)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


_clip_lock = threading.Lock()
_clip_state: dict = {"ts": None, "formats": []}


def _clipboard_formats() -> list[str]:
    """剪贴板格式元数据（不读内容）。IsClipboardFormatAvailable 无需 OpenClipboard。"""
    formats: list[str] = []
    try:
        if _user32.IsClipboardFormatAvailable(CF_HDROP):
            formats.append("files")
        if _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            formats.append("text")
        html_fmt = _user32.RegisterClipboardFormatW("HTML Format")
        if html_fmt and _user32.IsClipboardFormatAvailable(html_fmt):
            formats.append("html")
    except Exception:
        pass
    return formats


def clipboard_age_s(now: "float | None" = None) -> "float | None":
    """距最近剪贴板事件的秒数；监听未就绪/无事件为 None。"""
    with _clip_lock:
        ts = _clip_state["ts"]
    if ts is None:
        return None
    return round((time.time() if now is None else now) - ts, 1)


def _hdrop_files(hdrop) -> list[str]:
    out: list[str] = []
    n = int(_shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0))
    for i in range(min(n, CLIP_FILES_MAX)):
        ln = _shell32.DragQueryFileW(hdrop, i, None, 0)
        if ln <= 0:
            continue
        buf = ctypes.create_unicode_buffer(ln + 1)
        _shell32.DragQueryFileW(hdrop, i, buf, ln + 1)
        if buf.value:
            out.append(buf.value[:300])
    return out


def read_clipboard_snapshot() -> dict:
    """召唤时读一次剪贴板：文件列表 + 格式标志。文本内容不读取（M7 红线）。"""
    files: list[str] = []
    has_text = False
    has_html = False
    note = ""
    try:
        html_fmt = _user32.RegisterClipboardFormatW("HTML Format")
        if not _user32.OpenClipboard(None):
            note = "剪贴板被占用"
        else:
            try:
                has_text = bool(_user32.IsClipboardFormatAvailable(CF_UNICODETEXT))
                has_html = bool(html_fmt and _user32.IsClipboardFormatAvailable(html_fmt))
                if _user32.IsClipboardFormatAvailable(CF_HDROP):
                    hdrop = _user32.GetClipboardData(CF_HDROP)
                    if hdrop:
                        files = _hdrop_files(hdrop)
            finally:
                _user32.CloseClipboard()
    except Exception as e:  # noqa: BLE001
        note = f"剪贴板读取失败（{type(e).__name__}）"
    return {"files": files, "has_text": has_text, "has_html": has_html, "note": note}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class SceneState:
    """去重 + 环形时间线（纯逻辑；单线程拥有——写线程）。"""

    def __init__(self, timeline_max: int = TIMELINE_MAX):
        self.timeline_max = timeline_max
        self.current: dict | None = None
        self.timeline: list[dict] = []

    def update(self, info: dict) -> dict | None:
        """采集到前台信息；(pid,title) 变化才推进并返回事件 current。"""
        key = (info.get("pid"), info.get("title"))
        if self.current is not None and (self.current.get("pid"), self.current.get("title")) == key:
            return None
        self.current = info
        self.timeline.append(dict(info))
        if len(self.timeline) > self.timeline_max:
            del self.timeline[: len(self.timeline) - self.timeline_max]
        return info

    def snapshot(self) -> dict:
        return {
            "current": dict(self.current) if self.current else None,
            "timeline": [dict(x) for x in self.timeline[:-SNAPSHOT_TIMELINE_MAX - 1:-1]],
        }


class ProcCache:
    """pid → (process, exe)；NoSuchProcess/AccessDenied 容忍为空串。"""

    def __init__(self, max_entries: int = PROC_CACHE_MAX):
        self._cache: dict[int, tuple[str, str]] = {}
        self._max = max_entries

    def get(self, pid: int) -> tuple[str, str]:
        hit = self._cache.get(pid)
        if hit is not None:
            return hit
        name, exe = "", ""
        try:
            proc = psutil.Process(pid)
            name = proc.name() or ""
            try:
                exe = proc.exe() or ""
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe = ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        if len(self._cache) >= self._max:
            self._cache.clear()
        self._cache[pid] = (name, exe)
        return name, exe


def capture_window(hwnd: int, proc_cache: "ProcCache | None" = None) -> "dict | None":
    """读指定窗口身份；无标题/无 pid 返回 None。"""
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(512)
    n = _user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value[: max(n, 0)]
    if not title.strip():
        return None
    n = _user32.GetClassNameW(hwnd, buf, 512)
    cls = buf.value[: max(n, 0)]
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    proc, exe = (proc_cache or ProcCache()).get(pid.value)
    return {
        "pid": pid.value,
        "process": proc,
        "exe": exe,
        "title": _clip(title, TITLE_MAX),
        "class": _clip(cls, CLASS_MAX),
        "ts": round(time.time(), 3),
    }


def capture_foreground(proc_cache: "ProcCache | None" = None) -> "dict | None":
    return capture_window(_user32.GetForegroundWindow() or 0, proc_cache)


# ---- stdio 协议（校验/响应与 search_adapter 同构） ----

def _validate(raw: str):
    try:
        req = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, "bad_json"
    if not isinstance(req, dict):
        return None, "bad_json"
    rid = req.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return None, "bad_id"
    op = req.get("op")
    if op not in ("snapshot", "ping", "probe", "capture", "clipboard"):
        return None, "bad_op"
    if op == "clipboard":
        return {"id": rid, "op": op}, None
    hwnd = req.get("hwnd", 0)
    if op in ("probe", "capture"):
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd < 0:
            return None, "bad_hwnd"
        if op == "capture" and hwnd == 0:
            return None, "bad_hwnd"  # 截窗必须指名窗口
        req_out: dict = {"id": rid, "op": op, "hwnd": hwnd}
        if op == "probe":
            # M6 感知分档：fast 跳过 UIA（缺省 full，保持旧行为）
            tier = req.get("tier", "full")
            req_out["tier"] = tier if tier in ("fast", "full") else "full"
        else:
            # M6 融合管线：capture 可携带 L1 材料快照，应答前先融合去重
            req_out["materials"] = _sanitize_materials(req.get("materials"))
        return req_out, None
    return {"id": rid, "op": op}, None


def _sanitize_materials(raw) -> list[dict]:
    """capture 携带的 L1 材料快照消毒：非列表/超限一律截断，绝不拒绝请求。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for m in raw[:8]:
        if not isinstance(m, dict):
            continue
        text = m.get("text", "")
        item: dict = {
            "kind": str(m.get("kind", ""))[:40],
            "text": str(text)[:2600] if isinstance(text, str) else "",
        }
        lines = m.get("lines")
        if isinstance(lines, list):
            clean_lines = []
            for ln in lines[:120]:
                if not isinstance(ln, dict):
                    continue
                box = ln.get("box")
                if (isinstance(box, list) and len(box) == 4
                        and all(isinstance(v, (int, float)) for v in box)):
                    clean_lines.append({"text": str(ln.get("text", ""))[:200],
                                        "box": [float(v) for v in box]})
            if clean_lines:
                item["lines"] = clean_lines
        out.append(item)
    return out


def _respond(resp: dict) -> None:
    line = json.dumps(resp, ensure_ascii=False)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_event(current: dict) -> None:
    _respond({"event": "foreground", "current": current})


def main() -> None:
    state = SceneState()
    cache = ProcCache()
    events: "queue.Queue[int | None]" = queue.Queue()
    hook_thread_id = 0

    def writer_loop() -> None:
        while True:
            hwnd = events.get()
            if hwnd is None:
                return
            info = capture_window(hwnd, cache)
            if info is None:
                continue
            changed = state.update(info)
            if changed is not None:
                try:
                    _emit_event(changed)
                except OSError:
                    return  # 宿主管道已断

    def hook_proc(hook, event, hwnd, id_object, id_child, thread, ts):
        # 只入队 hwnd；采集与输出在写线程（spec 决策 1）
        try:
            events.put(hwnd or 0)
        except Exception:
            pass

    def hook_thread_main() -> None:
        nonlocal hook_thread_id
        hook_thread_id = _kernel32.GetCurrentThreadId()

        def clip_wndproc(c_hwnd, msg, wparam, lparam):
            # 只记格式元数据（不读内容）；内容仅在 op=clipboard 召唤链路读一次
            if msg == WM_CLIPBOARDUPDATE:
                formats = _clipboard_formats()
                ts = time.time()
                with _clip_lock:
                    _clip_state["ts"] = ts
                    _clip_state["formats"] = formats
                try:
                    _respond({"event": "clipboard", "formats": formats, "ts": ts})
                except OSError:
                    pass
                return 0
            return _user32.DefWindowProcW(c_hwnd, msg, wparam, lparam)

        cb = _WINEVENTPROC(hook_proc)
        hook = _user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
            0, cb, 0, 0, WINEVENT_OUTOFCONTEXT,
        )
        if not hook:
            return
        # M7：同线程挂剪贴板监听（message-only 窗；失败只丢信号不拖垮身份流）
        clip_cb = _WNDPROC(clip_wndproc)
        clip_hwnd = None
        wndcls = _WNDCLASSW()
        wndcls.lpfnWndProc = clip_cb
        wndcls.lpszClassName = "AgentDeckSceneClip"
        try:
            if _user32.RegisterClassW(ctypes.byref(wndcls)):
                clip_hwnd = _user32.CreateWindowExW(
                    0, wndcls.lpszClassName, "", 0, 0, 0, 0, 0,
                    wt.HANDLE(HWND_MESSAGE), None, None, None)
                if clip_hwnd and not _user32.AddClipboardFormatListener(clip_hwnd):
                    _user32.DestroyWindow(clip_hwnd)
                    clip_hwnd = None
        except Exception:
            clip_hwnd = None
        msg = wt.MSG()
        # WM_QUIT 时 GetMessage 返回 0（-1 为错误，同样退出）
        while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        if clip_hwnd:
            _user32.RemoveClipboardFormatListener(clip_hwnd)
            _user32.DestroyWindow(clip_hwnd)
        _user32.UnhookWinEvent(hook)

    def probe_loop() -> None:
        """单飞探测/capture：队列最新胜；线程 MTA COM + UiaSource 惰性初始化一次。"""
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        source = None
        source_tried = False
        while True:
            item = probes.get()
            if item is None:
                return
            while True:  # drain：只处理最新请求；被换下的旧项回包（宿主按 id 等待）
                try:
                    nxt = probes.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    return
                try:
                    _respond({"id": item[0], "status": "error", "error": "superseded",
                              "identity": None, "scenario": "generic",
                              "materials": [], "coverage": "none", "note": ""})
                except OSError:
                    return
                item = nxt
            rid, hwnd, op, tier, l1_materials = item
            if not source_tried:
                source_tried = True
                try:
                    source = scene_probe.UiaSource()
                except Exception:
                    source = None
            try:
                if op == "clipboard":
                    snap = read_clipboard_snapshot()
                    _respond({"id": rid, "status": "ok",
                              "clipboard": {"files": snap["files"],
                                            "has_text": snap["has_text"],
                                            "has_html": snap["has_html"],
                                            "age_s": clipboard_age_s()},
                              "note": snap["note"]})
                    continue
                if op == "capture":
                    # capture 在独立无 COM 线程执行：探线程是 MTA 但不泵消息，
                    # PrintWindow(RENDERFULLCONTENT) 的 DWM 回调在非泵线程上有
                    # 死锁前科；join 超时兜底（超时只丢材料不拖垮探线程）
                    box = {}

                    def _do_capture(h=hwnd, out=box):
                        session = _get_capture_session()
                        if session is None:
                            # 会话建不起来（极端环境）退回一次性子进程
                            out["m"] = scene_capture.capture_material(h)
                        else:
                            out["m"] = session.get(h, timeout=12)

                    t = threading.Thread(target=_do_capture, daemon=True)
                    t.start()
                    t.join(15)
                    if t.is_alive():
                        _respond({"id": rid, "status": "ok", "material": None,
                                  "note": "capture 超时（窗口渲染无响应）"})
                        continue
                    material = box.get("m")
                    fuse_note = ""
                    if material is not None and l1_materials:
                        # M6 融合管线：OCR 行与 L1 材料去重后再出境
                        try:
                            material, fuse_note = scene_fusion.merge_material(
                                l1_materials, material)
                        except Exception:  # noqa: BLE001 融合失败不丢原材料
                            fuse_note = ""
                    note = (material or {}).pop("caveat", "") or ""
                    if fuse_note:
                        note = f"{note}；{fuse_note}" if note else fuse_note
                    _respond({"id": rid, "status": "ok", "material": material,
                              "note": note})
                    continue
                identity = capture_window(hwnd) if hwnd else None
                result = scene_probe.probe_window(hwnd, identity or {}, source, tier=tier)
                _respond({"id": rid, "status": "ok", "identity": identity,
                          "tier": tier, **result})
            except OSError:
                return  # 宿主管道已断
            except Exception as e:  # noqa: BLE001 探测兜底：单窗失败不当拖垮适配器
                _respond({"id": rid, "status": "error", "error": f"{op}_failed:{e}",
                          "identity": None, "scenario": "generic",
                          "materials": [], "coverage": "none", "note": ""})

    capture_session: "scene_capture.CaptureWorkerSession | None" = None

    def _get_capture_session():
        """常驻采集会话：惰性建立，死亡自动重建（导入成本只付一次）。"""
        nonlocal capture_session
        if capture_session is None or capture_session.dead:
            try:
                capture_session = scene_capture.CaptureWorkerSession()
            except Exception:
                capture_session = None
        return capture_session

    probes: "queue.Queue[tuple[int, int, str, str, list] | None]" = queue.Queue()
    writer = threading.Thread(target=writer_loop, name="scene-writer", daemon=True)
    writer.start()
    prober = threading.Thread(target=probe_loop, name="scene-probe", daemon=True)
    prober.start()
    hook_thread = threading.Thread(target=hook_thread_main, name="scene-hook", daemon=True)
    hook_thread.start()
    # 启动即采集一次当前前台（宿主首次 snapshot 前就有数据）
    events.put(_user32.GetForegroundWindow() or 0)

    for raw in sys.stdin:
        raw = raw.strip().lstrip("\ufeff")
        if not raw:
            continue
        req, err = _validate(raw)
        if req is None:
            _respond({"id": -1, "status": "error", "current": None,
                      "timeline": [], "error": err})
            continue
        if req["op"] == "ping":
            _respond({"id": req["id"], "status": "ok", "current": None, "timeline": []})
            continue
        if req["op"] in ("probe", "capture", "clipboard"):
            probes.put((req["id"], req.get("hwnd", 0), req["op"],
                        req.get("tier", "full"), req.get("materials", [])))
            continue
        _respond({"id": req["id"], "status": "ok", **state.snapshot()})

    # stdin 关闭（宿主退出）：拆钩收线
    events.put(None)
    probes.put(None)
    if hook_thread_id:
        _user32.PostThreadMessageW(hook_thread_id, WM_QUIT, 0, 0)
    hook_thread.join(timeout=2)
    writer.join(timeout=2)
    prober.join(timeout=3)
    if capture_session is not None:
        capture_session.close()


if __name__ == "__main__":
    main()
