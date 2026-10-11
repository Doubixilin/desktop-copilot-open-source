"""编排核心：归类、应用区栏位与文档区落位（纯函数）。

不碰 Win32、文件系统、注册表。输入是任何带 name/kind/index/mtime 属性的
桌面项记录（实机为 desktop_icons.DesktopItem），输出是每项的 Placement：
归到哪一区、占第几个栏位（仅应用区）、目标坐标。
文档区按文档组排布：固定组序、每组一列、组间空一列、组内新在上、
满 8 行折本组右侧相邻列。
"""
from dataclasses import dataclass
from pathlib import Path

import zones_geometry as geo

# 回收站的显示名随系统 UI 语言变化；本机实测为英文
RECYCLE_NAMES = {"Recycle Bin", "回收站"}
APP_KINDS = {"shortcut", "url", "special"}
DOC_KINDS = {"file", "folder"}

# 文档组固定组序：位置可预测是"分区"能靠肌肉记忆使用的前提
GROUP_ORDER = ("folders", "office", "pdf", "image", "archive", "other")
OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".csv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tif", ".tiff"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}


@dataclass(frozen=True)
class Placement:
    name: str
    zone: str  # app | doc | recycle | untouched
    slot: int | None
    pos: tuple[int, int] | None
    source: str | None = None  # 应用区栏位来源：pinned | recommended


def classify(item):
    if item.kind == "system":
        return "untouched"
    if item.name in RECYCLE_NAMES:
        return "recycle"
    if item.kind in DOC_KINDS:
        return "doc"
    if item.kind in APP_KINDS:
        return "app"
    return "untouched"


def doc_group(item):
    if item.kind == "folder":
        return "folders"
    ext = Path(item.name).suffix.lower()
    if ext in OFFICE_EXTS:
        return "office"
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return "other"


def _columns_used(count):
    """一个文档组占几列：满 DOC_MAX_ROWS 行折一列。"""
    return -(-count // geo.DOC_MAX_ROWS)


def _doc_placements(doc_items):
    """文档区落位：固定组序、每组一列、组间空一列、组内新在上、满 8 行折右侧相邻列。"""
    groups = {g: [] for g in GROUP_ORDER}
    for item in doc_items:
        groups[doc_group(item)].append(item)
    for members in groups.values():
        members.sort(key=lambda i: (-(i.mtime if i.mtime is not None else 0.0), i.name))

    placements = {}
    col = 0
    for group in GROUP_ORDER:
        members = groups[group]
        if not members:
            continue
        for offset, item in enumerate(members):
            c = col + offset // geo.DOC_MAX_ROWS
            r = offset % geo.DOC_MAX_ROWS
            pos = geo.doc_cell(c, r)
            # 越过避让线就不再落位：宁可留空也不压右栏
            placements[item.index] = Placement(item.name, "doc", None, pos if pos[0] < geo.AVOID_X else None)
        col += _columns_used(len(members)) + 1  # +1 为组间空列
    return placements


def _dedupe(names):
    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def plan_layout(items, pinned=(), scores=None):
    """算出编排计划。pinned 为手钉显示名有序清单；scores 为 {显示名: 使用频次分数}。

    应用区栏位：手钉按清单顺序占最前的栏位，剩余栏位由推荐按分数降序填补；
    没有分数的快捷方式分数记 0，仍参与填补，不被丢弃；手钉永不被推荐顶替。
    内部以 index 为键：用户桌面与公共桌面可能出现同名项，以 name 为键会互相覆盖。
    """
    scores = scores or {}
    present = {i.name for i in items}
    pinned_order = [n for n in _dedupe(pinned) if n in present]
    pinned_set = set(pinned_order)
    app_items = [i for i in items if classify(i) == "app"]
    # 两段拼接而非混合排序键：手钉段按清单顺序，推荐段按分数降序、同分按当前 index
    ordered = sorted(
        (i for i in app_items if i.name in pinned_set),
        key=lambda i: pinned_order.index(i.name),
    ) + sorted(
        (i for i in app_items if i.name not in pinned_set),
        key=lambda i: (-scores.get(i.name, 0.0), i.index),
    )

    placements = {}
    slot = 0
    for item in ordered:
        source = "pinned" if item.name in pinned_set else "recommended"
        if slot < geo.APP_SLOTS:
            col, row = slot % geo.APP_COLS, slot // geo.APP_COLS
            placements[item.index] = Placement(item.name, "app", slot, geo.app_slot(col, row), source)
            slot += 1
        else:
            # 栏位已满：归类仍为 app 但不落位；source 保留身份，dry-run 仍认得出手钉
            placements[item.index] = Placement(item.name, "app", None, None, source)

    placements.update(_doc_placements([i for i in items if classify(i) == "doc"]))

    for item in items:
        if item.index in placements:
            continue
        zone = classify(item)
        if zone == "recycle":
            placements[item.index] = Placement(item.name, zone, None, geo.RECYCLE_POS)
        else:
            placements[item.index] = Placement(item.name, zone, None, None)

    return [placements[i.index] for i in items]
