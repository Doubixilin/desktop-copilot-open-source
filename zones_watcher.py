"""桌面目录看门狗：新增/删除才触发增量编排，自动落位。

漂移纠正的精确语义（2026-09-24 钉死）：
- 应用区只动**新出现的项**；已存在的快捷方式无论被用户拖到哪、推荐分数怎么
  变，看门狗都不碰——分数重排留给手动全量编排（05）。
- 文档区豁免保护：每次触发都按最新计划级联落位，新文档归组、删除后收紧。
- 回收站不由看门狗移动。
触发只看两个桌面目录的**文件名集合**：内容修改、重命名之外的元数据变化都
不算事件。检测到变化后先 settle（等集合稳定）把事件风暴合并成一次编排。
身份分两套空间（审计 B01）：监控/基线集合用文件系统名（Kimi.lnk），与
视图条目和保护判断比较前经 desktop_icons.display_names 投影成显示名（Kimi）。
"""
import logging
import os
import sys
import time
from pathlib import Path

import desktop_icons as di
import desktop_layout as dl
import usage_score
import zones_lock
import zones_orchestrate as zo
import zones_plan as zp

SETTLE_INTERVAL = 1.0
SETTLE_MAX_ROUNDS = 5
VIEW_CATCHUP_ROUNDS = 15  # 新增图标进 ListView 的延迟实测可达十秒级

log = logging.getLogger("zones-watcher")


def _configure_logging():
    if log.handlers:
        return
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "qoder-deck"
    try:
        root.mkdir(parents=True, exist_ok=True)
        log.setLevel(logging.INFO)
        file_handler = logging.FileHandler(root / "watcher.log", encoding="utf-8")
        # 带时间戳：fs 计数抖动排查需要把事件与电池/手动操作按时间对齐
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(file_handler)
    except OSError:
        pass  # 日志目录建不成就只走 stdout，绝不能拖死数据服务
    log.addHandler(logging.StreamHandler(sys.stdout))


_configure_logging()


def scan_names():
    """两个桌面目录下的可见文件名集合。

    隐藏/系统项（desktop.ini）必须排除：ListView 不显示它们，若计入则
    "视图追平"永远等不满，删除事件那一轮会拿含旧项的视图算计划而零动作。
    """
    names = set()
    for directory in di.desktop_dirs():
        for entry in directory.iterdir():
            attrs = di.file_attributes(entry)
            if attrs & (di.ATTR_HIDDEN | di.ATTR_SYSTEM):
                continue
            names.add(entry.name)
    return names


def changed(previous, current):
    return previous != current


def settle(scan, interval=SETTLE_INTERVAL, max_rounds=SETTLE_MAX_ROUNDS):
    """等文件名集合稳定一轮，把拷入风暴合并成一次编排。"""
    names = scan()
    for _ in range(max_rounds):
        time.sleep(interval)
        nxt = scan()
        if nxt == names:
            return names
        names = nxt
    return names


def incremental_moves(items, plan, previous_names, removed_names=()):
    """按漂移纠正语义从完整计划里挑出本次真正要写的落位动作。

    previous_names/removed_names 必须是**显示名**集合（.lnk/.url 为 stem），
    与 item.name 同一身份空间；文件系统名基线由调用方经 di.display_names
    投影后传入（审计 B01：身份混用会让已有应用保护失效）。
    """
    moves = []
    for item, placement in zip(items, plan):
        if placement.pos is None:
            continue
        if (item.x, item.y) == placement.pos:
            continue
        if placement.zone == "app" and item.name in previous_names:
            # 应用区只动新出现的项；但压在分区标签空行上的图标例外——
            # 标签可读性优先于"不碰用户摆位"（spec story 22）
            if not _in_label_band(item):
                continue
        if placement.zone == "app" and removed_names and item.name not in previous_names:
            continue  # 同轮既有消失又有新增，可能是重命名；不动，留给下次全量编排
        if placement.zone == "recycle":
            continue  # 回收站不由看门狗移动
        moves.append((item.name, placement.pos[0], placement.pos[1]))
    return moves


def _in_label_band(item):
    """图标是否压在分区标签独占的那一行空行里。"""
    import zones_geometry as geo

    top = geo.DOC_ORIGIN[1] - geo.ROW_STEP
    return top <= item.y < geo.DOC_ORIGIN[1] and item.x < geo.AVOID_X


def arrange_once(previous_names, *, scan=scan_names, list_items=di.list_items,
                 lock=None, plan=None, apply_moves=None,
                 settle_interval=SETTLE_INTERVAL):
    """一次增量编排：算计划、挑动作、快照、落位。

    previous_names 与返回的基线是**文件系统名**（事件检测语义不变）；与
    视图条目、已有应用保护比较前统一经 di.display_names 投影成显示名
    （审计 B01）。锁被手动编排占用时返回旧基线与 caught_up=False——
    忙也确认事件会把本轮变化吞进基线（审计 B02），保留给下一轮重试。
    scan/list_items/lock/plan/apply_moves 可注入，供单测隔离真实桌面。
    返回 (当前文件名集合, 落位数, 视图是否追平)。
    """

    def default_plan(items):
        return zp.plan_layout(items, zo.load_pinned(), usage_score.ranking(items))

    def default_apply(items, moves):
        dl.ensure_factory(items)
        dl.take_snapshot(items, "run")
        with di.IconView() as view:
            view.write_named_moves(moves)

    plan = plan or default_plan
    apply_moves = apply_moves or default_apply
    current = scan()
    removed = previous_names - current
    previous_disp = di.display_names(previous_names)
    current_disp = di.display_names(current)
    baseline = set(previous_names)
    try:
        with (lock or zones_lock.ArrangeLock(wait_seconds=0)):
            # 文件系统事件先于 explorer 的 ListView 刷新；视图没跟上就规划，
            # 会把"已删除"仍算在内、计划等于现状而什么都不做。等视图追平；
            # 新增图标进视图的延迟实测可达十秒级，等不满就标记未追平。
            # 判等只看有文件系统背书的视图项：回收站等特殊项永不进文件名
            # 集合，按全体视图名判等会永远等不满（审计 N01 的空转根源）。
            caught_up = False
            view_disp = set()
            for _ in range(VIEW_CATCHUP_ROUNDS):
                items = list_items()
                view_disp = {i.name for i in items if getattr(i, "path", None) is not None}
                if view_disp == current_disp:
                    caught_up = True
                    break
                time.sleep(settle_interval)
            placements = plan(items)
            moves = incremental_moves(items, placements, previous_disp,
                                      di.display_names(removed))
            log.info("arrange: view=%d fs=%d moves=%d", len(items), len(current), len(moves))
            if moves:
                apply_moves(items, moves)
                log.info("增量编排落位 %d 项", len(moves))
            # 基线确认规则（复核 F05）：新增项只有出现在视图里并参与本轮计划
            # 才算已处理——视图迟到超过等待上限的新文件保持待处理（留在基线
            # 外），下一轮仍按"新增"处理直至落位。删除项即时确认（视图里的
            # 幽灵项没有文件系统背书，不影响追平判等）；未追平的轮次里连删除
            # 也暂不确认，避免与迟到新增组成误判的重命名对。
            if caught_up:
                baseline = set(previous_names) & current
            baseline |= {name for name in (current - previous_names)
                         if di.display_names({name}) & view_disp}
    except zones_lock.ArrangeBusy:
        log.info("编排锁被占用（手动编排进行中），保留基线待下轮重试")
        return previous_names, 0, False
    return baseline, len(moves), caught_up


def _watch_once(previous, scan, arrange, settle_fn=None):
    """看门狗单轮：返回更新后的基线。

    异常时**保留原基线**（复核 F05：刷新成当前扫描会把未处理的事件吞进
    基线）；下一轮仍能检测到差异并重试。settle_fn 可注入（测试免休眠）。
    """
    try:
        current = scan()
        if not changed(previous, current):
            return previous
        current = (settle_fn or (lambda: settle(scan)))()
        previous, moved, caught_up = arrange(previous)
        if not caught_up:
            # 视图没追平：本轮计划可能漏了新图标，立刻再排一轮
            log.info("视图未追平，追加一轮编排")
            previous, moved, _ = arrange(previous)
    except di.DesktopViewUnavailable as exc:
        log.warning("本次编排跳过（图标视图不可用）：%s", exc)
    except Exception as exc:
        log.exception("编排失败，跳过：%s", exc)
    return previous


def run_watch_loop(interval=2.0):
    previous = scan_names()
    while True:
        time.sleep(interval)
        previous = _watch_once(previous, scan_names, arrange_once)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    interval = float(argv[0]) if argv else 2.0
    print(f"zones-watcher: 监控桌面目录，每 {interval}s 扫描；Ctrl-C 退出")
    try:
        run_watch_loop(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
