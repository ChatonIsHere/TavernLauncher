"""The rules for when a copy shipped in Patch/ may stand in for a download
that won't complete: only the exact file the build shipped, only when it
improves the install, and recorded so an offline install is
distinguishable from a fetched one."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dependencies"))
import refresh_bundled_payloads as rbp
from conftest import stub, temp_dir
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
        self.tmp = temp_dir(self, "tavern_bundled_")
        self.patch_dir = os.path.join(self.tmp, "Patch")
        self.game = os.path.join(self.tmp, "game")
        os.makedirs(self.patch_dir)
        os.makedirs(os.path.join(self.game, "Plugins"))
        self.payload = os.path.join(self.patch_dir, self.FILENAME)
        with open(self.payload, "wb") as f:
            f.write(PAYLOAD)
        self.write_manifest()
        stub(self, bundled, "bundled_dir", lambda: self.patch_dir)
        # An online install reads the release tag from GitHub's redirect.
        stub(self, mi, "_get_latest_release_tag", lambda url: "v2.0.0")
        stub(self, mi, "_download_with_retries", self._refuse)

    @staticmethod
    def _refuse(*_a, **_k):
        raise mi.DownloadError("no route to host")

    def write_manifest(self, **overrides):
        entry = {"filename": self.FILENAME, "tag": self.TAG,
                 "sha256": _sha256_file(self.payload),
                 "size": os.path.getsize(self.payload)}
        entry.update(overrides)
        with open(os.path.join(self.patch_dir, "bundled.json"), "w", encoding="utf-8") as f:
            json.dump({self.COMPONENT: entry}, f)


class PayloadVerification(BundledTestCase):
    """A file in Patch/ is a fallback only if it is byte-for-byte the file the
    build recorded."""

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

    # (shipped tag, install broken?, installed tag, used?)
    CASES = [
        ("v1.5.1", True, None, True),        # anything verified beats a broken install
        ("v1.5.1", True, "v9.9.9", True),    # broken: its recorded tag can't veto the repair
        ("v1.5.1", False, "v1.4.0", True),   # newer may update a working install
        ("v1.5.1", False, "v1.6.0", False),  # never downgrades a working install
        ("v1.5.1", False, "v1.5.1", False),  # same release: nothing to gain, wrong label
        ("v1.5.1", False, "nightly", False), # unorderable installed tag refuses
        ("rolling", False, "v1.0.0", False), # unorderable shipped tag refuses
        ("v1.10.0", False, "v1.9.0", True),  # numeric, not lexical, ordering
    ]

    def test_substitution_rules(self):
        for shipped, broken, installed, used in self.CASES:
            with self.subTest(shipped=shipped, broken=broken, installed=installed):
                self.write_manifest(tag=shipped)
                self.assertEqual(bundled.usable_payload(self.COMPONENT, broken, installed),
                                 self.payload if used else None)


class OfflineInstall(BundledTestCase):
    """_install_tavernlib end to end with GitHub unreachable."""

    def _installed(self):
        return os.path.join(self.game, "Plugins", self.FILENAME)

    def test_a_fresh_machine_gets_the_shipped_copy(self):
        mi._install_tavernlib(self.game, lambda _m: None)
        self.assertTrue(os.path.isfile(self._installed()))
        self.assertEqual(_sha256_file(self._installed()), _sha256_file(self.payload))

    def test_the_install_records_the_real_release_it_installed(self):
        """Recorded under the shipped copy's real release tag."""
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
    """The network-free half of _tavernlib_status decides which installs are
    not worth protecting, and with it whether the tag comparison above is
    consulted at all."""

    def _broken(self):
        return mi._tavernlib_local_state(self.game)[1] is not None

    def _write(self, body=PAYLOAD):
        dest = os.path.join(self.game, "Plugins", self.FILENAME)
        with open(dest, "wb") as f:
            f.write(body)
        return dest

    def test_nothing_installed_is_broken(self):
        self.assertTrue(self._broken())

    def test_a_file_failing_its_recorded_hash_is_broken(self):
        self._write()
        mi._save_mod_meta(self.game, {"tavernlib_sha256": "0" * 64,
                                      "tavernlib_fingerprint": "x",
                                      "tavernlib_tag": "v1.0.0"})
        self.assertTrue(self._broken())

    def test_an_incomplete_record_is_broken(self):
        dest = self._write()
        mi._save_mod_meta(self.game, {"tavernlib_sha256": _sha256_file(dest)})
        self.assertTrue(self._broken())

    def test_a_complete_intact_install_is_not_broken(self):
        dest = self._write()
        mi._save_mod_meta(self.game, {"tavernlib_sha256": _sha256_file(dest),
                                      "tavernlib_fingerprint": '"etag"',
                                      "tavernlib_tag": "v1.0.0"})
        self.assertFalse(self._broken())


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

    def test_an_unpatched_game_is_broken_despite_the_file_existing(self):
        self.assertEqual(pt._patch_local_state(self.exe)[1], "missing")

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



class RefreshBundledPayloads(unittest.TestCase):
    """The build-time re-cut of Patch/. A failed refresh must leave the
    shipped payloads, and the bundled.json describing them, untouched."""

    def setUp(self):
        self.patch_dir = temp_dir(self, "tavern_refresh_")
        self.old = {}
        for _c, _u, filename, _k, _e in rbp.TARGETS:
            self.old[filename] = b"OLD-" + filename.encode()
            with open(os.path.join(self.patch_dir, filename), "wb") as f:
                f.write(self.old[filename])
        self.old["bundled.json"] = b'{"old": true}'
        with open(os.path.join(self.patch_dir, "bundled.json"), "wb") as f:
            f.write(self.old["bundled.json"])
        stub(self, rbp, "bundled_dir", lambda: self.patch_dir)
        stub(self, rbp, "_get_latest_release_tag", lambda url: "v1.0.0")
        stub(self, rbp, "_verify_payload_type", lambda *a: None)

    def _serve(self, fail_on=None):
        def download(url, dest, on_progress, **_k):
            if url == fail_on:
                raise mi.DownloadError("cut off")
            with open(dest, "wb") as f:
                f.write(b"NEW-" + url.encode())
            return {}
        stub(self, rbp, "_download_with_retries", download)

    def _contents(self):
        out = {}
        for name in os.listdir(self.patch_dir):
            with open(os.path.join(self.patch_dir, name), "rb") as f:
                out[name] = f.read()
        return out

    def test_a_failed_download_leaves_patch_untouched(self):
        self._serve(fail_on=rbp.TARGETS[-1][1])
        with self.assertRaises(mi.DownloadError):
            rbp.refresh()
        self.assertEqual(self._contents(), self.old)

    def test_a_full_refresh_swaps_everything_and_describes_it(self):
        self._serve()
        manifest = rbp.refresh()
        contents = self._contents()
        self.assertEqual(set(contents), set(self.old))
        for component, url, filename, _k, _e in rbp.TARGETS:
            self.assertEqual(contents[filename], b"NEW-" + url.encode())
            self.assertEqual(manifest[component]["sha256"],
                             _sha256_file(os.path.join(self.patch_dir, filename)))
        self.assertEqual(json.loads(contents["bundled.json"]), manifest)

if __name__ == "__main__":
    unittest.main()
