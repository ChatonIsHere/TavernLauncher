"""The checks that tell a real install apart from one that only looked like it.

Every test here covers a failure that used to present as success, which is the
only reason they're worth having: an install that visibly fails is already
handled everywhere, and a corrupt file that reports itself as corrupt needs no
help. The dangerous shape is the one where nothing raises, nothing is logged,
the status says "Up to date", and the file on disk is wrong -- reported as
"the download seemed to complete, but the file didn't actually update".

Three distinct mechanisms produce that shape, one per class below.
"""
import io
import os
import sys
import zipfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared import mod_install as mi


class FakeResponse(io.BytesIO):
    """Enough of an HTTP response for _download_with_progress: a body, a
    headers dict, and context-manager support."""

    def __init__(self, body, declared_length=None):
        super().__init__(body)
        length = len(body) if declared_length is None else declared_length
        self.headers = {"Content-Length": str(length), "ETag": '"etag-v1"'}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class TruncatedDownloads(unittest.TestCase):
    """read() returning b"" means "no more data is coming", not "the file is
    complete". A connection cut mid-transfer by a proxy, VPN or antivirus ends
    the download loop in exactly the same way a finished one does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_dl_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        self._real_urlopen = mi._urlopen_hard_timeout

    def tearDown(self):
        mi._urlopen_hard_timeout = self._real_urlopen

    def _serve(self, body, declared_length=None):
        mi._urlopen_hard_timeout = lambda *_a, **_k: FakeResponse(body, declared_length)

    def test_a_short_body_is_a_failure_not_a_success(self):
        """The whole bug in one test: 300 bytes arrive of a file the server
        said was 1000. Without the length check this returns normally and
        every caller then hashes the truncated file, gets a hash that of
        course matches itself, and installs it as verified."""
        self._serve(b"x" * 300, declared_length=1000)
        with self.assertRaises(RuntimeError) as caught:
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)
        self.assertIn("ended early", str(caught.exception))

    def test_the_partial_file_is_not_left_behind(self):
        """A partial file left at dest_path is worse than none: the next thing
        along finds a plausible-looking file and has no way to know it's a
        fragment."""
        self._serve(b"x" * 300, declared_length=1000)
        with self.assertRaises(RuntimeError):
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)
        self.assertFalse(os.path.exists(self.dest))

    def test_the_message_says_nothing_was_installed(self):
        """This failure happens before anything is written anywhere, and the
        user's reasonable fear at this point is a half-updated game. Saying so
        is the difference between "retry" and "reinstall everything"."""
        self._serve(b"x" * 300, declared_length=1000)
        with self.assertRaises(RuntimeError) as caught:
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)
        msg = str(caught.exception)
        self.assertIn("Nothing was installed", msg)
        self.assertIn("300", msg)   # what arrived
        self.assertIn("1,000", msg) # what was promised

    def test_a_complete_download_still_succeeds(self):
        """The check must not fire on the normal path -- a false positive here
        breaks every install."""
        self._serve(b"y" * 1000)
        headers = mi._download_with_progress("https://example.test/f.dll", self.dest,
                                             lambda _m: None)
        self.assertEqual(os.path.getsize(self.dest), 1000)
        self.assertEqual(headers.get("ETag"), '"etag-v1"')

    def test_no_content_length_is_not_treated_as_a_short_read(self):
        """Chunked responses don't declare a length. Absence of a promise is
        not a broken promise."""
        resp = FakeResponse(b"z" * 50)
        del resp.headers["Content-Length"]
        mi._urlopen_hard_timeout = lambda *_a, **_k: resp
        mi._download_with_progress("https://example.test/f.dll", self.dest, lambda _m: None)
        self.assertEqual(os.path.getsize(self.dest), 50)


class BlockedExtractions(unittest.TestCase):
    """extractall() can return without error having written nothing, and when
    MelonLoader is being updated rather than installed fresh, the previous
    version's files are still sitting there -- so a presence check passes and
    the new release tag gets recorded against the old files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_zip_")
        self.zip_path = os.path.join(self.tmp, "ml.zip")
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("version.dll", b"NEW-LOADER-BYTES")
            zf.writestr("MelonLoader/net6/MelonLoader.dll", b"NEW-CORE-BYTES")

    def test_a_real_extraction_verifies_clean(self):
        dest = os.path.join(self.tmp, "game_ok")
        os.makedirs(dest)
        with zipfile.ZipFile(self.zip_path) as zf:
            zf.extractall(dest)
            self.assertEqual(mi._verify_extracted(zf, dest), [])

    def test_stale_files_from_the_previous_version_are_caught(self):
        """The exact silent-update case. The files are present and plausible,
        so os.path.isfile-based checks pass, but they are the OLD ones -- the
        extract never landed."""
        dest = os.path.join(self.tmp, "game_stale")
        os.makedirs(os.path.join(dest, "MelonLoader", "net6"))
        with open(os.path.join(dest, "version.dll"), "wb") as f:
            f.write(b"OLD-LOADER-BYTES")
        with open(os.path.join(dest, "MelonLoader", "net6", "MelonLoader.dll"), "wb") as f:
            f.write(b"OLD-CORE-BYTES")

        self.assertTrue(mi._melonloader_installed(dest),
                        "precondition: the presence check is fooled by the old files")
        with zipfile.ZipFile(self.zip_path) as zf:
            bad = mi._verify_extracted(zf, dest)
        self.assertEqual(sorted(bad),
                         ["MelonLoader/net6/MelonLoader.dll", "version.dll"])

    def test_missing_files_are_caught(self):
        dest = os.path.join(self.tmp, "game_empty")
        os.makedirs(dest)
        with zipfile.ZipFile(self.zip_path) as zf:
            self.assertEqual(len(mi._verify_extracted(zf, dest)), 2)


class DriftAfterInstall(unittest.TestCase):
    """A recorded version tag or ETag describes the release we fetched. It
    says nothing about what is on disk now, so on its own it keeps reporting
    'current' through a quarantine, a game update, or a truncated reinstall."""

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="tavern_drift_")
        os.makedirs(os.path.join(self.game, "Plugins"))
        self.tl = os.path.join(self.game, "Plugins", mi.TAVERNLIB_FILENAME)
        with open(self.tl, "wb") as f:
            f.write(b"TAVERNLIB-V1")
        self.ml = os.path.join(self.game, "version.dll")
        with open(self.ml, "wb") as f:
            f.write(b"LOADER-V1")
        os.makedirs(os.path.join(self.game, "MelonLoader"))
        mi._save_mod_meta(self.game, {
            "tavernlib_fingerprint": '"etag-v1"',
            "tavernlib_sha256": mi._sha256_file(self.tl),
            "melonloader_tag": "v0.7.3",
            "melonloader_version_sha256": mi._sha256_file(self.ml),
        })
        self._real_fp = mi._fetch_remote_fingerprint
        self._real_tag = mi._get_melonloader_latest_tag
        # Remote says everything is current, so any non-'current' result below
        # can only have come from the local content check.
        mi._fetch_remote_fingerprint = lambda *_a, **_k: '"etag-v1"'
        mi._get_melonloader_latest_tag = lambda *_a, **_k: "v0.7.3"

    def tearDown(self):
        mi._fetch_remote_fingerprint = self._real_fp
        mi._get_melonloader_latest_tag = self._real_tag

    def test_baseline_is_current(self):
        self.assertEqual(mi._tavernlib_status(self.game), "current")
        self.assertEqual(mi._melonloader_status(self.game), "current")
        self.assertFalse(mi._mods_need_attention(self.game))

    def test_a_changed_tavernlib_is_damaged_not_current(self):
        with open(self.tl, "wb") as f:
            f.write(b"CORRUPTED")
        self.assertEqual(mi._tavernlib_status(self.game), "damaged")

    def test_a_changed_melonloader_is_damaged_not_current(self):
        with open(self.ml, "wb") as f:
            f.write(b"CORRUPTED")
        self.assertEqual(mi._melonloader_status(self.game), "damaged")

    def test_damage_raises_the_setup_alert(self):
        """Detecting it and not surfacing it would be no better than not
        detecting it -- this is what flashes the Setup button."""
        with open(self.tl, "wb") as f:
            f.write(b"CORRUPTED")
        self.assertTrue(mi._mods_need_attention(self.game))

    def test_no_recorded_hash_is_unknown_rather_than_damaged(self):
        """An install predating the hash being recorded has no baseline. That
        must not be reported as damage -- a false 'damaged' on every existing
        install would train people to ignore the state entirely."""
        meta = mi._load_mod_meta(self.game)
        del meta["tavernlib_sha256"]
        del meta["tavernlib_fingerprint"]
        mi._save_mod_meta(self.game, meta)
        self.assertEqual(mi._tavernlib_status(self.game), "unknown")

    def test_a_deleted_file_is_missing_not_damaged(self):
        os.remove(self.tl)
        self.assertEqual(mi._tavernlib_status(self.game), "missing")


if __name__ == "__main__":
    unittest.main()
