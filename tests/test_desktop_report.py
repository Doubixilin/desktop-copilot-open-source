import unittest

import desktop_icons as di


class DisplayWidthTest(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(di.display_width("cscec portal"), 12)

    def test_cjk_counts_double(self):
        self.assertEqual(di.display_width("QQ音乐"), 6)

    def test_pad_reaches_display_width(self):
        self.assertEqual(di.display_width(di.pad("QQ音乐", 10)), 10)
        self.assertEqual(di.display_width(di.pad("abc", 10)), 10)

    def test_pad_never_truncates(self):
        self.assertTrue(di.pad("很长的中文名字", 4).startswith("很长的中文名字"))


class KindTest(unittest.TestCase):
    def test_unmatched_is_special(self):
        self.assertEqual(di._kind("Recycle Bin", None), "special")

    def test_hidden_system_is_system(self):
        fs = {"path": r"C:\x\desktop.ini", "is_dir": False, "hidden": True, "system": True}
        self.assertEqual(di._kind("desktop.ini", fs), "system")

    def test_folder(self):
        fs = {"path": r"C:\x\proj", "is_dir": True, "hidden": False, "system": False}
        self.assertEqual(di._kind("proj", fs), "folder")

    def test_shortcut_uses_fs_path_not_display_name(self):
        fs = {"path": r"C:\x\Kimi.lnk", "is_dir": False, "hidden": False, "system": False}
        self.assertEqual(di._kind("Kimi", fs), "shortcut")

    def test_url(self):
        fs = {"path": r"C:\x\site.url", "is_dir": False, "hidden": False, "system": False}
        self.assertEqual(di._kind("site", fs), "url")

    def test_plain_file(self):
        fs = {"path": r"C:\x\个人简历.docx", "is_dir": False, "hidden": False, "system": False}
        self.assertEqual(di._kind("个人简历.docx", fs), "file")


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            di.DesktopItem(0, "个人简历.docx", 13, 198, "file", r"C:\x\个人简历.docx", None, False),
            di.DesktopItem(1, "Kimi", 13, 394, "shortcut", r"C:\x\Kimi.lnk", r"C:\p\Kimi.exe", False),
        ]

    def test_header_counts_items(self):
        first = di.report(self.items, (150, 98)).splitlines()[0]
        self.assertIn("desktop icons: 2", first)

    def test_cjk_names_survive_verbatim(self):
        out = di.report(self.items, (150, 98))
        self.assertIn("个人简历.docx", out)

    def test_one_line_per_item_plus_header(self):
        self.assertEqual(len(di.report(self.items, (150, 98)).splitlines()), 3)

    def test_target_shown_for_shortcuts_only(self):
        out = di.report(self.items, (150, 98))
        self.assertIn("-> C:\\p\\Kimi.exe", out)
        self.assertNotIn("-> C:\\x\\个人简历.docx", out)


if __name__ == "__main__":
    unittest.main()
