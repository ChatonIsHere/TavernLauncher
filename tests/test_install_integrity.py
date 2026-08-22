"""The checks that tell a real install apart from one that only looked like it.

Every test here covers a failure that used to present as success, which is the
only reason they're worth having: an install that visibly fails is already
handled everywhere, and a corrupt file that reports itself as corrupt needs no
help. The dangerous shape is the one where nothing raises, nothing is logged,
the status says "Up to date", and the file on disk is wrong -- reported as
"the download seemed to complete, but the file didn't actually update".

Each class below covers one mechanism that produces that shape.
"""
import io
import os
import sys
import zipfile
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared import mod_install as mi
from tavern_shared import patch as pt


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


class MidBodyFailures(unittest.TestCase):
    """A connection cut DURING the body raises out of resp.read() rather than
    ending the loop early — reset, TLS error, socket timeout, short chunked
    read. Those are the same network-side failures as a refused connect, so
    they must surface as DownloadError: the retry loop retries only that
    class, and the Setup window's manual-install handoff classifies on it."""

    class _DyingResponse(io.BytesIO):
        def __init__(self, body_before_death, exc):
            super().__init__(body_before_death)
            self._exc = exc
            self.headers = {"Content-Length": str(len(body_before_death) * 2)}

        def read(self, *a, **kw):
            data = super().read(*a, **kw)
            if not data:
                raise self._exc
            return data

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_midbody_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        self._real_urlopen = mi._urlopen_hard_timeout

    def tearDown(self):
        mi._urlopen_hard_timeout = self._real_urlopen

    def _serve_dying(self, exc):
        resp = self._DyingResponse(b"x" * 200, exc)
        mi._urlopen_hard_timeout = lambda *_a, **_k: resp

    def test_a_mid_body_reset_is_a_download_error(self):
        self._serve_dying(ConnectionResetError("peer reset"))
        with self.assertRaises(mi.DownloadError):
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)

    def test_a_mid_body_incomplete_read_is_a_download_error(self):
        import http.client
        self._serve_dying(http.client.IncompleteRead(b"x" * 200))
        with self.assertRaises(mi.DownloadError):
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)

    def test_the_partial_file_is_not_left_behind(self):
        self._serve_dying(ConnectionResetError("peer reset"))
        with self.assertRaises(mi.DownloadError):
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)
        self.assertFalse(os.path.exists(self.dest))

    def test_a_mid_body_drop_is_retried(self):
        """The point of the classification: the retry docstring promises a
        dropped connection often succeeds on the next try, which is only true
        if a drop mid-body actually reaches the retry loop."""
        calls = {"n": 0}
        good = b"y" * 400
        def _urlopen(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._DyingResponse(b"x" * 200, ConnectionResetError("peer reset"))
            return FakeResponse(good)
        mi._urlopen_hard_timeout = _urlopen
        mi._download_with_retries("https://example.test/f.dll", self.dest,
                                  lambda _m: None, retry_delay=0)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(os.path.getsize(self.dest), len(good))


class RefusedResponses(unittest.TestCase):
    """The two download failures Content-Length alone can never catch, closed
    for the setup components (patch, TavernLib, MelonLoader) that have no
    independent hash to verify against.

    First: the short-read check IS the truncation detection for a bare .dll,
    and it silently stops existing the moment a proxy rewrites the response
    to chunked encoding -- so for those downloads, no declared length is
    itself a failure (require_length=True), not a shrug. Second: a proxy
    serving a complete, correct-length block page defeats any length check;
    only looking at the content catches that."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_refuse_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        self._real_urlopen = mi._urlopen_hard_timeout

    def tearDown(self):
        mi._urlopen_hard_timeout = self._real_urlopen

    def test_no_content_length_is_refused_when_required(self):
        resp = FakeResponse(b"z" * 50)
        del resp.headers["Content-Length"]
        mi._urlopen_hard_timeout = lambda *_a, **_k: resp
        with self.assertRaises(mi.DownloadError) as caught:
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None, require_length=True)
        self.assertIn("Content-Length", str(caught.exception))
        self.assertFalse(os.path.exists(self.dest),
                         "refused before reading the body, so nothing lands on disk")

    def test_a_block_page_is_not_a_dll(self):
        """A school proxy's login page arrives complete, matches its own
        Content-Length, and hashes consistently. Without a content check it
        would be installed as Root.Township.dll and recorded as good."""
        with open(self.dest, "wb") as f:
            f.write(b"<!DOCTYPE html><html>Blocked by NetGuard</html>" * 200)
        with self.assertRaises(mi.DownloadError) as caught:
            mi._verify_payload_type(self.dest, "https://example.test/f.dll", "dll")
        self.assertIn("isn't a Windows DLL", str(caught.exception))

    def test_a_block_page_is_not_a_zip(self):
        with open(self.dest, "wb") as f:
            f.write(b"<html>Sign in to continue</html>" * 300)
        with self.assertRaises(mi.DownloadError):
            mi._verify_payload_type(self.dest, "https://example.test/ml.zip", "zip")

    def test_a_real_dll_shaped_payload_passes(self):
        with open(self.dest, "wb") as f:
            f.write(b"MZ" + b"\0" * 8192)
        mi._verify_payload_type(self.dest, "https://example.test/f.dll", "dll")

    def test_a_tiny_mz_prefixed_file_is_still_refused(self):
        """Magic alone isn't enough -- an error body could start with the
        right two bytes. Every real payload here is orders of magnitude over
        the floor."""
        with open(self.dest, "wb") as f:
            f.write(b"MZ error")
        with self.assertRaises(mi.DownloadError):
            mi._verify_payload_type(self.dest, "https://example.test/f.dll", "dll")


class RetriedDownloads(unittest.TestCase):
    """A dropped connection often succeeds on the next attempt -- the retry
    exists so the user isn't handed a failure the launcher could have
    absorbed. What escapes after the last attempt must say the retrying
    already happened: the Setup window presents it as "we tried, here's the
    manual route", which is only honest if it did."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tavern_retry_")
        self.dest = os.path.join(self.tmp, "payload.bin")

    def test_a_transient_failure_is_absorbed(self):
        calls = {"n": 0}
        def flaky(url, dest, on_progress, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise mi.DownloadError("connection reset")
            with open(dest, "wb") as f:
                f.write(b"ok")
            return {}
        real = mi._download_with_progress
        mi._download_with_progress = flaky
        try:
            mi._download_with_retries("https://example.test/f.dll", self.dest,
                                      lambda _m: None, retry_delay=0)
        finally:
            mi._download_with_progress = real
        self.assertEqual(calls["n"], 2)
        self.assertEqual(os.path.getsize(self.dest), 2)

    def test_exhausted_retries_report_the_attempt_count(self):
        real = mi._download_with_progress
        def always_down(*_a, **_kw):
            raise mi.DownloadError("connection reset")
        mi._download_with_progress = always_down
        try:
            with self.assertRaises(mi.DownloadError) as caught:
                mi._download_with_retries("https://example.test/f.dll", self.dest,
                                          lambda _m: None, attempts=3, retry_delay=0)
        finally:
            mi._download_with_progress = real
        self.assertIn("attempted 3 times", str(caught.exception))

    def test_a_local_failure_is_not_retried(self):
        """Retrying a permissions error or a full disk just triples the wait
        before the same failure. Only DownloadError is the transient kind."""
        calls = {"n": 0}
        def broken_disk(*_a, **_kw):
            calls["n"] += 1
            raise PermissionError("disk says no")
        real = mi._download_with_progress
        mi._download_with_progress = broken_disk
        try:
            with self.assertRaises(PermissionError):
                mi._download_with_retries("https://example.test/f.dll", self.dest,
                                          lambda _m: None, retry_delay=0)
        finally:
            mi._download_with_progress = real
        self.assertEqual(calls["n"], 1)

    def test_download_error_is_still_a_runtime_error(self):
        """Every existing caller catches RuntimeError; the subclass exists to
        carry extra meaning, not to slip past those handlers."""
        self.assertTrue(issubclass(mi.DownloadError, RuntimeError))


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
            "tavernlib_tag": "v1.5.1",
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

    def test_no_recorded_hash_is_unrecorded_rather_than_damaged(self):
        """An install predating the hash being recorded has no baseline. That
        must not be reported as damage -- a false 'damaged' on every existing
        install would train people to ignore the state entirely. It reads as
        'unrecorded': locally repairable, so Automatic Setup reinstalls it,
        unlike 'unknown' (complete record, check unavailable) which it must
        leave alone."""
        meta = mi._load_mod_meta(self.game)
        del meta["tavernlib_sha256"]
        del meta["tavernlib_fingerprint"]
        mi._save_mod_meta(self.game, meta)
        self.assertEqual(mi._tavernlib_status(self.game), "unrecorded")

    def test_an_incomplete_record_is_auto_setup_work_but_never_an_alarm(self):
        """The split in one test: 'unrecorded' is in AUTO_SETUP_STATES (a
        reinstall repairs it) but not NEEDS_ATTENTION (a working install
        shouldn't flash the launcher); 'unknown' is in neither (reinstalling
        over an unreachable GitHub is guaranteed failure)."""
        meta = mi._load_mod_meta(self.game)
        del meta["melonloader_tag"]
        mi._save_mod_meta(self.game, meta)
        self.assertEqual(mi._melonloader_status(self.game), "unrecorded")
        self.assertFalse(mi._mods_need_attention(self.game))
        self.assertIn("unrecorded", mi.AUTO_SETUP_STATES)
        self.assertNotIn("unknown", mi.AUTO_SETUP_STATES)
        self.assertNotIn("unrecorded", mi.NEEDS_ATTENTION)

    def test_a_failed_update_check_on_a_full_record_is_unknown(self):
        """The other side of the split: everything recorded, GitHub
        unreachable. Not 'unrecorded' -- a reinstall would fix nothing."""
        def _down(*_a, **_k):
            raise OSError("network unreachable")
        mi._fetch_remote_fingerprint = _down
        self.assertEqual(mi._tavernlib_status(self.game), "unknown")

    def test_a_deleted_file_is_missing_not_damaged(self):
        os.remove(self.tl)
        self.assertEqual(mi._tavernlib_status(self.game), "missing")


class StrippedFingerprintHeaders(unittest.TestCase):
    """A proxy stripping ETag/Last-Modified used to leave installs that could
    never converge: install-time recorded no fingerprint (or kept a stale one)
    while the remote check fell back to a size, so status read 'unrecorded' or
    'outdated' forever and Automatic Setup reinstalled every single run. Both
    sides now fall back to the same value — the file's full size."""

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="tavern_strip_")
        self._real_dl = mi._download_with_retries
        self._real_tag = mi._get_latest_release_tag
        self._real_fp = mi._fetch_remote_fingerprint

    def tearDown(self):
        mi._download_with_retries = self._real_dl
        mi._get_latest_release_tag = self._real_tag
        mi._fetch_remote_fingerprint = self._real_fp

    def test_install_records_the_size_fallback_and_converges(self):
        """One clean install on a header-stripping network must end at
        'current', not loop forever."""
        body = b"MZ" + b"\0" * 8192
        def fake_download(url, dest, on_progress, **_kw):
            with open(dest, "wb") as f:
                f.write(body)
            return {"Content-Length": str(len(body))}  # no ETag/Last-Modified
        mi._download_with_retries = fake_download
        mi._get_latest_release_tag = lambda *_a, **_k: "v1.5.1"
        mi._install_tavernlib(self.game, lambda _m: None)
        meta = mi._load_mod_meta(self.game)
        self.assertEqual(meta["tavernlib_fingerprint"], str(len(body)))
        # The remote check's fallback computes the same size, so they agree.
        mi._fetch_remote_fingerprint = lambda *_a, **_k: str(len(body))
        self.assertEqual(mi._tavernlib_status(self.game), "current")

    def test_remote_fallback_reads_the_full_size_out_of_content_range(self):
        """The ranged-GET fallback's Content-Length describes the 1-byte
        slice — a constant "1" that could never match anything install-time
        records. The full size lives in Content-Range."""
        class _Resp:
            headers = {"Content-Length": "1",
                       "Content-Range": "bytes 0-0/8194"}
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False
        def fake_urlopen(req, timeout=None):
            if req.get_method() == "HEAD":
                raise OSError("host rejects HEAD")
            return _Resp()
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            fp = mi._fetch_remote_fingerprint("https://example.test/f.dll")
        finally:
            urllib.request.urlopen = real
        self.assertEqual(fp, "8194")


class ReleaseTags(unittest.TestCase):
    """The release tag read from a 'latest' alias's redirect is the only
    human-readable version the patch and TavernLib have — their PE version
    resources are never bumped (TavernLib says 1.0.0.0 forever, the patch
    inherits the game's own 0.0.0.1) — so it's recorded at install time and
    shown on the Setup rows. Display only; the ETag fingerprint stays the
    update check."""

    def setUp(self):
        self._real = mi._get_redirect_location

    def tearDown(self):
        mi._get_redirect_location = self._real

    def test_the_tag_is_read_from_the_redirect_target(self):
        mi._get_redirect_location = lambda *_a, **_k: (
            "https://github.com/ModdingTavern/TavernLib/releases/download/v1.5.1/TavernLib.dll")
        self.assertEqual(mi._get_latest_release_tag("https://x/latest/download/TavernLib.dll"),
                         "v1.5.1")

    def test_no_redirect_means_no_tag_not_a_crash(self):
        mi._get_redirect_location = lambda *_a, **_k: None
        self.assertIsNone(mi._get_latest_release_tag("https://x/latest/download/f.dll"))

    def test_an_unexpected_location_shape_means_no_tag(self):
        mi._get_redirect_location = lambda *_a, **_k: "https://cdn.example/somewhere/else"
        self.assertIsNone(mi._get_latest_release_tag("https://x/latest/download/f.dll"))


class PatchStatus(unittest.TestCase):
    """The patch used to be the one component with no update detection: a
    binary applied/not-applied, so a newly published patch release read as
    "Up to date" forever. It now answers the same five states as the other
    two — with one reading of its own: Root.Township.dll always exists in an
    unmodified game (it's the game's own file being replaced), so presence
    proves nothing, and a vanilla DLL with nothing recorded is 'missing',
    never 'unknown'."""

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="tavern_patch_")
        self.exe = os.path.join(self.game, "A Township Tale.exe")
        managed = os.path.join(self.game, pt.PATCH_TARGET_SUBDIR)
        os.makedirs(managed)
        self.dll = os.path.join(managed, pt.PATCH_TARGET_FILENAME)
        with open(self.dll, "wb") as f:
            f.write(b"PATCHED-BYTES")
        mi._save_mod_meta(self.game, {
            "patch_sha256": mi._sha256_file(self.dll),
            "patch_fingerprint": '"etag-v1"',
            "patch_tag": "v1.5.1",
        })
        self._real_fp = pt._fetch_remote_fingerprint
        pt._fetch_remote_fingerprint = lambda *_a, **_k: '"etag-v1"'

    def tearDown(self):
        pt._fetch_remote_fingerprint = self._real_fp

    def test_baseline_is_current(self):
        self.assertEqual(pt._patch_status(self.exe), "current")
        self.assertFalse(pt._patch_needs_attention(self.exe))

    def test_a_new_release_is_outdated_not_current(self):
        """The exact gap this status exists to close: applied, intact, and
        stale. The old binary check called this applied and said nothing."""
        pt._fetch_remote_fingerprint = lambda *_a, **_k: '"etag-v2"'
        self.assertEqual(pt._patch_status(self.exe), "outdated")
        self.assertTrue(pt._patch_needs_attention(self.exe))

    def test_a_vanilla_dll_is_missing_not_unknown(self):
        """The game's own Root.Township.dll sitting there, nothing recorded:
        never patched. 'missing' alerts unconditionally; 'unknown' never
        does — collapsing these would silence the first-run alert."""
        mi._save_mod_meta(self.game, {})
        self.assertEqual(pt._patch_status(self.exe), "missing")

    def test_an_overwritten_patch_is_damaged(self):
        """A game update replacing Root.Township.dll leaves a real DLL of a
        plausible size in place — only the recorded hash can tell."""
        with open(self.dll, "wb") as f:
            f.write(b"VANILLA-AGAIN")
        self.assertEqual(pt._patch_status(self.exe), "damaged")
        self.assertTrue(pt._patch_needs_attention(self.exe))

    def test_a_failed_update_check_is_unknown_and_never_alarms(self):
        def _down(*_a, **_k):
            raise OSError("network unreachable")
        pt._fetch_remote_fingerprint = _down
        self.assertEqual(pt._patch_status(self.exe), "unknown")
        self.assertFalse(pt._patch_needs_attention(self.exe))

    def test_a_legacy_install_without_fingerprint_is_unrecorded(self):
        """Installs recorded before the fingerprint existed have no baseline
        to compare against GitHub. That's 'unrecorded', not 'damaged' or a
        false 'current' — Automatic Setup reinstalls it, which records one.
        Not an alarm: the install itself works."""
        meta = mi._load_mod_meta(self.game)
        del meta["patch_fingerprint"]
        mi._save_mod_meta(self.game, meta)
        self.assertEqual(pt._patch_status(self.exe), "unrecorded")
        self.assertFalse(pt._patch_needs_attention(self.exe))

    def test_a_deleted_dll_is_missing(self):
        os.remove(self.dll)
        self.assertEqual(pt._patch_status(self.exe), "missing")


if __name__ == "__main__":
    unittest.main()
