import unittest

import zones_geometry as geo
import zones_plan as zp
from tests.records import shortcut


class RecommendFillTest(unittest.TestCase):
    def plan_of(self, items, pinned=(), scores=None):
        return {p.name: p for p in zp.plan_layout(items, pinned, scores)}

    def test_pinned_keep_front_slots_regardless_of_score(self):
        items = [shortcut("low", 0), shortcut("hot", 1)]
        plan = self.plan_of(items, ["low"], {"hot": 99.0, "low": 0.1})
        self.assertEqual(plan["low"].slot, 0)
        self.assertEqual(plan["low"].source, "pinned")
        self.assertEqual(plan["hot"].slot, 1)
        self.assertEqual(plan["hot"].source, "recommended")

    def test_remaining_slots_fill_by_score_desc(self):
        items = [shortcut("a", 0), shortcut("b", 1), shortcut("c", 2)]
        plan = self.plan_of(items, [], {"c": 3.0, "a": 1.0, "b": 2.0})
        self.assertEqual([plan["c"].slot, plan["b"].slot, plan["a"].slot], [0, 1, 2])

    def test_recommendation_never_takes_a_pinned_slot(self):
        items = [shortcut(f"s{i}", i) for i in range(6)]
        plan = self.plan_of(items, ["s5", "s4"], {f"s{i}": float(i) for i in range(6)})
        self.assertEqual(plan["s5"].slot, 0)
        self.assertEqual(plan["s4"].slot, 1)
        for i in range(4):  # s0..s3 都未钉，只能填第 2 栏位之后
            self.assertGreaterEqual(plan[f"s{i}"].slot, 2)

    def test_pinned_full_leaves_no_room_for_recommendations(self):
        pinned = [f"s{i}" for i in range(geo.APP_SLOTS)]
        items = pinned + ["x0", "x1"]
        recs = {n: 50.0 for n in items}
        plan = self.plan_of([shortcut(n, i) for i, n in enumerate(items)], pinned, recs)
        placed = [p for p in plan.values() if p.pos is not None]
        self.assertEqual(len(placed), geo.APP_SLOTS)
        self.assertIsNone(plan["x0"].pos)
        self.assertIsNone(plan["x1"].pos)
        self.assertEqual(
            sorted(p.source for p in placed), ["pinned"] * geo.APP_SLOTS
        )

    def test_equal_scores_keep_stable_index_order_and_repeat(self):
        items = [shortcut("b", 0), shortcut("a", 1)]
        first = self.plan_of(items, [], {"a": 1.0, "b": 1.0})
        second = self.plan_of(items, [], {"a": 1.0, "b": 1.0})
        self.assertEqual(first["b"].slot, 0)
        self.assertEqual(first["a"].slot, 1)
        self.assertEqual(first, second)

    def test_unscored_shortcuts_still_take_leftover_slots(self):
        items = [shortcut("scored", 0), shortcut("ghost", 1)]
        plan = self.plan_of(items, [], {"scored": 2.0})
        self.assertEqual(plan["scored"].slot, 0)
        self.assertEqual(plan["ghost"].slot, 1)
        self.assertEqual(plan["ghost"].source, "recommended")

    def test_multiple_unscored_keep_relative_index_order(self):
        items = [shortcut("z", 0), shortcut("a", 1), shortcut("m", 2)]
        plan = self.plan_of(items, [], {})
        self.assertEqual([plan["z"].slot, plan["a"].slot, plan["m"].slot], [0, 1, 2])

    def test_overflowed_pinned_item_keeps_its_identity(self):
        pinned = [f"s{i}" for i in range(geo.APP_SLOTS + 1)]
        items = [shortcut(n, i) for i, n in enumerate(pinned)]
        plan = self.plan_of(items, pinned, {})
        last = plan[pinned[-1]]
        self.assertIsNone(last.pos)
        self.assertEqual(last.source, "pinned")

    def test_no_scores_argument_behaves_like_zero_three(self):
        items = [shortcut("a", 0), shortcut("b", 1)]
        plan = self.plan_of(items, [])
        self.assertEqual(plan["a"].slot, 0)
        self.assertEqual(plan["b"].slot, 1)


if __name__ == "__main__":
    unittest.main()
