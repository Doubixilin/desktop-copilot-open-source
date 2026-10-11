import unittest

import zones_geometry as geo
import zones_plan as zp
from tests.records import Rec


def item(name, kind, index=0):
    return Rec(name=name, kind=kind, index=index)


class ClassifyTest(unittest.TestCase):
    def plan_of(self, it, pinned=()):
        return {p.name: p for p in zp.plan_layout([it], pinned)}[it.name]

    def test_shortcut_goes_to_app_zone(self):
        self.assertEqual(self.plan_of(item("Kimi", "shortcut")).zone, "app")

    def test_url_goes_to_app_zone(self):
        self.assertEqual(self.plan_of(item("site", "url")).zone, "app")

    def test_unmatched_special_that_is_not_recycle_goes_to_app_zone(self):
        self.assertEqual(self.plan_of(item("SomeUwpApp", "special")).zone, "app")

    def test_file_goes_to_doc_zone(self):
        self.assertEqual(self.plan_of(item("a.docx", "file")).zone, "doc")

    def test_folder_goes_to_doc_zone(self):
        self.assertEqual(self.plan_of(item("proj", "folder")).zone, "doc")

    def test_hidden_system_is_untouched(self):
        self.assertEqual(self.plan_of(item("desktop.ini", "system")).zone, "untouched")

    def test_recycle_bin_english(self):
        p = self.plan_of(item("Recycle Bin", "special"))
        self.assertEqual(p.zone, "recycle")
        self.assertEqual(p.pos, geo.RECYCLE_POS)

    def test_recycle_bin_chinese(self):
        p = self.plan_of(item("回收站", "special"))
        self.assertEqual(p.zone, "recycle")
        self.assertEqual(p.pos, geo.RECYCLE_POS)


class AppSlotTest(unittest.TestCase):
    def test_pinned_take_front_slots_row_major(self):
        items = [item("A", "shortcut", 0), item("B", "shortcut", 1), item("C", "shortcut", 2)]
        plan = {p.name: p for p in zp.plan_layout(items, ["B", "A"])}
        self.assertEqual(plan["B"].slot, 0)
        self.assertEqual(plan["A"].slot, 1)
        self.assertEqual(plan["B"].pos, geo.app_slot(0, 0))
        self.assertEqual(plan["A"].pos, geo.app_slot(1, 0))
        self.assertEqual(plan["C"].slot, 2)
        self.assertEqual(plan["C"].pos, geo.app_slot(2, 0))

    def test_second_row_starts_at_slot_six(self):
        items = [item(f"s{i}", "shortcut", i) for i in range(7)]
        plan = {p.name: p for p in zp.plan_layout(items, [])}
        self.assertEqual(plan["s6"].pos, geo.app_slot(0, 1))

    def test_unpinned_fill_in_current_index_order(self):
        items = [item("late", "shortcut", 5), item("early", "shortcut", 1)]
        plan = {p.name: p for p in zp.plan_layout(items, [])}
        self.assertEqual(plan["early"].slot, 0)
        self.assertEqual(plan["late"].slot, 1)

    def test_missing_pinned_name_does_not_reserve_slot(self):
        items = [item("A", "shortcut", 0)]
        plan = {p.name: p for p in zp.plan_layout(items, ["ghost", "A"])}
        self.assertEqual(plan["A"].slot, 0)

    def test_duplicate_pinned_names_deduped(self):
        items = [item("A", "shortcut", 0), item("B", "shortcut", 1)]
        plan = {p.name: p for p in zp.plan_layout(items, ["A", "A", "B"])}
        self.assertEqual(plan["A"].slot, 0)
        self.assertEqual(plan["B"].slot, 1)

    def test_overflow_beyond_slots_is_unplaced(self):
        items = [item(f"s{i}", "shortcut", i) for i in range(geo.APP_SLOTS + 2)]
        plan = zp.plan_layout(items, [])
        placed = [p for p in plan if p.pos is not None and p.zone == "app"]
        unplaced = [p for p in plan if p.zone == "app" and p.pos is None]
        self.assertEqual(len(placed), geo.APP_SLOTS)
        self.assertEqual(len(unplaced), 2)

    def test_pinned_list_longer_than_slots_does_not_overflow_slots(self):
        pinned = [f"s{i}" for i in range(geo.APP_SLOTS + 3)]
        items = [item(n, "shortcut", i) for i, n in enumerate(pinned)]
        plan = {p.name: p for p in zp.plan_layout(items, pinned)}
        placed = [p for p in plan.values() if p.pos is not None]
        self.assertEqual(len(placed), geo.APP_SLOTS)
        self.assertEqual(plan["s0"].slot, 0)
        self.assertEqual(plan[f"s{geo.APP_SLOTS - 1}"].slot, geo.APP_SLOTS - 1)
        self.assertIsNone(plan[f"s{geo.APP_SLOTS}"].pos)

    def test_every_app_position_stays_left_of_avoid_line(self):
        items = [item(f"s{i}", "shortcut", i) for i in range(geo.APP_SLOTS)]
        for p in zp.plan_layout(items, []):
            self.assertLess(p.pos[0], geo.AVOID_X)

    def test_no_two_items_share_a_position(self):
        items = [item(f"s{i}", "shortcut", i) for i in range(geo.APP_SLOTS)]
        items.append(item("r.docx", "file", 99))
        items.append(item("Recycle Bin", "special", 100))
        positions = [p.pos for p in zp.plan_layout(items, []) if p.pos is not None]
        self.assertEqual(len(positions), len(set(positions)))

    def test_duplicate_display_names_get_distinct_slots(self):
        items = [item("same", "shortcut", 0), item("same", "shortcut", 1)]
        plan = zp.plan_layout(items, [])
        self.assertEqual([p.slot for p in plan], [0, 1])
        self.assertEqual(len({p.pos for p in plan}), 2)


class DocAndUntouchedTest(unittest.TestCase):
    def test_empty_desktop_yields_empty_plan(self):
        self.assertEqual(zp.plan_layout([], ["ghost"]), [])

    def test_cjk_pinned_name_takes_front_slot(self):
        items = [item("微信", "shortcut", 0), item("obsidian", "shortcut", 1)]
        plan = {p.name: p for p in zp.plan_layout(items, ["微信"])}
        self.assertEqual(plan["微信"].slot, 0)
        self.assertEqual(plan["微信"].pos, geo.app_slot(0, 0))

    def test_doc_items_are_placed_in_doc_zone(self):
        plan = {p.name: p for p in zp.plan_layout([item("a.docx", "file", 0)], [])}
        self.assertEqual(plan["a.docx"].zone, "doc")
        self.assertEqual(plan["a.docx"].pos, geo.doc_cell(0, 0))

    def test_untouched_items_are_unplaced(self):
        plan = {p.name: p for p in zp.plan_layout([item("desktop.ini", "system", 0)], [])}
        self.assertIsNone(plan["desktop.ini"].pos)


if __name__ == "__main__":
    unittest.main()
