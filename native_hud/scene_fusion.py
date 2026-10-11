"""L1/L2 融合管线：OCR 行与 L1 材料去重融合（M6，纯逻辑全可测）。

设计（.scratch/scene-m6/spec.md 决策 1，源自 UFO2 混合检测管线）：
L1（UIA/Win32）已覆盖的内容不再以 OCR 形式重复进 prompt。剔除规则按证据强度
递减：规范化文本包含 → 字符集重叠 ≥0.85 →（双方均有 bbox）IoU>0.3 且重叠>0.5。
保留行维持阅读顺序，重组为新的 screen_text 材料。
"""
from __future__ import annotations

#: 字符集重叠率阈值（OCR 噪声容忍；只高不低的保守方向）
OVERLAP_DROP = 0.85
#: bbox 均可用时的 IoU / 重叠双阈值
IOU_DROP = 0.3
IOU_MIN_OVERLAP = 0.5
#: 短行噪声下限（规范化后少于此字符数直接丢弃）
MIN_LINE_CHARS = 2


def norm_text(s: str) -> str:
    """去全部空白 + 小写（CJK 友好：不含空格参与比较）。"""
    return "".join((s or "").split()).lower()


def char_overlap(line_norm: str, ref_norm: str) -> float:
    """line 字符集被 ref 字符集覆盖的比例（0..1；line 为空返回 0）。"""
    if not line_norm:
        return 0.0
    ref_chars = set(ref_norm)
    line_chars = set(line_norm)
    if not line_chars:
        return 0.0
    return len(line_chars & ref_chars) / len(line_chars)


def iou(a, b) -> float:
    """两个 [x, y, w, h] 框的交并比；任一非法返回 0。"""
    try:
        ax, ay, aw, ah = (float(v) for v in a)
        bx, by, bw, bh = (float(v) for v in b)
    except (TypeError, ValueError):
        return 0.0
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _l1_norms(materials: list[dict]) -> list[str]:
    return [norm_text(m.get("text", "")) for m in materials
            if isinstance(m, dict) and norm_text(m.get("text", ""))]


def _l1_boxes(materials: list[dict]) -> list[tuple[list, str]]:
    """L1 材料 lines 里的 (box, norm_text) 对（M6 决策 3：弹窗 sidecar）。"""
    out: list[tuple[list, str]] = []
    for m in materials:
        if not isinstance(m, dict):
            continue
        for ln in m.get("lines") or []:
            if isinstance(ln, dict) and ln.get("box") and norm_text(ln.get("text", "")):
                out.append((ln["box"], norm_text(ln["text"])))
    return out


def is_covered(line_text: str, line_box, l1_norms: list[str],
               l1_boxes: list[tuple[list, str]]) -> bool:
    """单条 OCR 行是否已被 L1 覆盖（三条剔除规则按序判）。"""
    ln = norm_text(line_text)
    if len(ln) < MIN_LINE_CHARS:
        return True  # 噪声短行视同无信息
    for ref in l1_norms:
        if ln in ref:
            return True
        if char_overlap(ln, ref) >= OVERLAP_DROP:
            return True
    if line_box:
        for box, ref in l1_boxes:
            if iou(line_box, box) > IOU_DROP and char_overlap(ln, ref) > IOU_MIN_OVERLAP:
                return True
    return False


def fuse_lines(l1_materials: list[dict], ocr_lines: list[dict]) -> tuple[list[dict], int]:
    """OCR 行列表滤掉 L1 已覆盖者；返回 (保留行, 剔除数)。输入行序（阅读顺序）保持。"""
    l1_norms = _l1_norms(l1_materials)
    l1_boxes = _l1_boxes(l1_materials)
    if not l1_norms and not l1_boxes:
        return [ln for ln in ocr_lines if len(norm_text(ln.get("text", ""))) >= MIN_LINE_CHARS], 0
    kept: list[dict] = []
    dropped = 0
    for ln in ocr_lines:
        if not isinstance(ln, dict):
            continue
        if is_covered(ln.get("text", ""), ln.get("box"), l1_norms, l1_boxes):
            dropped += 1
        else:
            kept.append(ln)
    return kept, dropped


def merge_material(l1_materials: list[dict], ocr_material: dict | None) -> tuple[dict | None, str]:
    """OCR 材料与 L1 材料融合：去重后重建 screen_text 材料。

    返回 (融合后材料 | None（无新增内容）, 备注)。无 lines 结构（旧 worker）退化为
    整段文本包含判定：被覆盖则整体丢弃。
    """
    if not ocr_material:
        return None, ""
    lines = ocr_material.get("lines")
    if not isinstance(lines, list) or not lines:
        # 无行结构：整段与 L1 比对
        l1_norms = _l1_norms(l1_materials)
        ln = norm_text(ocr_material.get("text", ""))
        covered = bool(ln) and any(ln in ref or char_overlap(ln, ref) >= OVERLAP_DROP
                                   for ref in l1_norms)
        if covered:
            return None, "OCR 文本与 L1 重复，已丢弃"
        return ocr_material, ""
    kept, dropped = fuse_lines(l1_materials, lines)
    if not kept:
        return None, f"OCR {dropped} 行均与 L1 重复，已丢弃"
    import scene_probe  # 延迟导入：纯逻辑自测时不拖入 ctypes/comtypes 链

    text = "\n".join(str(ln.get("text", "")).strip() for ln in kept
                     if str(ln.get("text", "")).strip())
    merged = scene_probe.make_material("screen_text",
                                       ocr_material.get("source", "ocr"), text)
    merged["lines"] = kept
    if ocr_material.get("caveat"):
        merged["caveat"] = ocr_material["caveat"]
    note = f"已剔除与 L1 重复的 {dropped} 行" if dropped else ""
    return merged, note
