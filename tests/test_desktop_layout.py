import json
import tempfile
import unittest
from pathlib import Path

import desktop_icons as di
import desktop_layout as dl


def item(name, x, y):
    return di.DesktopItem(0, name, x, y, "file", None, None, False)


class RestoreMovesTest(unittest.TestCase):
    def setUp(self):
        self.current = [item("a.docx", 13, 2), item("b", 13, 100), item("new.txt", 300, 2)]
        self.snapshot = [
            {"name": "a.docx", "x": 13, "y": 500},
            {"name": "b", "x": 163, "y": 100},
            {"name": "gone.lnk", "x": 13, "y": 2},
        ]

    def test_moves_only_present_items(self):
        moves, missing, extra, ambiguous = dl.restore_moves(self.current, self.snapshot)
        self.assertEqual(sorted(m[0] for m in moves), ["a.docx", "b"])
        self.assertEqual(missing, ["gone.lnk"])
        self.assertEqual(extra, ["new.txt"])
        self.assertEqual(ambiguous, [])

    def test_move_carries_index_and_target_coords(self):
        moves, _, _, _ = dl.restore_moves(self.current, self.snapshot)
        self.assertIn(("a.docx", 13, 500), moves)

    def test_duplicate_current_names_are_reported_not_moved(self):
        current = [item("same", 13, 2), item("same", 163, 2)]
        snapshot = [{"name": "same", "x": 13, "y": 500}]
        moves, missing, extra, ambiguous = dl.restore_moves(current, snapshot)
        self.assertEqual(moves, [])
        self.assertEqual(ambiguous, ["same"])
        self.assertEqual(extra, [])

    def test_empty_snapshot_moves_nothing(self):
        moves, missing, extra, ambiguous = dl.restore_moves(self.current, [])
        self.assertEqual(moves, [])
        self.assertEqual(missing, [])
        self.assertEqual(ambiguous, [])
        self.assertEqual(len(extra), 3)


class SnapshotFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_round_trip_preserves_names_and_coords(self):
        path = dl.take_snapshot([item("个人简历.docx", 13, 2)], "run", self.dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["items"][0], {"name": "个人简历.docx", "x": 13, "y": 2})

    def test_factory_written_once_and_never_overwritten(self):
        first = dl.ensure_factory([item("a", 1, 1)], self.dir)
        content = first.read_text(encoding="utf-8")
        second = dl.ensure_factory([item("a", 999, 999)], self.dir)
        self.assertEqual(first, second)
        self.assertEqual(second.read_text(encoding="utf-8"), content)

    def test_run_snapshots_sorted_and_last(self):
        dl.take_snapshot([item("a", 1, 1)], "run", self.dir)
        dl.take_snapshot([item("a", 2, 2)], "run", self.dir)
        runs = dl.run_snapshots(self.dir)
        self.assertEqual(len(runs), 2)
        self.assertEqual(dl.load_snapshot("last", self.dir)["items"][0]["x"], 2)

    def test_same_tick_run_snapshots_do_not_overwrite(self):
        # Windows 时钟粒度约 15ms：同一时间戳内两次快照必须落成两个文件且 last 为后者
        real_now_tag = dl._now_tag
        dl._now_tag = lambda: "20260925T000000000000Z"
        try:
            dl.take_snapshot([item("a", 1, 1)], "run", self.dir)
            dl.take_snapshot([item("a", 2, 2)], "run", self.dir)
        finally:
            dl._now_tag = real_now_tag
        runs = dl.run_snapshots(self.dir)
        self.assertEqual(len(runs), 2)
        self.assertEqual(dl.load_snapshot("last", self.dir)["items"][0]["x"], 2)

    def test_missing_snapshot_raises(self):
        with self.assertRaises(FileNotFoundError):
            dl.load_snapshot("factory", self.dir)
        with self.assertRaises(FileNotFoundError):
            dl.load_snapshot("last", self.dir)

    def test_factory_excluded_from_run_list(self):
        dl.ensure_factory([item("a", 1, 1)], self.dir)
        self.assertEqual(dl.run_snapshots(self.dir), [])


if __name__ == "__main__":
    unittest.main()
