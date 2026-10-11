import unittest
from datetime import datetime, timedelta, timezone

import usage_score as us
from tests.records import Rec, shortcut


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class DecayTest(unittest.TestCase):
    def test_half_life_is_fourteen_days(self):
        self.assertAlmostEqual(us.decay(14.0), 0.5)

    def test_zero_age_is_full_weight(self):
        self.assertAlmostEqual(us.decay(0.0), 1.0)

    def test_two_half_lives(self):
        self.assertAlmostEqual(us.decay(28.0), 0.25)


class ScoreStartsTest(unittest.TestCase):
    def test_single_start_decays_by_age(self):
        events = [(NOW - timedelta(days=14), "a.exe")]
        self.assertAlmostEqual(us.score_starts(events, NOW)["a.exe"], 0.5)

    def test_multiple_starts_sum_independent_decays(self):
        events = [(NOW, "a.exe"), (NOW - timedelta(days=14), "a.exe"), (NOW, "b.exe")]
        scores = us.score_starts(events, NOW)
        self.assertAlmostEqual(scores["a.exe"], 1.5)
        self.assertAlmostEqual(scores["b.exe"], 1.0)

    def test_current_time_is_a_parameter_not_read_internally(self):
        events = [(NOW - timedelta(days=14), "a.exe")]
        later = NOW + timedelta(days=14)
        self.assertAlmostEqual(us.score_starts(events, later)["a.exe"], 0.25)


class UserAssistParseTest(unittest.TestCase):
    def blob(self, count, filetime):
        data = bytearray(72)
        data[0:4] = (145).to_bytes(4, "little")  # 会话/版本常量字段，非次数
        data[us.COUNT_OFFSET:us.COUNT_OFFSET + 4] = count.to_bytes(4, "little")
        data[us.FILETIME_OFFSET:us.FILETIME_OFFSET + 8] = filetime.to_bytes(8, "little")
        return bytes(data)

    def test_rot13_name_and_count_and_filetime(self):
        import codecs

        plain = r"C:\Program Files\app\app.exe"
        rotated = codecs.encode(plain, "rot_13")
        ft = 133000000000000000  # 任意合法 FILETIME
        name, entry = us.parse_userassist(rotated, self.blob(145, ft))
        self.assertEqual(name, plain)
        self.assertEqual(entry.count, 145)
        self.assertIsInstance(entry.last, datetime)

    def test_short_blob_is_rejected(self):
        self.assertIsNone(us.parse_userassist("abc", b"\x00" * 10))

    def test_zero_filetime_is_rejected(self):
        self.assertIsNone(us.parse_userassist("abc", self.blob(3, 0)))


class FuseIconsTest(unittest.TestCase):
    def setUp(self):
        self.items = [shortcut("Kimi", target=r"C:\P\Kimi.exe")]

    def test_empty_log_still_ranks_from_prior(self):
        prior = {r"c:\p\kimi.exe": us.PriorEntry(100, NOW - timedelta(days=7))}
        fused = us.fuse_icons(prior, [], NOW, self.items)
        self.assertIn("Kimi", fused)
        self.assertGreater(fused["Kimi"], 0)

    def test_abundant_log_suppresses_prior(self):
        prior = {r"c:\p\kimi.exe": us.PriorEntry(1000, NOW)}
        events = [(NOW, r"C:\P\Kimi.exe")] * 99
        fused = us.fuse_icons(prior, events, NOW, self.items)
        self.assertLess(fused["Kimi"] - 99.0, 1000.0 * 0.01 + 1e-9)

    def test_lnk_prior_recedes_once_the_icon_appears_in_log(self):
        # 任务栏 .lnk 先验：日志只记 .exe，退位必须发生在图标层面而非路径层面
        prior = {r"{guid}\taskbar\kimi.lnk": us.PriorEntry(50, NOW)}
        events = [(NOW, r"C:\P\Kimi.exe")] * 9
        fused = us.fuse_icons(prior, events, NOW, self.items)
        self.assertLess(fused["Kimi"] - 9.0, 50.0 * 0.1 + 1e-9)

    def test_case_mismatch_does_not_split_one_app(self):
        prior = {r"C:\P\KIMI.EXE": us.PriorEntry(10, NOW)}
        events = [(NOW, r"c:\p\kimi.exe")] * 5
        fused = us.fuse_icons(prior, events, NOW, self.items)
        self.assertEqual(len(fused), 1)
        self.assertLess(fused["Kimi"] - 5.0, 10.0 * 0.2 + 1e-9)

    def test_unmapped_exes_never_reach_ranking(self):
        fused = us.fuse_icons({}, [(NOW, "noise.exe")], NOW, self.items)
        self.assertEqual(fused, {})


class MapToIconsTest(unittest.TestCase):
    def test_target_match_is_case_insensitive(self):
        items = [shortcut("Kimi", target=r"C:\P\Kimi.exe")]
        ranking = us.map_to_icons({r"c:\p\kimi.exe": 3.0}, items)
        self.assertEqual(ranking, {"Kimi": 3.0})

    def test_unmapped_exes_are_dropped_from_ranking(self):
        items = [shortcut("Kimi", target=r"C:\P\Kimi.exe")]
        ranking = us.map_to_icons({r"c:\p\kimi.exe": 3.0, "noise.exe": 9.0}, items)
        self.assertEqual(set(ranking), {"Kimi"})

    def test_non_shortcut_items_never_rank(self):
        items = [Rec("a.docx", "file", 0), Rec("Recycle Bin", "special", 1)]
        self.assertEqual(us.map_to_icons({"x.exe": 5.0}, items), {})

    def test_userassist_lnk_entries_map_by_stem(self):
        items = [shortcut("Kimi", target=r"C:\P\Kimi.exe")]
        ranking = us.map_to_icons({r"C:\x\TaskBar\Kimi.lnk": 2.0}, items)
        self.assertEqual(ranking, {"Kimi": 2.0})


class NoPrefetchTest(unittest.TestCase):
    def test_module_never_touches_prefetch(self):
        from pathlib import Path

        source = Path(us.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Prefetch", source)


if __name__ == "__main__":
    unittest.main()
