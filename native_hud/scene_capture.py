"""L2 视觉兜底：PrintWindow 截窗 + 本地 OCR（M4）。

红线（.scratch/scene-m4/spec.md）：
- **DPI 感知先行**：进程启动时设置 Per-Monitor 感知，否则高 DPI 下截屏 1:1
  裁切左上角（2880x1800@200% 实锤教训）；
- **图片不落盘**：位图用后即弃，只产出 OCR 文本材料（≤MATERIAL_TEXT_BUDGET）；
- PrintWindow(PW_RENDERFULLCONTENT) 优先（prevFg 被 HUD 遮挡仍可整窗截取），
  失败/空图降级 BitBlt 屏幕区域（遮挡风险记 note）；
- rapidocr 惰性单例，未装/失败返回 None + 人话原因——不拖垮探测主链路。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import threading
import time

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_shcore = None
try:  # Win8.1+；更老的系统走 SetProcessDPIAware 降级
    _shcore = ctypes.WinDLL("shcore")
except OSError:
    pass

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0

#: OCR 文本预算（与 scene_probe.MATERIAL_TEXT_MAX 对齐）
MATERIAL_TEXT_BUDGET = 2400
#: 超过此宽度先等比降采样再 OCR（速度/精度平衡）
OCR_MAX_WIDTH = 1600
#: 行级结构上限（融合去重的输入；超出截断）
MAX_OCR_LINES = 120
MAX_OCR_LINE_CHARS = 160

_dpi_done = threading.Event()


def ensure_dpi_aware() -> bool:
    """进程级 Per-Monitor DPI 感知；幂等。返回是否设置成功（失败只影响截图像素）。"""
    if _dpi_done.is_set():
        return getattr(ensure_dpi_aware, "ok", False)
    ok = False
    if _shcore is not None:
        try:
            # PER_MONITOR_DPI_AWARE_V2=2；已被设置会报错，容忍
            if _shcore.SetProcessDpiAwareness(2) == 0:
                ok = True
        except Exception:
            pass
    if not ok:
        try:
            ok = bool(_user32.SetProcessDPIAware())
        except Exception:
            ok = False
    ensure_dpi_aware.ok = ok  # type: ignore[attr-defined]
    _dpi_done.set()
    return ok


class _RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _blank(buf: ctypes.Array, w: int, h: int) -> bool:
    """纯色/近纯色检测（PrintWindow 对部分窗口会返回空图）。"""
    step = max(1, (len(buf) // 4096)) * 4
    first = bytes(buf[:4])
    for off in range(0, len(buf) - 4, step):
        if bytes(buf[off:off + 4]) != first:
            return False
    return True


def capture_window_bytes(hwnd: int):
    """截窗 → (w, h, BGRA bytes) 或 None。PrintWindow 优先，BitBLt 兜底。"""
    ensure_dpi_aware()
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0 or w > 8000 or h > 8000:
        return None

    def _grab(render_full: bool):
        hdc_window = _user32.GetWindowDC(hwnd) if render_full else _user32.GetDC(0)
        if not hdc_window:
            return None
        try:
            mem_dc = _gdi32.CreateCompatibleDC(hdc_window)
            bmp = _gdi32.CreateCompatibleBitmap(hdc_window, w, h)
            old = _gdi32.SelectObject(mem_dc, bmp)
            try:
                ok = False
                if render_full:
                    ok = bool(_user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))
                    if not ok:
                        return None
                else:
                    # BitBLt：窗口已被遮挡时截到的是屏幕现状（调用方记 note）
                    ok = bool(_gdi32.BitBlt(mem_dc, 0, 0, w, h, hdc_window,
                                            rect.left, rect.top, SRCCOPY))
                    if not ok:
                        return None
                bmi = _BMIH()
                bmi.biSize = ctypes.sizeof(_BMIH)
                bmi.biWidth = w
                bmi.biHeight = -h  # 自上而下
                bmi.biPlanes = 1
                bmi.biBitCount = 32
                bmi.biCompression = BI_RGB
                buf = ctypes.create_string_buffer(w * h * 4)
                got = _gdi32.GetDIBits(
                    hdc_window, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS
                )
                if not got:
                    return None
                return bytes(buf)
            finally:
                _gdi32.SelectObject(mem_dc, old)
                _gdi32.DeleteObject(bmp)
                _gdi32.DeleteDC(mem_dc)
        finally:
            if render_full:
                _user32.ReleaseDC(hwnd, hdc_window)
            else:
                _user32.ReleaseDC(0, hdc_window)

    data = _grab(render_full=True)
    if data is not None and not _blank(data, w, h):
        return w, h, data, None
    note = None
    if data is None:
        note = "PrintWindow 失败，改用屏幕区域截图（可能含遮挡）"
    else:
        note = "PrintWindow 空图，改用屏幕区域截图（可能含遮挡）"
    data2 = _grab(render_full=False)
    if data2 is not None and not _blank(data2, w, h):
        return w, h, data2, note
    return None


_ocr_lock = threading.Lock()
_ocr_state: dict = {"engine": ..., "reason": ""}  # engine: ...=未初始化 / None=不可用


def order_lines(result, *, row_ratio: float = 0.6) -> list[dict]:
    """rapidocr 原始结果 → 阅读顺序行 [{text, box, score, row}]（纯逻辑，M6）。

    行聚类：按 y 中心升序扫描，与既有行 y 中心距 ≤ row_ratio×行高即归该行
    （多栏版面同行并列）；行内按 x 左缘排序。box 为四点包围盒 [x, y, w, h]。
    """
    entries: list[dict] = []
    for item in result or []:
        if not item or len(item) < 3:
            continue
        pts, text, score = item[0], str(item[1]).strip(), item[2]
        if not text or not pts:
            continue
        try:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
        except (TypeError, ValueError, IndexError):
            continue
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        entries.append({"text": text, "score": float(score),
                        "box": [x0, y0, x1 - x0, y1 - y0], "_cy": (y0 + y1) / 2})
    entries.sort(key=lambda e: (e["_cy"], e["box"][0]))
    rows: list[dict] = []
    for e in entries:
        for row in rows:
            if abs(e["_cy"] - row["cy"]) <= row_ratio * max(row["h"], e["box"][3], 1.0):
                row["items"].append(e)
                row["cy"] = sum(i["_cy"] for i in row["items"]) / len(row["items"])
                row["h"] = max(row["h"], e["box"][3])
                break
        else:
            rows.append({"cy": e["_cy"], "h": e["box"][3], "items": [e]})
    lines: list[dict] = []
    for row_idx, row in enumerate(rows):
        row["items"].sort(key=lambda e: e["box"][0])
        for e in row["items"]:
            lines.append({"text": e["text"], "box": e["box"],
                          "score": e["score"], "row": row_idx})
    return lines


def lines_to_text(lines: list[dict]) -> str:
    """阅读顺序行 → 文本：同行单元两空格分隔，行间换行。"""
    out: list[str] = []
    prev_row = None
    for ln in lines:
        text = str(ln.get("text", "")).strip()
        if not text:
            continue
        if prev_row is not None and ln.get("row") == prev_row and out:
            out[-1] = out[-1] + "  " + text
        else:
            out.append(text)
        prev_row = ln.get("row")
    return "\n".join(out)


def ocr_lines(w: int, h: int, bgra: bytes):
    """rapidocr 识别 → ([{text, box, score, row}] | None, 原因)。

    box 还原为原图（窗口）物理像素；引擎惰性单例，未装/失败返回 (None, 原因)。
    """
    with _ocr_lock:
        if _ocr_state["engine"] is ...:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _ocr_state["engine"] = RapidOCR()
            except Exception as e:  # noqa: BLE001 依赖缺席/模型加载失败
                _ocr_state["engine"] = None
                _ocr_state["reason"] = f"OCR 不可用（{type(e).__name__}）"
        engine = _ocr_state["engine"]
        unavailable = _ocr_state["reason"]
    if engine is None:
        return None, unavailable
    try:
        from PIL import Image

        img = Image.frombytes("RGBA", (w, h), bgra).convert("RGB")
        scale = 1.0
        if w > OCR_MAX_WIDTH:
            scale = w / OCR_MAX_WIDTH
            img = img.resize((OCR_MAX_WIDTH, max(1, int(h / scale))))
        import numpy as np

        result, _ = engine(np.asarray(img))
    except Exception as e:  # noqa: BLE001
        return None, f"OCR 执行失败（{type(e).__name__}）"
    lines = order_lines(result)
    if not lines:
        return None, "OCR 未识别到文字"
    for ln in lines:  # 降采样坐标还原 + 整型化（控制 JSON 体积）
        x, y, bw, bh = ln["box"]
        ln["box"] = [int(round(x * scale)), int(round(y * scale)),
                     int(round(bw * scale)), int(round(bh * scale))]
        ln["text"] = ln["text"][:MAX_OCR_LINE_CHARS]
    return lines[:MAX_OCR_LINES], None


def ocr_bytes(w: int, h: int, bgra: bytes):
    """OCR 整段文本（阅读顺序拼接）；未装/失败/空结果返回 (None, 原因)。"""
    lines, note = ocr_lines(w, h, bgra)
    if lines is None:
        return None, note
    text = lines_to_text(lines)
    if not text.strip():
        return None, "OCR 未识别到文字"
    return text[:MATERIAL_TEXT_BUDGET], None


def _dbg(msg):
    import os
    import sys
    if os.environ.get("QD_SCENE_DEBUG"):
        print(f"[capture {time.time():.2f}] {msg}", file=sys.stderr, flush=True)


def capture_material_inline(hwnd: int) -> "dict | None":
    """本进程内执行：截窗→OCR→材料（含行级结构，供融合去重）。worker 子进程与单测使用。"""
    got = capture_window_bytes(hwnd)
    if got is None:
        return None
    w, h, bgra, note = got
    lines, ocr_note = ocr_lines(w, h, bgra)
    if lines is None:
        return None
    import scene_probe

    m = scene_probe.make_material("screen_text", "ocr",
                                  lines_to_text(lines)[:MATERIAL_TEXT_BUDGET])
    m["lines"] = [{"text": ln["text"], "box": ln["box"]} for ln in lines]
    caveat = note or ocr_note or ""
    if caveat:
        m["caveat"] = caveat
    return m


def capture_material(hwnd: int, *, timeout: float = 25.0) -> "dict | None":
    """完整 L2 兜底（默认独立子进程执行，见 scene_capture_worker 的理由栏）。

    子进程输出一行 JSON（材料或 null）；超时/异常返回 None。
    """
    import subprocess
    import sys

    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scene_capture_worker.py")
    try:
        r = subprocess.run(
            [sys.executable, "-B", "-u", worker, str(hwnd)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=os.path.dirname(worker),
            creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    line = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    if not line or line == "null":
        return None
    try:
        m = json.loads(line)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


class CaptureWorkerSession:
    """常驻采集会话：--loop worker 进程 + 按行请求应答。

    常驻动机：从适配器（comtypes/WinEventHook/GDI 常驻线程环境）派生的子进程
    里，rapidocr/onnxruntime 的装载链实测会被拖到 25s+（loader-lock 类相互作
    用，根因未定案）；常驻后导入只在启动付一次，探测退化为一次 IPC 往返。
    会话死亡（EOF/超时）自动置弃，由所有者重建。
    """

    def __init__(self):
        import queue as _queue
        import subprocess
        import sys
        import threading as _threading

        self._q: "queue.Queue[object]" = _queue.Queue()
        self._lock = _threading.Lock()
        self._dead = False
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "scene_capture_worker.py")
        self._proc = subprocess.Popen(
            [sys.executable, "-B", "-u", worker, "--loop"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=os.path.dirname(worker),
            creationflags=0x08000000 if os.name == "nt" else 0,
        )

        def reader():
            try:
                for line in self._proc.stdout:
                    self._q.put(line)
            except OSError:
                pass
            finally:
                self._dead = True
                self._q.put(None)

        _threading.Thread(target=reader, name="capture-worker-reader",
                          daemon=True).start()

    @property
    def dead(self) -> bool:
        return self._dead

    def get(self, hwnd: int, *, timeout: float = 12.0) -> "dict | None":
        """一次采集往返；超时/会话异常返回 None 并置弃。"""
        import queue as _queue

        if self._dead or hwnd <= 0:
            return None
        try:
            with self._lock:
                self._proc.stdin.write(json.dumps({"hwnd": hwnd}) + "\n")
                self._proc.stdin.flush()
                # 丢掉可能残留的旧应答，保持一问一答对齐
                while True:
                    try:
                        self._q.get_nowait()
                    except _queue.Empty:
                        break
                item = self._q.get(timeout=timeout)
        except (OSError, _queue.Empty, ValueError):
            self._dead = True
            return None
        if item is None:
            self._dead = True
            return None
        line = str(item).strip()
        if not line or line == "null":
            return None
        try:
            m = json.loads(line)
        except ValueError:
            return None
        return m if isinstance(m, dict) else None

    def close(self) -> None:
        self._dead = True
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
