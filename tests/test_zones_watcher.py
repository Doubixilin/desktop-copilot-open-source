import contextlib
import pathlib
import unittest
from types import SimpleNamespace

import desktop_icons as di
import zones_watcher as zw
from tests.records import Rec, doc, shortcut


class Plan_Stub:
    def __init__(self, name, zone, pos):
        self.name = name
        self.zone = zone
        self.pos = pos
        self.slot = None
        self.source = None


class ChangedTest(unittest.TestCase):
    def test_added_name_is_a_change(self):
        self.assertTrue(zw.changed({"a"}, {"a", "b"}))

    def test_removed_name_is_a_change(self):
        self.assertTrue(zw.changed({"a", "b"}, {"a"}))

    def test_content_only_is_not_a_change(self):
        self.assertFalse(zw.changed({"a", "b"}, {"b", "a"}))


class SettleTest(unittest.TestCase):
    def test_storm_collapses_to_one_final_set(self):
        sequence = iter([{"a"}, {"a", "b"}, {"a", "b", "c"}, {"a", "b", "c"}])
        calls = {"n": 0}

        def scan():
            calls["n"] += 1
            return next(sequence)

        final = zw.settle(scan, interval=0, max_rounds=5)
        self.assertEqual(final, {"a", "b", "c"})

    def test_settle_gives_up_after_max_rounds(self):
        growing = [{"a", str(i)} for i in range(100)]
        sequence = iter(growing)

        def scan():
            return next(sequence)

        final = zw.settle(scan, interval=0, max_rounds=3)
        self.assertEqual(len(final), 2)


class IncrementalMovesTest(unittest.TestCase):
    def moves_for(self, items, plans, prev):
        return zw.incremental_moves(items, plans, prev)

    def test_new_doc_is_placed(self):
        items = [doc("new.docx", 0)]
        plans = [Plan_Stub("new.docx", "doc", (13, 394))]
        moves = self.moves_for(items, plans, set())
        self.assertEqual(moves, [("new.docx", 13, 394)])

    def test_existing_doc_off_plan_is_cascaded(self):
        items = [doc("old.docx", 0, x=999, y=999)]
        plans = [Plan_Stub("old.docx", "doc", (13, 394))]
        moves = self.moves_for(items, plans, {"old.docx"})
        self.assertEqual(moves, [("old.docx", 13, 394)])

    def test_existing_app_off_plan_is_left_alone(self):
        items = [shortcut("Kimi", 0)]
        items[0] = Rec(name="Kimi", kind="shortcut", index=0, x=999, y=999)
        plans = [Plan_Stub("Kimi", "app", (13, 100))]
        moves = self.moves_for(items, plans, {"Kimi"})
        self.assertEqual(moves, [])

    def test_new_app_off_plan_is_placed(self):
        items = [Rec(name="Kimi", kind="shortcut", index=0, x=999, y=999)]
        plans = [Plan_Stub("Kimi", "app", (13, 100))]
        moves = self.moves_for(items, plans, set())
        self.assertEqual(moves, [("Kimi", 13, 100)])

    def test_same_round_add_and_remove_suspends_app_placement(self):
        # 重命名在 fs 差分里表现为"一增一删"；新名字不该被当全新项拖进栏位
        items = [Rec(name="Kimi2", kind="shortcut", index=0, x=999, y=999)]
        plans = [Plan_Stub("Kimi2", "app", (13, 100))]
        moves = zw.incremental_moves(items, plans, {"Kimi"}, removed_names={"Kimi"})
        self.assertEqual(moves, [])

    def test_app_icon_covering_label_band_is_moved(self):
        # 标签空行被用户拖来的图标压住时，可读性优先于"不碰用户摆位"
        import zones_geometry as geo

        band_y = geo.DOC_ORIGIN[1] - geo.ROW_STEP
        items = [Rec(name="Kimi", kind="shortcut", index=0, x=13, y=band_y)]
        plans = [Plan_Stub("Kimi", "app", (13, geo.APP_ORIGIN[1]))]
        moves = zw.incremental_moves(items, plans, {"Kimi"})
        self.assertEqual(moves, [("Kimi", 13, geo.APP_ORIGIN[1])])

    def test_recycle_is_never_moved_by_watcher(self):
        items = [Rec(name="Recycle Bin", kind="special", index=0, x=999, y=999)]
        plans = [Plan_Stub("Recycle Bin", "recycle", (13, 1276))]
        self.assertEqual(self.moves_for(items, plans, set()), [])

    def test_items_already_on_plan_are_skipped(self):
        items = [doc("a.docx", 0, x=13, y=394)]
        plans = [Plan_Stub("a.docx", "doc", (13, 394))]
        self.assertEqual(self.moves_for(items, plans, {"a.docx"}), [])

    def test_unplaced_entries_produce_no_moves(self):
        items = [doc("a.docx", 0, x=999, y=999)]
        plans = [Plan_Stub("a.docx", "doc", None)]
        self.assertEqual(self.moves_for(items, plans, set()), [])


class ArrangeLockTest(unittest.TestCase):
    """锁测试必须用临时目录：生产锁路径会被真实看门狗合法持有。"""

    def setUp(self):
        import tempfile

        import zones_lock

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._real = zones_lock.lock_path
        zones_lock.lock_path = lambda: pathlib.Path(self.tmp.name) / "arrange.lock"
        self.addCleanup(setattr, zones_lock, "lock_path", self._real)

    def test_second_holder_is_refused(self):
        import zones_lock

        with zones_lock.ArrangeLock(wait_seconds=0):
            with self.assertRaises(zones_lock.ArrangeBusy):
                with zones_lock.ArrangeLock(wait_seconds=0):
                    pass

    def test_stale_lock_from_dead_pid_is_broken(self):
        import zones_lock

        lock = zones_lock.ArrangeLock(wait_seconds=0)
        lock.path.mkdir()
        lock.holder.write_text("999999999 0", encoding="utf-8")  # 不存在的 pid +  epoch 时间戳
        with zones_lock.ArrangeLock(wait_seconds=0):
            pass  # 陈旧锁应被打破而不是报忙

    def test_lock_is_released(self):
        import zones_lock

        with zones_lock.ArrangeLock(wait_seconds=0):
            pass
        with zones_lock.ArrangeLock(wait_seconds=0):
            pass


class DisplayNamesTest(unittest.TestCase):
    """fs 名 → 显示名投影（审计 B01 的身份换算收口）。"""

    def test_shortcuts_and_urls_project_to_stem(self):
        self.assertEqual(di.display_names({"Kimi.lnk", "site.url", "notes.docx"}),
                         {"Kimi", "site", "notes.docx"})

    def test_plain_names_pass_through(self):
        self.assertEqual(di.display_names({"Kimi", "report.pdf"}), {"Kimi", "report.pdf"})

    def test_mixed_case_suffix(self):
        self.assertEqual(di.display_names({"App.LNK"}), {"App"})


class _BusyLock:
    """永远忙的锁桩：手动编排持锁期间看门狗撞锁的形状。"""

    def __enter__(self):
        import zones_lock

        raise zones_lock.ArrangeBusy("busy")

    def __exit__(self, *exc):
        return False


def _view(name, x=0, y=0, path="C:/desktop/backing"):
    """视图条目桩：path=None 表示无文件系统背书（回收站等特殊项/幽灵项）。"""
    return SimpleNamespace(name=name, x=x, y=y, path=path)


class ArrangeOnceTest(unittest.TestCase):
    """arrange_once 的身份投影与锁忙语义（审计 B01/B02/N01）。

    全依赖注入：scan/list_items/lock/plan/apply_moves 都是桩，不碰真实
    桌面、真实锁目录与文件系统。
    """

    def _run(self, previous, fs_names, items, plans, lock=None,
             list_items_sequence=None):
        calls = {"apply": [], "views": 0}

        def scan():
            return set(fs_names)

        def list_items():
            calls["views"] += 1
            if list_items_sequence:
                return list_items_sequence[min(calls["views"] - 1,
                                               len(list_items_sequence) - 1)]
            return items

        def apply_moves(_items, moves):
            calls["apply"].append(list(moves))

        # 复核修正：非锁忙用例也注入 nullcontext，绝不触碰真实编排锁目录
        out = zw.arrange_once(set(previous), scan=scan, list_items=list_items,
                              lock=lock or contextlib.nullcontext(),
                              plan=lambda _items: plans,
                              apply_moves=apply_moves, settle_interval=0)
        return out, calls

    def test_fs_name_baseline_protects_existing_app(self):
        # B01 复刻生产身份：基线/监控是文件名 Kimi.lnk，视图条目是显示名 Kimi。
        # 修复前 protection 判断 "Kimi" in {"Kimi.lnk"} 为假，已有应用被重新归位。
        items = [_view("Kimi", x=999, y=999)]
        plans = [Plan_Stub("Kimi", "app", (13, 198))]
        out, calls = self._run({"Kimi.lnk"}, {"Kimi.lnk"}, items, plans)
        self.assertEqual(out, ({"Kimi.lnk"}, 0, True))
        self.assertEqual(calls["apply"], [])
        self.assertEqual(calls["views"], 1)  # 首轮追平，无空转（N01）

    def test_new_app_with_fs_baseline_is_placed(self):
        items = [_view("Kimi", x=999, y=999)]
        plans = [Plan_Stub("Kimi", "app", (13, 198))]
        out, calls = self._run(set(), {"Kimi.lnk"}, items, plans)
        self.assertEqual(out, ({"Kimi.lnk"}, 1, True))
        self.assertEqual(calls["apply"], [[("Kimi", 13, 198)]])

    def test_rename_round_suspends_app_placement(self):
        # 一增一删疑似重命名：Kimi2 不是已有项，也不能当全新项拖进栏位
        items = [_view("Kimi2", x=999, y=999)]
        plans = [Plan_Stub("Kimi2", "app", (13, 198))]
        out, calls = self._run({"Kimi.lnk"}, {"Kimi2.lnk"}, items, plans)
        self.assertEqual(out[1], 0)
        self.assertEqual(calls["apply"], [])

    def test_lock_busy_keeps_baseline_for_retry(self):
        # B02：锁忙返回新集合并 caught_up=True 会把事件吞进基线；
        # 修复后保留旧基线、标记未追平，下一轮扫描仍能看到变化。
        items = [_view("Kimi", 13, 100), _view("new.docx", x=999, y=999)]
        plans = [Plan_Stub("Kimi", "app", (13, 100)),
                 Plan_Stub("new.docx", "doc", (13, 394))]
        out, calls = self._run({"Kimi.lnk"}, {"Kimi.lnk", "new.docx"},
                               items, plans, lock=_BusyLock())
        self.assertEqual(out, ({"Kimi.lnk"}, 0, False))
        self.assertEqual(calls["views"], 0)  # 没拿到锁就不碰视图

    def test_view_waits_until_new_icon_appears(self):
        # 新文件先进 fs、后进视图：追平循环要等视图出现才继续
        first = [_view("Kimi", 13, 100)]
        caught = [_view("Kimi", 13, 100), _view("notes.docx", x=999, y=999)]
        plans = [Plan_Stub("Kimi", "app", (13, 100)),
                 Plan_Stub("notes.docx", "doc", (13, 394))]
        out, calls = self._run({"Kimi.lnk"}, {"Kimi.lnk", "notes.docx"},
                               caught, plans, list_items_sequence=[first, caught])
        self.assertEqual(calls["views"], 2)
        self.assertEqual(out, ({"Kimi.lnk", "notes.docx"}, 1, True))
        self.assertEqual(calls["apply"], [[("notes.docx", 13, 394)]])

    def test_special_items_do_not_block_catchup(self):
        # N01：回收站等特殊项永不进 fs 名集合；按全体视图名判等会永远等不满。
        items = [_view("Kimi", 13, 100), _view("notes.docx", 13, 394),
                 _view("Recycle Bin", x=500, y=900, path=None)]
        plans = [Plan_Stub("Kimi", "app", (13, 100)),
                 Plan_Stub("notes.docx", "doc", (13, 394)),
                 Plan_Stub("Recycle Bin", "recycle", (13, 1276))]
        out, calls = self._run({"Kimi.lnk", "notes.docx"},
                               {"Kimi.lnk", "notes.docx"}, items, plans)
        self.assertEqual(out[2], True)
        self.assertEqual(calls["views"], 1)   # 首轮追平：消除 15 轮空转
        self.assertEqual(calls["apply"], [])  # 全部就位或豁免

    def test_late_arrival_stays_pending_then_places_on_retry(self):
        # F05：视图迟到超过首轮 15 次等待——新文件不得提前进基线，
        # 否则第二轮它已是"已有应用"而永不落位
        empty, arrived = [], [_view("NewApp", x=999, y=999)]
        plans = [Plan_Stub("NewApp", "app", (13, 198))]
        first, calls1 = self._run(set(), {"NewApp.lnk"}, empty, plans)
        self.assertEqual(first[0], set())            # 未出现在视图 → 基线外待处理
        self.assertEqual(first[1:], (0, False))
        self.assertEqual(calls1["views"], zw.VIEW_CATCHUP_ROUNDS)
        second, calls2 = self._run(first[0], {"NewApp.lnk"}, arrived, plans)
        self.assertEqual(second[0], {"NewApp.lnk"})  # 出现并落位后才确认
        self.assertEqual(second[1], 1)
        self.assertEqual(calls2["apply"], [[("NewApp", 13, 198)]])

    def test_consecutive_missing_rounds_keep_pending(self):
        # 连续两轮视图不到达：基线始终不含新文件，事件不丢
        baseline = {"Old.lnk"}
        items = [_view("Old", 13, 100)]
        plans = [Plan_Stub("Old", "app", (13, 100)),
                 Plan_Stub("NewApp", "app", (13, 198))]
        out1, _ = self._run(baseline, {"Old.lnk", "NewApp.lnk"}, items, plans)
        self.assertEqual(out1[0], {"Old.lnk"})
        out2, _ = self._run(out1[0], {"Old.lnk", "NewApp.lnk"}, items, plans)
        self.assertEqual(out2[0], {"Old.lnk"})

    def test_partial_arrival_confirms_only_visible(self):
        # 部分到达：可见者确认并落位，不可见者保持待处理
        items = [_view("Seen", x=999, y=999)]
        plans = [Plan_Stub("Seen", "doc", (13, 394))]
        out, calls = self._run({"Seen.docx"}, {"Seen.docx", "Ghost.docx"}, items, plans)
        # 视图缺 Ghost → 未追平；删除（无）+ 已有 Seen 保持，Ghost 待处理
        self.assertEqual(out[0], {"Seen.docx"})
        self.assertEqual(out[2], False)


class WatchOnceTest(unittest.TestCase):
    """_watch_once：异常保留基线（复核 F05 的同类丢事件风险）。"""

    def test_unchanged_keeps_baseline_without_arranging(self):
        arrange_calls = []
        baseline = {"a.txt"}
        out = zw._watch_once(baseline, lambda: {"a.txt"},
                             lambda prev: arrange_calls.append(prev) or (prev, 0, True),
                             settle_fn=lambda: {"a.txt"})
        self.assertEqual(out, baseline)
        self.assertEqual(arrange_calls, [])

    def test_view_unavailable_keeps_baseline_for_retry(self):
        def boom(previous):
            raise di.DesktopViewUnavailable("synthetic view offline")
        out = zw._watch_once({"a.txt"}, lambda: {"a.txt", "b.txt"}, boom,
                             settle_fn=lambda: {"a.txt", "b.txt"})
        self.assertEqual(out, {"a.txt"})   # 不刷新成当前扫描，事件留给下一轮

    def test_generic_failure_keeps_baseline(self):
        def boom(previous):
            raise RuntimeError("synthetic planner crash")
        out = zw._watch_once({"a.txt"}, lambda: {"a.txt", "b.txt"}, boom,
                             settle_fn=lambda: {"a.txt", "b.txt"})
        self.assertEqual(out, {"a.txt"})


if __name__ == "__main__":
    unittest.main()
