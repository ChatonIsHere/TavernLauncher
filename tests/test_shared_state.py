"""The install record when both launchers are pointed at the same game folder.

.tavern_mods_meta.json lives in game_dir rather than in either app's own
state precisely so the client launcher and the server launcher can see each
other's installs -- _patch_status says so outright. Both of them write it
too, and the write used to be a plain truncate-and-dump of a dict that was
read before a download started. Each test below covers a way that sharing
lost data instead of coordinating.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared import bundled
from tavern_shared import mod_install as mi
from tavern_shared.paths import _sha256_file

PAYLOAD = b"MZ" + b"\0" * 8000


class SharedInstallRecord(unittest.TestCase):

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="tavern_game_")

    def tearDown(self):
        shutil.rmtree(self.game, ignore_errors=True)

    def test_an_update_keeps_fields_it_does_not_touch(self):
        """The lost update in one test: the server launcher records TavernLib
        while the client launcher is mid-way through applying the patch. The
        patch fields have to survive the TavernLib write."""
        mi._save_mod_meta(self.game, {"patch_sha256": "abc", "patch_tag": "v1.5.1"})
        mi._update_mod_meta(self.game, {"tavernlib_tag": "v1.5.1"})
        meta = mi._load_mod_meta(self.game)
        self.assertEqual(meta["patch_tag"], "v1.5.1")
        self.assertEqual(meta["patch_sha256"], "abc")
        self.assertEqual(meta["tavernlib_tag"], "v1.5.1")

    def test_drop_removes_only_what_it_names(self):
        mi._save_mod_meta(self.game, {"patch_tag": "v1", "tavernlib_tag": "v2"})
        mi._update_mod_meta(self.game, {}, ["patch_tag"])
        meta = mi._load_mod_meta(self.game)
        self.assertNotIn("patch_tag", meta)
        self.assertEqual(meta["tavernlib_tag"], "v2")

    def test_the_record_is_swapped_in_never_truncated_in_place(self):
        """A reader in the other process must see the old file or the new one,
        never the empty window a plain open(w) opens -- _load_mod_meta turns a
        JSON error into {}, which reads as "nothing is installed", which makes
        Automatic Setup reinstall all three components."""
        mi._save_mod_meta(self.game, {"patch_tag": "v1"})
        self.assertEqual([f for f in os.listdir(self.game) if f.endswith(".tmp")], [])
        self.assertEqual(mi._load_mod_meta(self.game)["patch_tag"], "v1")

    def test_an_unwritable_record_leaves_the_previous_one_intact(self):
        """Failing to write must not be the same as erasing: a launcher that
        can't save still has to leave the other one's view readable."""
        mi._save_mod_meta(self.game, {"patch_tag": "v1"})
        # A value json can't serialise fails mid-dump, after the temp file is
        # open -- the same shape a disk-full or blocked write takes.
        mi._save_mod_meta(self.game, {"bad": {1, 2, 3}})
        self.assertEqual(mi._load_mod_meta(self.game)["patch_tag"], "v1")
        self.assertEqual([f for f in os.listdir(self.game) if f.endswith(".tmp")], [])

    def test_a_real_install_keeps_the_other_launchers_fields(self):
        """Not just the helper -- a whole TavernLib install, run over a record
        the other launcher wrote first."""
        os.makedirs(os.path.join(self.game, "Plugins"))
        mi._save_mod_meta(self.game, {"patch_sha256": "abc", "patch_tag": "v1.5.1",
                                      "melonloader_tag": "v0.7.3"})
        patch_dir = tempfile.mkdtemp(prefix="tavern_patch_")
        payload = os.path.join(patch_dir, "TavernLib.dll")
        with open(payload, "wb") as f:
            f.write(PAYLOAD)
        with open(os.path.join(patch_dir, "bundled.json"), "w", encoding="utf-8") as f:
            json.dump({"tavernlib": {"filename": "TavernLib.dll", "tag": "v1.5.1",
                                     "sha256": _sha256_file(payload),
                                     "size": os.path.getsize(payload)}}, f)
        real_download, real_dir = mi._download_with_retries, bundled.bundled_dir
        mi._download_with_retries = self._refuse
        bundled.bundled_dir = lambda: patch_dir
        try:
            mi._install_tavernlib(self.game, lambda _m: None)
        finally:
            mi._download_with_retries = real_download
            bundled.bundled_dir = real_dir
            shutil.rmtree(patch_dir, ignore_errors=True)
        meta = mi._load_mod_meta(self.game)
        self.assertEqual(meta["patch_tag"], "v1.5.1")
        self.assertEqual(meta["melonloader_tag"], "v0.7.3")
        self.assertEqual(meta["tavernlib_tag"], "v1.5.1")

    @staticmethod
    def _refuse(*_a, **_k):
        raise mi.DownloadError("no route to host")


if __name__ == "__main__":
    unittest.main()
