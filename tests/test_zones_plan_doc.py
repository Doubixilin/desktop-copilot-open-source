import unittest

import zones_geometry as geo
import zones_plan as zp
from tests.records import Rec


def doc(name, index=0, mtime=0.0):
    return Rec(name=name, kind="file", index=index, mtime=mtime)


class DocGroupingTest(unittest.TestCase):
    def positions(self, items):
        return {p.name: p.pos for p in zp.plan_layout(items, [])}

    def test_single_group_one_column(self):
        pos = self.positions([doc(f"a{i}.docx", i, float(i)) for i in range(3)])
        xs = {x for x, _ in pos.values()}
        self.assertEqual(len(xs), 1)
        self.assertEqual(min(xs), geo.DOC_ORIGIN[0])

    def test_groups_are_separated_by_one_empty_column(self):
        items = [doc("a.docx", 0, 1.0), doc("b.pdf", 1, 1.0)]
        pos = self.positions(items)
        col_a = (pos["a.docx"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        col_b = (pos["b.pdf"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        self.assertEqual(col_b - col_a, 2)

    def test_group_order_is_fixed_not_by_count(self):
        items = [doc(f"z{i}.zip", i, 1.0) for i in range(5)] + [doc("one.pdf", 9, 1.0)]
        pos = self.positions(items)
        col_pdf = (pos["one.pdf"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        col_zip = (pos["z0.zip"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        self.assertLess(col_pdf, col_zip)

    def test_folders_group_comes_first(self):
        items = [doc("a.docx", 0, 1.0), Rec("proj", "folder", 1, 1.0)]
        pos = self.positions(items)
        self.assertEqual(pos["proj"][0], geo.DOC_ORIGIN[0])

    def test_unrecognized_extension_falls_to_other(self):
        items = [doc("weird.xyz", 0, 1.0), doc("a.docx", 1, 1.0)]
        pos = self.positions(items)
        col_doc = (pos["a.docx"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        col_other = (pos["weird.xyz"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        self.assertLess(col_doc, col_other)

    def test_newest_first_within_group(self):
        items = [doc("old.docx", 0, 100.0), doc("new.docx", 1, 900.0)]
        pos = self.positions(items)
        self.assertLess(pos["new.docx"][1], pos["old.docx"][1])

    def test_mtime_tie_breaks_by_name_for_determinism(self):
        items = [doc("b.docx", 0, 5.0), doc("a.docx", 1, 5.0)]
        pos = self.positions(items)
        self.assertLess(pos["a.docx"][1], pos["b.docx"][1])

    def test_exactly_eight_items_stay_in_one_column(self):
        pos = self.positions([doc(f"a{i}.docx", i, float(i)) for i in range(8)])
        xs = {x for x, _ in pos.values()}
        self.assertEqual(len(xs), 1)

    def test_ninth_item_folds_to_adjacent_column(self):
        pos = self.positions([doc(f"a{i}.docx", i, 1.0) for i in range(9)])
        xs = sorted({x for x, _ in pos.values()})
        self.assertEqual(len(xs), 2)
        self.assertEqual(xs[1] - xs[0], geo.COL_STEP)
        folded = [n for n, (x, _) in pos.items() if x == xs[1]]
        self.assertEqual(folded, ["a8.docx"])

    def test_folded_group_keeps_contiguous_columns(self):
        items = [doc(f"a{i}.docx", i, float(i)) for i in range(9)] + [doc("b.pdf", 50, 1.0)]
        pos = self.positions(items)
        col_last_doc = max((x - geo.DOC_ORIGIN[0]) // geo.COL_STEP for x, _ in pos.values() if x != pos["b.pdf"][0])
        col_pdf = (pos["b.pdf"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        self.assertEqual(col_pdf - col_last_doc, 2)

    def test_no_docs_at_all(self):
        items = [Rec("Kimi", "shortcut", 0), Rec("Recycle Bin", "special", 1)]
        plan = zp.plan_layout(items, [])
        self.assertEqual([p for p in plan if p.zone == "doc"], [])

    def test_empty_groups_do_not_consume_columns(self):
        # folders 组为空：office 占第 0 列，pdf 必须紧跟在第 2 列（0 + 1 用列 + 1 空列）
        items = [doc(f"a{i}.docx", i, 1.0) for i in range(3)] + [doc("b.pdf", 9, 1.0)]
        pos = self.positions(items)
        col_office = (pos["a0.docx"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        col_pdf = (pos["b.pdf"][0] - geo.DOC_ORIGIN[0]) // geo.COL_STEP
        self.assertEqual(col_office, 0)
        self.assertEqual(col_pdf, 2)

    def test_none_mtime_is_treated_as_oldest(self):
        items = [doc("no_mtime.docx", 0, None), doc("recent.docx", 1, 5.0)]
        pos = self.positions(items)
        self.assertLess(pos["recent.docx"][1], pos["no_mtime.docx"][1])

    def test_folders_and_files_mixed(self):
        items = [Rec("proj", "folder", 0, 1.0), doc("a.docx", 1, 1.0)]
        pos = self.positions(items)
        self.assertEqual(pos["proj"][0], geo.DOC_ORIGIN[0])
        self.assertNotEqual(pos["a.docx"][0], geo.DOC_ORIGIN[0])


class DocBoundsTest(unittest.TestCase):
    def test_doc_rows_never_exceed_bottom(self):
        items = [doc(f"a{i}.docx", i, float(i)) for i in range(40)]
        for p in zp.plan_layout(items, []):
            if p.pos is not None:
                self.assertLessEqual(p.pos[1] + geo.ROW_STEP, geo.WORK_H)

    def test_doc_columns_stay_left_of_avoid_line_or_go_unplaced(self):
        # 单组 120 项 = 15 列；第 14 列 x=2113 已越避让线，必须不落位
        items = [doc(f"a{i}.docx", i, float(i)) for i in range(120)]
        plan = zp.plan_layout(items, [])
        placed = [p for p in plan if p.pos is not None]
        unplaced = [p for p in plan if p.pos is None]
        self.assertEqual(len(placed), 14 * geo.DOC_MAX_ROWS)
        self.assertEqual(len(unplaced), 120 - 14 * geo.DOC_MAX_ROWS)
        for p in placed:
            self.assertLess(p.pos[0], geo.AVOID_X)

    def test_doc_and_app_and_recycle_never_collide(self):
        items = [Rec(f"s{i}", "shortcut", i) for i in range(4)]
        items += [doc(f"a{i}.docx", 10 + i, float(i)) for i in range(10)]
        items += [Rec("Recycle Bin", "special", 99)]
        positions = [p.pos for p in zp.plan_layout(items, []) if p.pos is not None]
        self.assertEqual(len(positions), len(set(positions)))


if __name__ == "__main__":
    unittest.main()
