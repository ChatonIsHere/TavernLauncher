"""The rules that decide whether a copy shipped in Patch/ may stand in for a
download that won't complete.

There was a fallback here before and it was removed, so these tests are
mostly a record of the two ways it went wrong: it installed whatever file was
sitting in Patch/ without checking it was the file the build shipped, and it
installed it whenever a download failed, including over a working install that
was newer. Both failures reported plain success. Everything below either
pins the check that closes one of those, or pins the honest reporting that
makes an offline install distinguishable from a fetched one afterwards.
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
from tavern_shared import patch as pt
from tavern_shared.paths import _sha256_file

PAYLOAD = b"MZ" + b"\0" * 8000   # passes _verify_payload_type's "dll" check


class BundledTestCase(unittest.TestCase):
    """A throwaway Patch/ folder holding one fake tavernlib payload, plus a
    game dir to install it into."""

    COMPONENT = "tavernlib"
    FILENAME = "TavernLib.dll"
    TAG = "v1.5.1"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_bundled_")
        self.patch_dir = os.path.join(self.tmp, "Patch")
        self.game = os.path.join(self.tmp, "game")
        os.makedirs(self.patch_dir)
        os.makedirs(os.path.join(self.game, "Plugins"))
        self.payload = os.path.join(self.patch_dir, self.FILENAME)
        with open(self.payload, "wb") as f:
            f.write(PAYLOAD)
        self.write_manifest()
        self._real_dir = bundled.bundled_dir
        bundled.bundled_dir = lambda: self.patch_dir

    def tearDown(self):
        bundled.bundled_dir = self._real_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_manifest(self, **overrides):
        entry = {"filename": self.FILENAME, "tag": self.TAG,
                 "sha256": _sha256_file(self.payload),
                 "size": os.path.getsize(self.payload)}
        entry.update(overrides)
        with open(os.path.join(self.patch_dir, "bundled.json"), "w", encoding="utf-8") as f:
            json.dump({self.COMPONENT: entry}, f)


class PayloadVerification(BundledTestCase):
    """A file in Patch/ is only a fallback if it is the file the build put
    there. A half-synced update, a partial download, or something antivirus
    has rewritten is a different file wearing the right name -- and the old
    fallback would have installed any of them."""

    def test_the_shipped_copy_is_usable(self):
        self.assertEqual(bundled.verified_payload(self.COMPONENT), self.payload)

    def test_a_payload_whose_bytes_changed_is_not_usable(self):
        """Same name, same length, different content -- the case a size check
        alone lets through."""
        with open(self.payload, "r+b") as f:
            f.seek(len(PAYLOAD) - 1)
            f.write(b"\xff")
        self.assertIsNone(bundled.verified_payload(self.COMPONENT))

    def test_a_truncated_payload_is_not_usable(self):
        with open(self.payload, "wb") as f:
            f.write(PAYLOAD[:100])
        self.assertIsNone(bundled.verified_payload(self.COMPONENT))

    def test_a_missing_payload_is_not_usable(self):
        os.remove(self.payload)
        self.assertIsNone(bundled.verified_payload(self.COMPONENT))

    def test_a_build_that_ships_no_manifest_has_no_fallback(self):
        """Not an error -- it's the download-only behaviour this replaced."""
        os.remove(os.path.join(self.patch_dir, "bundled.json"))
        self.assertEqual(bundled.load_manifest(), {})
        self.assertIsNone(bundled.verified_payload(self.COMPONENT))

    def test_an_unlisted_component_has_no_fallback(self):
        self.assertIsNone(bundled.verified_payload("melonloader"))


class SubstitutionRules(BundledTestCase):
    """When the shipped copy is allowed to overwrite what's already installed.
    The shipped copy is a snapshot from whenever the build was cut, so being
    verified is not enough on its own -- it also has to be an improvement."""

    def test_anything_verified_beats_a_broken_install(self):
        self.assertEqual(
            bundled.usable_payload(self.COMPONENT, True, None), self.payload)

    def test_a_broken_install_is_repaired_even_from_a_newer_one(self):
        """install_broken already means the recorded tag describes nothing
        trustworthy, so it must not be allowed to veto the repair."""
        self.assertEqual(
            bundled.usable_payload(self.COMPONENT, True, "v9.9.9"), self.payload)

    def test_a_newer_shipped_copy_may_update_a_working_install(self):
        self.assertEqual(
            bundled.usable_payload(self.COMPONENT, False, "v1.4.0"), self.payload)

    def test_an_older_shipped_copy_never_downgrades_a_working_install(self):
        """The original fallback's worst behaviour: one unreachable GitHub and
        a working install silently went backwards to whatever the build had."""
        self.assertIsNone(bundled.usable_payload(self.COMPONENT, False, "v1.6.0"))

    def test_the_same_release_is_not_worth_reinstalling(self):
        """Nothing changes on disk, and the install would be relabelled as an
        offline one when it wasn't."""
        self.assertIsNone(bundled.usable_payload(self.COMPONENT, False, self.TAG))

    def test_an_unorderable_installed_tag_refuses_rather_than_guesses(self):
        self.assertIsNone(bundled.usable_payload(self.COMPONENT, False, "nightly"))

    def test_an_unorderable_shipped_tag_refuses_rather_than_guesses(self):
        self.write_manifest(tag="rolling")
        self.assertIsNone(bundled.usable_payload(self.COMPONENT, False, "v1.0.0"))

    def test_version_ordering_is_numeric_not_lexical(self):
        """v1.10.0 is newer than v1.9.0 even though it sorts before it."""
        self.write_manifest(tag="v1.10.0")
        self.assertEqual(
            bundled.usable_payload(self.COMPONENT, False, "v1.9.0"), self.payload)


class OfflineInstall(BundledTestCase):
    """_install_tavernlib end to end with GitHub unreachable."""

    def setUp(self):
        super().setUp()
        self._real_download = mi._download_with_retries
        mi._download_with_retries = self._refuse

    def tearDown(self):
        mi._download_with_retries = self._real_download
        super().tearDown()

    @staticmethod
    def _refuse(*_a, **_k):
        raise mi.DownloadError("no route to host")

    def _installed(self):
        return os.path.join(self.game, "Plugins", self.FILENAME)

    def test_a_fresh_machine_gets_the_shipped_copy(self):
        mi._install_tavernlib(self.game, lambda _m: None)
        self.assertTrue(os.path.isfile(self._installed()))
        self.assertEqual(_sha256_file(self._installed()), _sha256_file(self.payload))

    def test_the_install_records_the_real_release_it_installed(self):
        """The old fallback recorded "bundled:<hash>", which named no release
        at all, so the Setup row could only hide it."""
        mi._install_tavernlib(self.game, lambda _m: None)
        meta = mi._load_mod_meta(self.game)
        self.assertEqual(meta["tavernlib_tag"], self.TAG)
        self.assertEqual(meta["tavernlib_sha256"], _sha256_file(self.payload))

    def test_the_install_is_marked_as_coming_from_the_shipped_copy(self):
        mi._install_tavernlib(self.game, lambda _m: None)
        self.assertEqual(mi._load_mod_meta(self.game)["tavernlib_source"], "bundled")

    def test_the_recorded_fingerprint_can_never_match_a_real_one(self):
        """So the first check that does reach GitHub reads 'outdated' and pulls
        the published file, instead of an offline install looking current
        forever."""
        mi._install_tavernlib(self.game, lambda _m: None)
        fingerprint = mi._load_mod_meta(self.game)["tavernlib_fingerprint"]
        self.assertTrue(fingerprint.startswith("bundled:"))

    def test_a_later_online_install_clears_the_offline_marker(self):
        mi._install_tavernlib(self.game, lambda _m: None)
        mi._download_with_retries = self._succeed
        mi._install_tavernlib(self.game, lambda _m: None)
        meta = mi._load_mod_meta(self.game)
        self.assertNotIn("tavernlib_source", meta)
        self.assertEqual(meta["tavernlib_fingerprint"], '"etag-v2"')

    def _succeed(self, url, dest, on_progress, **_k):
        with open(dest, "wb") as f:
            f.write(PAYLOAD)
        return {"ETag": '"etag-v2"'}

    def test_nothing_usable_still_raises_so_setup_offers_manual_install(self):
        """The Setup window keys manual-install instructions off DownloadError
        specifically -- swallowing it here would leave the user with a silent
        no-op instead of a way forward."""
        os.remove(self.payload)
        with self.assertRaises(mi.DownloadError):
            mi._install_tavernlib(self.game, lambda _m: None)

    def test_a_newer_working_install_is_left_alone_and_raises(self):
        """The download failed, the shipped copy is older, so there is nothing
        to do but hand the user manual instructions -- and critically, the
        working install must survive that."""
        dest = self._installed()
        with open(dest, "wb") as f:
            f.write(b"MZnewer")
        mi._save_mod_meta(self.game, {
            "tavernlib_sha256": _sha256_file(dest),
            "tavernlib_fingerprint": '"etag-real"',
            "tavernlib_tag": "v9.0.0",
        })
        with self.assertRaises(mi.DownloadError):
            mi._install_tavernlib(self.game, lambda _m: None)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"MZnewer")
        self.assertEqual(mi._load_mod_meta(self.game)["tavernlib_tag"], "v9.0.0")

    def test_the_shipped_copy_survives_being_installed_from(self):
        """It is not a temp file -- deleting it would leave the next offline
        attempt with no fallback at all."""
        mi._install_tavernlib(self.game, lambda _m: None)
        self.assertTrue(os.path.isfile(self.payload))


class BrokenInstallReadings(BundledTestCase):
    """_tavernlib_install_broken is the network-free half of
    _tavernlib_status, and has to agree with it about which installs are not
    worth protecting -- it is what decides whether the tag comparison above
    is consulted at all."""

    def _write(self, body=PAYLOAD):
        dest = os.path.join(self.game, "Plugins", self.FILENAME)
        with open(dest, "wb") as f:
            f.write(body)
        return dest

    def test_nothing_installed_is_broken(self):
        self.assertTrue(mi._tavernlib_install_broken(self.game))

    def test_a_file_failing_its_recorded_hash_is_broken(self):
        self._write()
        mi._save_mod_meta(self.game, {"tavernlib_sha256": "0" * 64,
                                      "tavernlib_fingerprint": "x",
                                      "tavernlib_tag": "v1.0.0"})
        self.assertTrue(mi._tavernlib_install_broken(self.game))

    def test_an_incomplete_record_is_broken(self):
        dest = self._write()
        mi._save_mod_meta(self.game, {"tavernlib_sha256": _sha256_file(dest)})
        self.assertTrue(mi._tavernlib_install_broken(self.game))

    def test_a_complete_intact_install_is_not_broken(self):
        dest = self._write()
        mi._save_mod_meta(self.game, {"tavernlib_sha256": _sha256_file(dest),
                                      "tavernlib_fingerprint": '"etag"',
                                      "tavernlib_tag": "v1.0.0"})
        self.assertFalse(mi._tavernlib_install_broken(self.game))


class PatchOfflineInstall(BundledTestCase):
    """apply_patch takes the same route, with one difference worth pinning:
    Root.Township.dll always exists, so presence proves nothing about whether
    the patch was ever applied."""

    COMPONENT = "patch"
    FILENAME = "themoddingtavern.dll"

    def setUp(self):
        super().setUp()
        self.managed = os.path.join(self.game, "A Township Tale_Data", "Managed")
        os.makedirs(self.managed)
        self.exe = os.path.join(self.game, "TownshipTale.exe")
        with open(os.path.join(self.managed, "Root.Township.dll"), "wb") as f:
            f.write(b"MZvanilla")
        self._real_download = mi._download_with_retries
        pt._download_with_retries = self._refuse

    def tearDown(self):
        pt._download_with_retries = self._real_download
        super().tearDown()

    @staticmethod
    def _refuse(*_a, **_k):
        raise mi.DownloadError("no route to host")

    def test_an_unpatched_game_is_broken_despite_the_file_existing(self):
        self.assertTrue(pt._patch_install_broken(self.exe))

    def test_the_shipped_patch_is_applied_offline(self):
        self.assertEqual(pt.apply_patch(self.exe, lambda _m: None), "downloaded")
        applied = os.path.join(self.managed, "Root.Township.dll")
        self.assertEqual(_sha256_file(applied), _sha256_file(self.payload))
        meta = mi._load_mod_meta(self.game)
        self.assertEqual(meta["patch_tag"], self.TAG)
        self.assertEqual(meta["patch_source"], "bundled")
        self.assertTrue(meta["patch_fingerprint"].startswith("bundled:"))

    def test_no_download_and_no_payload_leaves_the_game_file_untouched(self):
        os.remove(self.payload)
        with self.assertRaises(mi.DownloadError):
            pt.apply_patch(self.exe, lambda _m: None)
        with open(os.path.join(self.managed, "Root.Township.dll"), "rb") as f:
            self.assertEqual(f.read(), b"MZvanilla")


if __name__ == "__main__":
    unittest.main()
