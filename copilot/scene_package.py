"""现场包冻结：把召唤瞬间捕获的现场固化为不可变 JSON（M3）。

契约（spec .scratch/scene-m3/ 决策 1/3）：八字段精简——schema/package_id/
created_at/task/scene/materials/integrity；材料带 {kind, source, tool, chars,
sha256, captured_at, text}，出处映射人话；完整性显性化（caveats 收一切降级
原因，"未识别场景仅身份"也算）。包文件原子写、写后不改写；重新发起=新包。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SCENE_PACKAGE_SCHEMA = 1

#: prompt 现场块里材料文本合计上限（现场块是上下文不是主体）
SCENE_BLOCK_TEXT_BUDGET = 4000

#: source → 人话出处（tool 字段）
SOURCE_TOOLS = {
    "uia:text": "UIA TextPattern",
    "uia:document": "UIA DocumentRange",
    "win32:static": "Win32 子窗口文本",
    "win32:button": "Win32 子窗口按钮",
    "title": "窗口标题",
}

#: 浏览器现场身份的保留键（票02 预留：扩展采集接入时直接落包，不改 schema）
_BROWSER_IDENTITY_KEYS = (
    "browser_session_id", "client_id", "tab_id", "document_id",
    "url", "content_revision",
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_material(m: dict[str, Any]) -> Optional[dict[str, Any]]:
    """探测材料 → 冻结材料；结构坏/无文本返回 None（integrity 记缺口）。

    obtained 显式区分"内容已取得"与"仅引用线索"（链接存在≠已审阅）；
    首版桌面材料均为文本已取得，链接类材料由浏览器采集票（11）启用。
    """
    if not isinstance(m, dict):
        return None
    kind = str(m.get("kind", "")).strip()
    source = str(m.get("source", "")).strip()
    text = m.get("text")
    if not kind or not isinstance(text, str) or not text.strip():
        return None
    return {
        "kind": kind[:40],
        "source": source[:40],
        "tool": SOURCE_TOOLS.get(source, source or "未知"),
        "chars": len(text),
        "sha256": _sha256(text),
        "captured_at": m.get("ts") or m.get("captured_at"),
        "obtained": bool(m.get("obtained", True)),
        "text": text,
    }


def freeze_scene_package(
    prompt: str,
    probe: Optional[dict[str, Any]],
    *,
    package_dir: Path,
    clock=time.time,
) -> dict[str, Any]:
    """把探测结果冻结为现场包并原子落盘。probe 为 None/空按"无现场"冻结。"""
    probe = probe or {}
    package_id = f"pkg-{uuid.uuid4().hex[:12]}"
    created_at = clock()

    raw_materials = probe.get("materials") or []
    if not isinstance(raw_materials, list):
        raw_materials = []
    materials = []
    missing = []
    for i, m in enumerate(raw_materials):
        cleaned = _clean_material(m)
        if cleaned is None:
            missing.append(f"材料#{i + 1} 结构不完整，未入包")
        else:
            materials.append(cleaned)

    identity = probe.get("identity")
    if not isinstance(identity, dict):
        identity = {}
    scenario = str(probe.get("scenario", "")).strip() or "unknown"
    coverage = str(probe.get("coverage", "none")).strip()
    scene_id = str(probe.get("scene_id", "")).strip()
    generation = probe.get("generation")
    generation = int(generation) if isinstance(generation, (int, float)) and generation > 0 else 0

    caveats = [c for c in (str(probe.get("note", "")).strip(),) if c]
    if not materials:
        caveats.append("无材料入包（仅现场身份或探测未取得内容）")
    if coverage != "full":
        caveats.append(f"材料覆盖：{coverage or 'none'}")

    frozen_identity = {
        "pid": identity.get("pid"),
        "process": identity.get("process", ""),
        "exe": identity.get("exe", ""),
        "title": identity.get("title", ""),
        "class": identity.get("class", ""),
        "captured_at": identity.get("ts"),
    }
    # 浏览器身份结构预留：存在即落包（tab/document/url 等），桌面场景缺席不影响
    for k in _BROWSER_IDENTITY_KEYS:
        if identity.get(k) is not None:
            frozen_identity[k] = identity.get(k)

    scene_meta: dict[str, Any] = {
        "scenario": scenario,
        "coverage": coverage or "none",
        "identity": frozen_identity,
    }
    if scene_id:
        scene_meta["scene_id"] = scene_id
    if generation:
        scene_meta["generation"] = generation

    package = {
        "schema": SCENE_PACKAGE_SCHEMA,
        "package_id": package_id,
        "created_at": created_at,
        "task": {"prompt": prompt, "prompt_chars": len(prompt)},
        "scene": scene_meta,
        "materials": materials,
        "integrity": {
            "materials_count": len(materials),
            "chars_total": sum(m["chars"] for m in materials),
            "caveats": caveats,
        },
    }
    _persist(package_dir, package)
    return package


def _persist(package_dir: Path, package: dict[str, Any]) -> None:
    package_dir = Path(package_dir)
    package_dir.mkdir(parents=True, exist_ok=True)
    path = package_dir / f"{package['package_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def load_scene_package(package_dir: Path, package_id: str) -> Optional[dict[str, Any]]:
    path = Path(package_dir) / f"{package_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCENE_PACKAGE_SCHEMA else None


def verify_package_materials(package: dict[str, Any]) -> list[bool]:
    """逐材料复核 sha256（读取侧完整性自证）。"""
    out = []
    for m in package.get("materials", []):
        out.append(isinstance(m, dict) and m.get("sha256") == _sha256(m.get("text", "")))
    return out


def render_scene_block(package: dict[str, Any], *, budget: int = SCENE_BLOCK_TEXT_BUDGET) -> str:
    """现场包 → prompt 注入块（材料文本受预算约束，超出截断注明）。"""
    scene = package.get("scene", {})
    identity = scene.get("identity", {})
    header = (f"[现场包 {package.get('package_id', '?')} | 场景={scene.get('scenario', '?')}"
              f" 覆盖={scene.get('coverage', '?')}")
    if scene.get("generation"):
        header += f" 代次={scene['generation']}"
    lines = [header + "]"]
    title = identity.get("title", "")
    who = identity.get("process", "") or f"pid {identity.get('pid', '?')}"
    if title:
        lines.append(f"现场：{who} — {title}")
    used = 0
    truncated = False
    for m in package.get("materials", []):
        text = m.get("text", "")
        remain = budget - used
        if len(text) > remain:
            text = text[: max(remain, 0)]
            truncated = True
        used += len(text)
        head = f"材料[{m.get('kind')}]（{m.get('chars', 0)}字，出处 {m.get('tool')}）："
        lines.append(head + (f"\n{text}" if text else "（无文本）"))
        if used >= budget:
            truncated = True
            break
    if truncated:
        lines.append(f"（材料文本超出 {budget} 字预算，已截断——全文见现场包文件）")
    lines.append("[现场包结束]")
    return "\n".join(lines)
