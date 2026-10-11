# -*- coding: utf-8 -*-
"""L2 采集 worker：独立短命子进程（M4）。

用法：python -B -u scene_capture_worker.py <hwnd>
输出：一行 JSON（材料 dict 或 null）到 stdout，退出码恒 0（失败即 null）。

为什么独立进程：宿主适配器（scene_adapter）常驻线程环境（comtypes COM +
WinEventHook 泵 + GDI）中，rapidocr/onnxruntime 的 DLL 装载链实测会被拖到
分钟级（loader-lock 类相互作用，根因未定案）；裸进程导入仅 ~0.5s。进程隔离
同时兜住 OCR 引擎级卡死（超时可整树击杀），代价约 +0.6s 启动——可接受。
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import scene_capture  # noqa: E402


def _emit(material):
    print(json.dumps(material, ensure_ascii=False) if material is not None else "null",
          flush=True)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        # 常驻会话：每行一个 {"hwnd":N} 请求，回一行材料/null（导入成本只付一次）
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                hwnd = int(json.loads(raw).get("hwnd", 0))
            except (ValueError, json.JSONDecodeError):
                hwnd = 0
            try:
                _emit(scene_capture.capture_material_inline(hwnd) if hwnd else None)
            except Exception:
                _emit(None)
        return 0
    try:
        hwnd = int(sys.argv[1])
    except (IndexError, ValueError):
        print("null")
        return 0
    try:
        material = scene_capture.capture_material_inline(hwnd)
    except Exception:
        material = None
    _emit(material)
    return 0


if __name__ == "__main__":
    sys.exit(main())
