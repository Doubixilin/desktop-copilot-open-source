"""首次全量编排的命令行入口：dry-run 打印计划，显式 --apply 才落位。

落位前自动写运行前快照（出厂态快照若不存在则先生成）；落位后读回坐标
与计划比对，任何不一致都显式上报——网格吸附或晶格不同余会让实际落点
偏离计划，静默通过等于把"排了一半"的桌面留给用户。
"""
import json
import sys
from pathlib import Path

import desktop_icons as di
import desktop_layout as dl
import zones_lock
import usage_score
import zones_plan as zp

PINNED_FILE = Path(__file__).resolve().parent / "pinned.json"


def load_pinned():
    if not PINNED_FILE.exists():
        return []
    return json.loads(PINNED_FILE.read_text(encoding="utf-8"))


def moves_from_plan(items, plan):
    """从计划算出真正需要写的落位动作：(index, 显示名, x, y)。"""
    moves = []
    for item, placement in zip(items, plan):
        if placement.pos is None:
            continue
        if (item.x, item.y) == placement.pos:
            continue
        moves.append((item.name, placement.pos[0], placement.pos[1]))
    return moves


def verify_positions(current, plan):
    """读回比对：返回 (mismatches, ambiguous)。

    mismatches = [(显示名, 计划坐标, 实际坐标)]；同名多项进 ambiguous，跳过且不猜。
    """
    counts = di.name_counts(current)
    actual = {i.name: (i.x, i.y) for i in current}
    mismatches = []
    ambiguous = []
    for placement in plan:
        if placement.pos is None:
            continue
        if counts.get(placement.name, 0) != 1:
            ambiguous.append(placement.name)
            continue
        if actual.get(placement.name) != placement.pos:
            mismatches.append((placement.name, placement.pos, actual.get(placement.name)))
    return mismatches, sorted(set(ambiguous))


def plan_report(items, plan):
    lines = [di.pad("name", 34) + di.pad("zone", 9) + di.pad("source", 12) + "current      -> target"]
    for item, placement in zip(items, plan):
        target = f"({placement.pos[0]:>4},{placement.pos[1]:>4})" if placement.pos else "   -"
        lines.append(
            di.pad(item.name, 34)
            + di.pad(placement.zone, 9)
            + di.pad(placement.source or "-", 12)
            + f"({item.x:>4},{item.y:>4}) -> {target}"
        )
    return "\n".join(lines)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    do_apply = "--apply" in argv
    try:
        items = di.list_items()
    except di.DesktopViewUnavailable as exc:
        print(f"zones-orchestrate: {exc}", file=sys.stderr)
        return 2
    plan = zp.plan_layout(items, load_pinned(), usage_score.ranking(items))

    if not do_apply:
        print(f"dry-run: {len(items)} 项，其中 {len(moves_from_plan(items, plan))} 项将移动；加 --apply 才落位")
        print(plan_report(items, plan))
        return 0

    try:
        with zones_lock.ArrangeLock(wait_seconds=20):
            # 读、计划、快照、写全在锁内：否则看门狗可能在读与写之间完成一轮
            # 编排，运行前快照会记下一个桌面上从未存在过的状态
            items = di.list_items()
            plan = zp.plan_layout(items, load_pinned(), usage_score.ranking(items))
            moves = moves_from_plan(items, plan)
            dl.ensure_factory(items)
            dl.take_snapshot(items, "run")
            with di.IconView() as view:
                view.write_named_moves(moves)
    except zones_lock.ArrangeBusy as exc:
        print(f"zones-orchestrate: {exc}，稍后重试", file=sys.stderr)
        return 2
    except di.DesktopViewUnavailable as exc:
        print(f"zones-orchestrate: 打开图标视图失败，未写入：{exc}", file=sys.stderr)
        return 2
    except di.WriteAborted as exc:
        print(
            f"zones-orchestrate: 落位中止于第 {exc.done + 1}/{len(moves)} 项（{exc.name}）：{exc}",
            file=sys.stderr,
        )
        print("zones-orchestrate: 用 desktop_layout.py restore 回退", file=sys.stderr)
        return 2
    try:
        current = di.list_items()
    except di.DesktopViewUnavailable as exc:
        print(f"zones-orchestrate: 已落位但读回失败，请手动核对：{exc}", file=sys.stderr)
        return 2
    mismatches, ambiguous = verify_positions(current, plan)
    print(f"applied: {len(moves)} 项已落位")
    for name in ambiguous:
        print(f"  ambiguous (same-named icons, not verified): {name}")
    if mismatches:
        for name, planned, actual in mismatches:
            print(f"  mismatch: {name} planned={planned} actual={actual}")
        print("zones-orchestrate: 实际落点与计划不一致（网格吸附或晶格不同余）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
