import unittest

import zones_geometry as geo


class ObservedStepsTest(unittest.TestCase):
    def test_three_columns_and_rows(self):
        coords = [(13, 198), (13, 296), (13, 394), (163, 198), (313, 198)]
        self.assertEqual(geo.observed_steps(coords), (150, 98))

    def test_single_column_has_no_column_step(self):
        coords = [(13, 2), (13, 100), (13, 198)]
        self.assertEqual(geo.observed_steps(coords), (None, 98))

    def test_empty(self):
        self.assertEqual(geo.observed_steps([]), (None, None))

    def test_uses_smallest_gap_not_average(self):
        coords = [(0, 0), (150, 0), (400, 0)]
        self.assertEqual(geo.observed_steps(coords)[0], 150)


class LayoutBoundsTest(unittest.TestCase):
    def test_app_zone_origin(self):
        self.assertEqual(geo.app_slot(0, 0), geo.APP_ORIGIN)

    def test_app_zone_stays_left_of_avoid_line(self):
        right = geo.APP_ORIGIN[0] + geo.APP_COLS * geo.COL_STEP
        self.assertLess(right, geo.AVOID_X)

    def test_app_slots_count(self):
        self.assertEqual(geo.APP_SLOTS, 12)

    def test_doc_zone_bottom_bound(self):
        bottom = geo.doc_cell(0, geo.DOC_MAX_ROWS - 1)[1] + geo.ROW_STEP
        self.assertLessEqual(bottom, geo.WORK_H)

    def test_recycle_bin_sits_on_bottom_edge(self):
        self.assertLessEqual(geo.RECYCLE_POS[1] + geo.ROW_STEP, geo.WORK_H)

    def test_icon_origins_are_lattice_congruent(self):
        # explorer 会把落位吸附到晶格；常量不同余就会被悄悄挪走（实测踩过）
        for origin in (geo.APP_ORIGIN, geo.DOC_ORIGIN, geo.RECYCLE_POS):
            self.assertEqual((origin[0] - geo.APP_ORIGIN[0]) % geo.COL_STEP, 0, origin)
            self.assertEqual((origin[1] - geo.APP_ORIGIN[1]) % geo.ROW_STEP, 0, origin)

    def test_label_row_is_between_zones(self):
        app_bottom = geo.APP_ORIGIN[1] + geo.APP_ROWS * geo.ROW_STEP
        self.assertGreater(geo.LABEL_Y, app_bottom)
        self.assertLess(geo.LABEL_Y, geo.DOC_ORIGIN[1])


if __name__ == "__main__":
    unittest.main()
