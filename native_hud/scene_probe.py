"""L1 现场内容探测：场景匹配 + 受限采集（M2）。

结构（spec .scratch/scene-m2/）：
- 纯逻辑（全可测）：场景注册表 match_scenario、标题文件名解析 parse_file_from_title、
  材料预算与摘要 make_material；
- 采集源（可注入）：UiaSource 真实实现（comtypes + UIAutomationCore typelib；
  缺席时降级）与 Win32 弹窗采集（EnumChildWindows，无 UIA 依赖）；
- probe_window：按场景取材料，逐项 try——单点失败降 coverage，不整体失败。

红线：IsPassword 一律跳过；元素/字符预算硬上限；generic 场景只给身份不采集。
"""
from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes as wt

# ---- 场景注册表（纯逻辑） ----

IDE_PROCESSES = {
    "code.exe", "code - ins.exe", "idea64.exe", "pycharm64.exe", "webstorm64.exe",
    "goland64.exe", "rustrover64.exe", "rider64.exe", "datagrip64.exe",
    "notepad++.exe", "sublime_text.exe", "notepad.exe",
}
TERMINAL_PROCESSES = {
    "windowsterminal.exe", "wt.exe", "conhost.exe", "mintty.exe",
    "wezterm-gui.exe", "alacritty.exe",
}
TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "mintty"}
DIALOG_CLASSES = {"#32770"}

SCENARIOS = ("ide", "dialog", "terminal", "generic")


def match_scenario(process: str, title: str, win_class: str) -> str:
    p = (process or "").lower()
    c = (win_class or "").strip()
    if c in DIALOG_CLASSES:
        return "dialog"
    if p in IDE_PROCESSES:
        return "ide"
    if p in TERMINAL_PROCESSES or c in TERMINAL_CLASSES:
        return "terminal"
    return "generic"


_TITLE_SPLIT = re.compile(r"\s+[-—–]\s+")
_HAS_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_TITLE_NOISE = "*➜●✎✳⟳◦·"


def parse_file_from_title(title: str) -> str | None:
    """IDE/编辑器标题 → 文件名/路径。

    惯例样本："file.py — dir — VSCode"、"file.py - PyCharm"、"*file.py - 记事本"。
    取首段、去修改标记；有扩展名或路径分隔符才认为有效。
    """
    seg = _TITLE_SPLIT.split(title or "", maxsplit=1)[0].strip()
    seg = seg.lstrip(_TITLE_NOISE).strip()
    if not seg or "\n" in seg:
        return None
    if _HAS_EXT.search(seg) or "\\" in seg or "/" in seg:
        return seg[:260]
    return None


# ---- 预算（spec 决策 4） ----

MAX_ELEMENTS = 300
MAX_SELECTION_CHARS = 2000
MAX_DOCUMENT_TAIL = 2000
MAX_VALUE_CHARS = 500
PREVIEW_CHARS = 80
MATERIAL_TEXT_MAX = 2400


def make_material(kind: str, source: str, text: str, lines: "list[dict] | None" = None) -> dict:
    """文本 → 材料条目（带摘要与字符数；预览脱去首尾空白）。

    lines：可选的行级结构 [{text, box}]——box 为窗口相对物理像素，
    供融合管线（scene_fusion）做 bbox 级去重。
    """
    text = (text or "").strip()
    preview = " ".join(text.split())
    if len(preview) > PREVIEW_CHARS:
        preview = preview[: PREVIEW_CHARS - 1] + "…"
    m = {
        "kind": kind,
        "source": source,
        "chars": len(text),
        "preview": preview,
        # 全文仅供现场包冻结（M3）；展示层只用 preview（spec 决策 2）
        "text": text[:MATERIAL_TEXT_MAX],
    }
    if lines:
        m["lines"] = lines
    return m


# ---- UIA 采集源（真实实现；测试注入伪源） ----

class UiaSource:
    """comtypes + UIAutomationCore。线程须已 CoInitializeEx(MTA)。

    全部方法独立 try——任何 COM 失败返回 None（调用方降 coverage）。
    """

    def __init__(self):
        import comtypes
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import (
            CUIAutomation8,
            IUIAutomation,
            IUIAutomationTextPattern,
            UIA_TextPatternId,
        )

        self._uia = comtypes.client.CreateObject(CUIAutomation8, interface=IUIAutomation)
        self._IUIAutomationTextPattern = IUIAutomationTextPattern
        self._UIA_TextPatternId = UIA_TextPatternId

    def _element(self, hwnd: int):
        return self._uia.ElementFromHandle(hwnd)

    def selection(self, hwnd: int, limit: int = MAX_SELECTION_CHARS) -> str | None:
        """TextPattern.GetSelection 合成文本（选区为空返回 ""）。"""
        el = self._element(hwnd)
        if el.CurrentIsPassword:
            return None
        pat = el.GetCurrentPattern(self._UIA_TextPatternId)
        if pat is None:
            return None
        tp = pat.QueryInterface(self._IUIAutomationTextPattern)
        parts = []
        for rng in tp.GetSelection():
            text = rng.GetText(-1)
            if text:
                parts.append(text)
            if sum(len(p) for p in parts) >= limit:
                break
        joined = "\n".join(parts)[:limit]
        return joined

    def document_tail(self, hwnd: int, limit: int = MAX_DOCUMENT_TAIL) -> str | None:
        """DocumentRange 全文取尾部（终端可见缓冲区等）。"""
        el = self._element(hwnd)
        if el.CurrentIsPassword:
            return None
        pat = el.GetCurrentPattern(self._UIA_TextPatternId)
        if pat is None:
            return None
        tp = pat.QueryInterface(self._IUIAutomationTextPattern)
        full = tp.DocumentRange.GetText(65536) or ""
        full = full[-limit:]
        return full if full.strip() else None


# ---- Win32 弹窗采集（无 UIA 依赖） ----

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_ENUM_CHILD_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


def collect_dialog_texts(hwnd: int, *, max_items: int = 24) -> dict | None:
    """#32770 的 Static/Button 文本 + 窗口相对矩形（融合管线 bbox 去重用）。

    DirectUIHWND 内容 HWND 不可见 → partial。
    """
    statics: list[str] = []
    buttons: list[str] = []
    items: list[dict] = []
    seen_dui = False
    win_rect = _RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(win_rect))

    def on_child(child, _lparam):
        nonlocal seen_dui
        cls = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(child, cls, 64)
        cls_name = cls.value
        if cls_name == "DirectUIHWND":
            seen_dui = True
        buf = ctypes.create_unicode_buffer(512)
        n = _user32.GetWindowTextW(child, buf, 512)
        text = buf.value[: max(n, 0)].strip()
        if text and len(statics) + len(buttons) < max_items:
            role = ""
            if cls_name.startswith("Button"):
                role = "button"
                buttons.append(text[:120])
            elif cls_name == "Static":
                role = "static"
                statics.append(text[:400])
            if role:
                rc = _RECT()
                box = None
                if _user32.GetWindowRect(child, ctypes.byref(rc)):
                    box = [rc.left - win_rect.left, rc.top - win_rect.top,
                           rc.right - rc.left, rc.bottom - rc.top]
                item: dict = {"text": text[:400], "role": role}
                if box:
                    item["box"] = box
                items.append(item)
        return True

    _user32.EnumChildWindows(hwnd, _ENUM_CHILD_PROC(on_child), 0)
    # 兼容旧式消息框：主窗口自身文本常是标题行之外的正文
    coverage = "partial" if seen_dui else "full"
    if not statics and not buttons and not seen_dui:
        return None
    return {"statics": statics, "buttons": buttons, "items": items, "coverage": coverage}


# ---- 探测主入口 ----

def probe_window(hwnd: int, identity: dict, source, *, clock=time.time,
                 tier: str = "full") -> dict:
    """对指定窗口做一次 L1 探测。source 需提供 selection/document_tail（可为伪源）。

    tier="fast"（M6 感知分档）：跳过 UIA——ide 只给标题解析、terminal 只给身份、
    dialog 的 Win32 枚举本就廉价照常执行；覆盖 ≠ full 时由宿主升级 full 档。
    返回 {scenario, materials, coverage, note}；身份由调用方附带。
    """
    process = (identity or {}).get("process", "")
    title = (identity or {}).get("title", "")
    win_class = (identity or {}).get("class", "")
    scenario = match_scenario(process, title, win_class)
    fast = tier == "fast"
    materials: list[dict] = []
    coverage = "full"
    notes: list[str] = []

    if scenario == "generic":
        # 隐私保守缺省：无适配器的应用只给身份，不做深采集（spec 决策 2）
        return {"scenario": scenario, "materials": [], "coverage": "none",
                "note": "未识别场景：仅现场身份"}

    if scenario == "ide":
        fname = parse_file_from_title(title)
        if fname:
            materials.append(make_material("file", "title", fname))
        else:
            notes.append("标题未解析出文件名")
        if fast:
            coverage = "partial"
            notes.append("快档：未做 UIA 选区拉取")
        elif source is not None:
            try:
                sel = source.selection(hwnd)
            except Exception:
                sel = None
                notes.append("选区不可得（UIA）")
            if sel:
                materials.append(make_material("selection", "uia:text", sel))
            elif sel is None and "选区不可得（UIA）" not in notes:
                notes.append("无选区或无 TextPattern（Electron 需开辅助功能）")
        else:
            coverage = "partial"
            notes.append("UIA 不可用（comtypes 未装）")

    elif scenario == "dialog":
        got = collect_dialog_texts(hwnd)
        if got is None:
            coverage = "partial"
            notes.append("弹窗文本未取得")
        else:
            if got["coverage"] == "partial":
                coverage = "partial"
                notes.append("现代弹窗(DirectUI)文本可能不全")
            if got["statics"]:
                materials.append(make_material(
                    "dialog_text", "win32:static", "\n".join(got["statics"]),
                    lines=[{"text": it["text"], "box": it["box"]}
                           for it in got.get("items") or []
                           if it.get("role") == "static" and it.get("box")]))
            if got["buttons"]:
                materials.append(make_material(
                    "dialog_buttons", "win32:button", " | ".join(got["buttons"]),
                    lines=[{"text": it["text"], "box": it["box"]}
                           for it in got.get("items") or []
                           if it.get("role") == "button" and it.get("box")]))

    elif scenario == "terminal":
        if fast:
            coverage = "none"
            notes.append("快档：未做 UIA 终端文本拉取")
        elif source is not None:
            try:
                tail = (source.document_tail(hwnd) or "").strip()
            except Exception:
                tail = ""
            if tail:
                materials.append(make_material("terminal_tail", "uia:document", tail))
            else:
                coverage = "partial"
                notes.append("终端文本不可得（UIA）")
        else:
            coverage = "partial"
            notes.append("UIA 不可用（comtypes 未装）")

    return {
        "scenario": scenario,
        "materials": materials,
        "coverage": coverage,
        "note": "；".join(notes),
    }
