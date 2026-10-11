"""桌面分区功能的实机验收电池：python scripts/accept_zones.py

覆盖票 11 中可无人值守执行的验收项。两项例外：桌面观感需人工看截图
（capture_desktop.ps1），重启后自启需人工重启验证。任一项失败退出码非 0。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import desktop_icons as di  # noqa: E402
import desktop_layout as dl  # noqa: E402
import usage_log as ul  # noqa: E402
import usage_score  # noqa: E402
import zones_geometry as geo  # noqa: E402
import zones_orchestrate as zo  # noqa: E402
import zones_plan as zp  # noqa: E402

FAILURES = []


def run_apply():
    return subprocess.run([sys.executable, str(ROOT / "zones_orchestrate.py"), "--apply"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))


def last_line(result):
    lines = (result.stdout or "").strip().splitlines()
    return lines[-1] if lines else ""


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def positions():
    return {i.name: (i.x, i.y) for i in di.list_items()}


def wait_for(predicate, timeout=12.0, step=1.5):
    """explorer 落位带动画、视图刷新有延迟：所有读回断言都必须轮询等稳定。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    # 1 出厂态快照存在且从未被覆盖（创建时间 == 修改时间）
    factory = dl.data_dir() / dl.FACTORY_NAME
    if factory.exists():
        st = factory.stat()
        check("出厂态快照存在且未被覆盖", abs(st.st_mtime - st.st_ctime) < 1.0,
              f"mtime-ctime={st.st_mtime - st.st_ctime:.2f}s")
    else:
        check("出厂态快照存在且未被覆盖", False, "文件不存在")

    # 0 自归一化：验收假设桌面处于当前几何的计划态
    norm = run_apply()
    check("开头自归一化 apply 退出码 0", norm.returncode == 0, last_line(norm))
    time.sleep(3)

    # 2 dry-run 只读且退出码 0
    dry = subprocess.run([sys.executable, str(ROOT / "zones_orchestrate.py")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    check("dry-run 退出码 0 且打印计划", dry.returncode == 0 and "dry-run" in dry.stdout)

    # 3 几何约束：避让线、应用区两行、回收站落点
    pos = positions()
    over = [n for n, (x, _y) in pos.items() if x >= geo.AVOID_X]
    check("无图标越过避让线", not over, str(over))

    recycle = next((n for n in pos if n in zp.RECYCLE_NAMES), None)
    check("回收站在左下角", recycle is not None and pos[recycle] == geo.RECYCLE_POS, str(pos.get(recycle)))
    app_rows = {y for _n, (x, y) in pos.items() if x < 900 and y < geo.LABEL_Y}
    check("应用区只占两个晶格行", app_rows <= {geo.APP_ORIGIN[1], geo.APP_ORIGIN[1] + geo.ROW_STEP},
          str(sorted(app_rows)))

    # 4 隐私复核：所有使用日志记录的字段集合恰为 {ts, exe}
    bad = []
    for path in ul.log_dir().glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if set(json.loads(line)) != {"ts", "exe"}:
                bad.append(path.name)
    check("使用日志仅含 ts 与 exe", not bad, str(bad[:3]))

    # 5 数据服务契约
    for endpoint in ("/performance", "/deck"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:5000{endpoint}", timeout=5) as r:
                ok = r.status == 200 and len(r.read()) > 0
        except OSError:
            ok = False
        check(f"{endpoint} 契约可用", ok)

    # 6 还原链：出厂态 -> 最近一次运行前态
    def settled_to(snapshot_items):
        def ok():
            after = positions()
            return not [e for e in snapshot_items if after.get(e["name"]) != (e["x"], e["y"])]
        return wait_for(ok)

    snapshot = dl.load_snapshot("factory")
    dl.restore("factory")
    check("restore factory 与出厂快照零偏差", settled_to(snapshot["items"]))
    last = dl.load_snapshot("last")
    dl.restore("last")
    check("restore last 与运行前快照零偏差", settled_to(last["items"]))

    # 7 看门狗增量：新建文档自动归位、删除后收紧
    probe = di.desktop_dirs()[0] / "zz-accept.docx"
    probe.write_text("x", encoding="utf-8")
    placed_ok = wait_for(lambda: positions().get("zz-accept.docx") == (geo.DOC_ORIGIN[0], geo.DOC_ORIGIN[1]), timeout=30)
    check("新建文档被自动归位", placed_ok, str(positions().get("zz-accept.docx")))
    probe.unlink()

    def tightened():
        ys = sorted(i.y for i in di.list_items() if i.kind == "file")
        return ys == [geo.DOC_ORIGIN[1] + k * geo.ROW_STEP for k in range(len(ys))] and "zz-accept.docx" not in positions()

    check("删除后文档区收紧无空洞", wait_for(tightened, timeout=30))

    # 8 用户拖走的应用图标不被看门狗拖回（漂移纠正语义）
    app_name = next(n for n, (x, y) in positions().items()
                    if y in (geo.APP_ORIGIN[1], geo.APP_ORIGIN[1] + geo.ROW_STEP) and x < 900)
    with di.IconView() as view:
        idx = {view.text(i): i for i in range(view.count())}
        view.set_position(idx[app_name], 1500, 800)
    dragged = wait_for(lambda: positions().get(app_name) is not None)
    time.sleep(6)
    check("手动拖走的应用图标不被拖回", positions().get(app_name, (0, 0))[0] >= 1500, str(positions().get(app_name)))

    # 9 收尾：把桌面带回当前几何的计划态（restore 会停在旧快照的几何）
    apply = run_apply()
    check("收尾 apply 退出码 0", apply.returncode == 0, last_line(apply))

    # 9b 收尾全计划复验：独立兜底，专抓成对错位（permutation）类回归。
    # 几何检查（避让线 / 行列集合 / 回收站落点）抓不到两个图标在应用区内互换
    # 栏位——互换后双方仍各占合法晶格位、行/列集合不变。重启前 6 图标两两互换
    # 即属此类，旧电池放行。这里读实时坐标、算一份新鲜计划、逐项按名比对。
    def full_plan_mismatches():
        try:
            live = di.list_items()
        except di.DesktopViewUnavailable:
            return ["<view unavailable>"]
        plan = zp.plan_layout(live, zo.load_pinned(), usage_score.ranking(live))
        mismatches, _ambiguous = zo.verify_positions(live, plan)
        return [f"{n} planned={p} actual={a}" for n, p, a in mismatches]

    wait_for(lambda: not full_plan_mismatches(), timeout=15)  # 等 apply 动画收敛
    final = full_plan_mismatches()
    check("收尾全计划复验零偏差", not final, str(final[:4]))

    # 10 单测套件
    suite = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    check("单元测试全绿", suite.returncode == 0, suite.stderr.strip().splitlines()[-1] if suite.stderr else "")

    print()
    if FAILURES:
        print(f"验收失败 {len(FAILURES)} 项：{FAILURES}")
        return 1
    print("验收电池全部通过（桌面观感与重启自启两项需人工）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
