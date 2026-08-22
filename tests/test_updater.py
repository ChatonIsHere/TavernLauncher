"""The partial-update marker is one shared file in AppData for BOTH launcher
exes (deliberately not next to either exe — the marker exists to report that
writing next to the exe was blocked). The install_dir recorded inside it is
therefore load-bearing: without checking it, whichever launcher started first
consumed and cleared the other launcher's report, so the user it was actually
for never saw it.
"""
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dependencies"))
import updater


class PartialUpdateMarker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_marker_")
        self._real_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = os.path.join(self.tmp, "AppData")
        self.own_dir = os.path.join(self.tmp, "ClientInstall")
        self.other_dir = os.path.join(self.tmp, "ServerInstall")
        os.makedirs(self.own_dir)
        os.makedirs(self.other_dir)
        self._real_exe = sys.executable
        sys.executable = os.path.join(self.own_dir, "TavernLauncher - Client.exe")

    def tearDown(self):
        sys.executable = self._real_exe
        if self._real_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._real_appdata

    def test_own_marker_is_returned(self):
        updater._write_partial_update_marker(self.own_dir, ["Patch/a.dll"])
        marker = updater.read_partial_update_marker()
        self.assertIsNotNone(marker)
        self.assertEqual(marker["files"], ["Patch/a.dll"])

    def test_case_only_spelling_difference_is_still_own(self):
        """The writer records whatever spelling the finish-update process had;
        Windows paths compare case-insensitively, so a case difference must
        not make a launcher's own report look foreign."""
        updater._write_partial_update_marker(self.own_dir.upper(), ["Patch/a.dll"])
        self.assertIsNotNone(updater.read_partial_update_marker())

    def test_foreign_marker_is_ignored_and_preserved(self):
        """The whole bug: the client starting first must neither display nor
        destroy the server launcher's still-unshown report."""
        updater._write_partial_update_marker(self.other_dir, ["Patch/b.dll"])
        self.assertIsNone(updater.read_partial_update_marker())
        self.assertTrue(os.path.isfile(updater._marker_path()),
                        "the other launcher still needs to find this")

    def test_clear_does_not_eat_a_foreign_marker(self):
        updater._write_partial_update_marker(self.other_dir, ["Patch/b.dll"])
        updater.clear_partial_update_marker()
        self.assertTrue(os.path.isfile(updater._marker_path()))

    def test_clear_removes_an_own_marker(self):
        updater._write_partial_update_marker(self.own_dir, ["Patch/a.dll"])
        updater.clear_partial_update_marker()
        self.assertFalse(os.path.exists(updater._marker_path()))

    def test_clear_with_explicit_install_dir(self):
        """finish_update_if_requested passes the install dir it just updated
        explicitly — its own sys.executable still points into staging."""
        updater._write_partial_update_marker(self.other_dir, ["Patch/b.dll"])
        updater.clear_partial_update_marker(self.own_dir)
        self.assertTrue(os.path.isfile(updater._marker_path()))
        updater.clear_partial_update_marker(self.other_dir)
        self.assertFalse(os.path.exists(updater._marker_path()))

    def test_unreadable_marker_reads_as_none_and_is_cleared(self):
        """Garbage can't be shown to anyone, so it isn't worth preserving for
        either launcher — clearing it stops it wedging the mechanism."""
        path = updater._marker_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(updater.read_partial_update_marker())
        updater.clear_partial_update_marker()
        self.assertFalse(os.path.exists(path))

    def test_no_marker_reads_as_none(self):
        self.assertIsNone(updater.read_partial_update_marker())
        updater.clear_partial_update_marker()  # no-op, must not raise

    def test_marker_records_what_the_reader_needs(self):
        updater._write_partial_update_marker(self.own_dir, ["addons/x/y.dll"])
        with open(updater._marker_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["install_dir"], self.own_dir)
        self.assertEqual(raw["files"], ["addons/x/y.dll"])
        self.assertIn("when", raw)


if __name__ == "__main__":
    unittest.main()
