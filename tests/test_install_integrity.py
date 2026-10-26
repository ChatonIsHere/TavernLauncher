"""The checks that tell a real install apart from one that only looked like
it: each class covers a way an install can report success while the file
on disk is wrong."""
import io
import os
import sys
import zipfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from conftest import stub, temp_dir
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
        self.tmp = temp_dir(self, "tavern_dl_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        stub(self, mi, "_urlopen_hard_timeout", mi._urlopen_hard_timeout)

    def _serve(self, body, declared_length=None):
        mi._urlopen_hard_timeout = lambda *_a, **_k: FakeResponse(body, declared_length)

    def test_a_short_body_fails_says_so_and_leaves_nothing(self):
        """300 of a promised 1000 bytes: the fragment would hash consistently and
        install as verified. It must fail, say nothing was installed, and leave
        nothing at dest."""
        self._serve(b"x" * 300, declared_length=1000)
        with self.assertRaises(mi.DownloadError) as caught:
            mi._download_with_progress("https://example.test/f.dll", self.dest,
                                       lambda _m: None)
        msg = str(caught.exception)
        for part in ("ended early", "Nothing was installed", "300", "1,000"):
            self.assertIn(part, msg)
        self.assertFalse(os.path.exists(self.dest))

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


class ProgressThrottling(unittest.TestCase):
    """Every progress call becomes a Tk event; one per 64 KiB chunk was
    ~8,000 for a 500 MB mod."""

    def setUp(self):
        self.tmp = temp_dir(self, "tavern_progress_")
        stub(self, mi, "_urlopen_hard_timeout", mi._urlopen_hard_timeout)

    def _download(self, resp, **kw):
        mi._urlopen_hard_timeout = lambda *_a, **_k: resp
        calls = []
        mi._download_with_progress("https://example.test/f.dll",
                                   os.path.join(self.tmp, "f.bin"), calls.append, **kw)
        return calls

    def test_reports_once_per_percent(self):
        calls = self._download(FakeResponse(b"x" * 5000), chunk_size=10)
        self.assertEqual(len(calls), 101)      # 0%..100% over 500 chunks
        self.assertIn("100%", calls[-1])

    def test_reports_every_megabyte_without_a_length(self):
        resp = FakeResponse(b"x" * (3 * 1024 * 1024 + 5))
        del resp.headers["Content-Length"]
        self.assertEqual(len(self._download(resp)), 3)


class MidBodyFailures(unittest.TestCase):
    """A connection cut during the body raises out of read(); it must surface
    as DownloadError, the class retries and the manual-install handoff key on."""

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
        self.tmp = temp_dir(self, "tavern_midbody_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        stub(self, mi, "_urlopen_hard_timeout", mi._urlopen_hard_timeout)

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
        stub(self, mi, "_RETRY_DELAY", 0)
        mi._download_with_retries("https://example.test/f.dll", self.dest, lambda _m: None)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(os.path.getsize(self.dest), len(good))


class RefusedResponses(unittest.TestCase):
    """What Content-Length can't catch: no length at all (refused when
    require_length), and a complete, correct-length block page (magic check)."""

    def setUp(self):
        self.tmp = temp_dir(self, "tavern_refuse_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        stub(self, mi, "_urlopen_hard_timeout", mi._urlopen_hard_timeout)

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


class ForceIPv4(unittest.TestCase):
    """_force_ipv4 patches socket.getaddrinfo process-wide, and the Setup
    status checks run it from several threads at once."""

    def test_overlapping_threads_leave_getaddrinfo_restored(self):
        """A in, B in, A out, B out: B stays protected after A leaves, and the
        original is back once both are out."""
        import socket
        import threading
        real = socket.getaddrinfo
        a_in, b_in, a_out, b_check = (threading.Event() for _ in range(4))
        seen = {}

        def a():
            with mi._force_ipv4():
                a_in.set()
                b_in.wait(5)
            a_out.set()

        def b():
            a_in.wait(5)
            with mi._force_ipv4():
                b_in.set()
                a_out.wait(5)
                seen["patched_while_b_inside"] = socket.getaddrinfo is not real

        threads = [threading.Thread(target=a), threading.Thread(target=b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertTrue(seen["patched_while_b_inside"])
        self.assertIs(socket.getaddrinfo, real)

    def test_many_overlapping_callers_restore_the_original(self):
        import socket
        import threading
        real = socket.getaddrinfo
        start = threading.Barrier(8)

        def worker():
            start.wait(5)
            for _ in range(200):
                with mi._force_ipv4():
                    pass
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertIs(socket.getaddrinfo, real)


class RetriedDownloads(unittest.TestCase):
    """Transient failures are retried, and the final error says it was."""

    def setUp(self):
        self.tmp = temp_dir(self, "tavern_retry_")
        self.dest = os.path.join(self.tmp, "payload.bin")
        stub(self, mi, "_RETRY_DELAY", 0)

    def test_a_transient_failure_is_absorbed(self):
        calls = {"n": 0}
        def flaky(url, dest, on_progress, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise mi.DownloadError("connection reset")
            with open(dest, "wb") as f:
                f.write(b"ok")
            return {}
        stub(self, mi, "_download_with_progress", flaky)
        mi._download_with_retries("https://example.test/f.dll", self.dest,
                                  lambda _m: None)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(os.path.getsize(self.dest), 2)

    def test_exhausted_retries_report_the_attempt_count(self):
        def always_down(*_a, **_kw):
            raise mi.DownloadError("connection reset")
        stub(self, mi, "_download_with_progress", always_down)
        with self.assertRaises(mi.DownloadError) as caught:
            mi._download_with_retries("https://example.test/f.dll", self.dest,
                                      lambda _m: None)
        self.assertIn("attempted 3 times", str(caught.exception))

    def test_a_local_failure_is_not_retried(self):
        """Retrying a permissions error or a full disk just triples the wait
        before the same failure. Only DownloadError is the transient kind."""
        calls = {"n": 0}
        def broken_disk(*_a, **_kw):
            calls["n"] += 1
            raise PermissionError("disk says no")
        stub(self, mi, "_download_with_progress", broken_disk)
        with self.assertRaises(PermissionError):
            mi._download_with_retries("https://example.test/f.dll", self.dest,
                                      lambda _m: None)
        self.assertEqual(calls["n"], 1)

    def test_download_error_is_still_a_runtime_error(self):
        """Every existing caller catches RuntimeError; the subclass exists to
        carry extra meaning, not to slip past those handlers."""
        self.assertTrue(issubclass(mi.DownloadError, RuntimeError))


class BlockedExtractions(unittest.TestCase):
    """A silently blocked extractall() during an update leaves the old files,
    which a presence check passes."""

    def setUp(self):
        self.tmp = temp_dir(self, "tavern_zip_")
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
        self.game = temp_dir(self, "tavern_drift_")
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
        # Remote says everything is current, so any non-'current' result below
        # can only have come from the local content check.
        stub(self, mi, "_fetch_remote_fingerprint", lambda *_a, **_k: '"etag-v1"')
        stub(self, mi, "_get_melonloader_latest_tag", lambda *_a, **_k: "v0.7.3")

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
        """No baseline is not damage: it reads 'unrecorded', which Automatic Setup
        repairs, unlike 'unknown'."""
        meta = mi._load_mod_meta(self.game)
        del meta["tavernlib_sha256"]
        del meta["tavernlib_fingerprint"]
        mi._save_mod_meta(self.game, meta)
        self.assertEqual(mi._tavernlib_status(self.game), "unrecorded")

    def test_an_incomplete_record_is_auto_setup_work_but_never_an_alarm(self):
        """'unrecorded' is Automatic Setup work but no alarm; 'unknown' is neither."""
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
    """With ETag/Last-Modified stripped, install and remote check both fall back
    to the full size, so one install converges to 'current'."""

    def setUp(self):
        self.game = temp_dir(self, "tavern_strip_")
        for name in ("_download_with_retries", "_get_latest_release_tag",
                     "_fetch_remote_fingerprint"):
            stub(self, mi, name, getattr(mi, name))

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
        stub(self, urllib.request, "urlopen", fake_urlopen)
        fp = mi._fetch_remote_fingerprint("https://example.test/f.dll")
        self.assertEqual(fp, "8194")


class ReleaseTags(unittest.TestCase):
    """The release tag from a 'latest' alias's redirect: display only, since
    these DLLs carry no usable version; the ETag stays the update check."""

    def setUp(self):
        stub(self, mi, "_get_redirect_location", mi._get_redirect_location)

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
    """The patch answers the same states as the other two, except that a
    vanilla Root.Township.dll with nothing recorded is 'missing'."""

    def setUp(self):
        self.game = temp_dir(self, "tavern_patch_")
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
        stub(self, pt, "_fetch_remote_fingerprint", lambda *_a, **_k: '"etag-v1"')

    def test_baseline_is_current(self):
        self.assertEqual(pt._patch_status(self.exe), "current")
        self.assertFalse(pt._patch_needs_attention(self.exe))

    def test_a_new_release_is_outdated_not_current(self):
        """Applied and intact, but a newer release is published."""
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
        """No fingerprint recorded: 'unrecorded' (repairable, no alarm)."""
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
