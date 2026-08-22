"""
Installing the two mods that are prerequisites rather than choices:
MelonLoader, which has to load before anything else can, and TavernLib, which
is what makes a server's mod support work at all. Shared by both apps, since
both need Install/Update buttons for the same two.

Nothing else belongs here. CircuitsVoiceChat used to, as an "optional mod"
row, and its installer was a second bespoke copy of this file's
download-verify-fallback machinery pinned to one hard-coded GitHub repo. It is
a community mod now, installed through the Mod Manager like any other, which
gets it version pinning, dependency resolution, a sha256 checked against a
reviewed index, and the ability to be turned off per server -- none of which a
hard-coded installer here could offer.
"""
import os
import time
import json
import shutil
import socket
import threading
import tempfile
import zipfile
import zlib
import struct
import contextlib
import urllib.request
import urllib.error
import http.client
from urllib.parse import urlparse

from tavern_shared.paths import _sha256_file
from tavern_shared.bundled import bundled_tag, usable_payload

MELONLOADER_ZIP_URLS = {
    "x64": "https://github.com/LavaGang/MelonLoader/releases/latest/download/MelonLoader.x64.zip",
    "x86": "https://github.com/LavaGang/MelonLoader/releases/latest/download/MelonLoader.x86.zip",
}


TAVERNLIB_DOWNLOAD_URL = "https://github.com/ModdingTavern/TavernLib/releases/latest/download/TavernLib.dll"


TAVERNLIB_FILENAME = "TavernLib.dll"


MODS_META_FILENAME = ".tavern_mods_meta.json"


class DownloadError(RuntimeError):
    """A download that failed for reasons on the network's side of the line:
    couldn't connect, was cut short, stalled, or handed back something that
    isn't the requested file. Distinct from RuntimeError so the Setup window
    can tell "the download itself keeps failing" (offer the manual-install
    route: the user's browser usually works where this process is blocked)
    apart from a local failure like a blocked write, where re-downloading
    would change nothing."""


def _mods_meta_path(game_dir):
    return os.path.join(game_dir, MODS_META_FILENAME)


def _load_mod_meta(game_dir):
    try:
        with open(_mods_meta_path(game_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_mod_meta(game_dir, meta):
    """Replaces the whole record, atomically.

    Written to a temp file in the same folder and swapped in with os.replace
    rather than truncating the real one, because this file is deliberately
    shared: it lives in game_dir so the client launcher and the server
    launcher can see each other's installs (see _patch_status). A plain
    open(w) leaves it empty for as long as the write takes, and anything that
    reads it in that window gets a JSON error -- which _load_mod_meta turns
    into {}, which reads as "nothing is installed", which makes Automatic
    Setup reinstall all three components. The swap has no such window: a
    reader sees either the old file or the new one."""
    path = _mods_meta_path(game_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, path)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass


def _update_mod_meta(game_dir, changes, drop=()):
    """Applies field changes to the record, re-reading it immediately before
    the write.

    The read-modify-write the installers do spans a download and a file swap
    -- seconds at least, minutes for MelonLoader. Two launchers pointed at one
    game folder (installing TavernLib in one while applying the patch in the
    other is the obvious way in) would each write back a copy of the record
    they read at the start, and whoever finished last would erase the other's
    fields entirely. Re-reading here narrows that to the width of one
    os.replace, and the fields the two write never overlap."""
    meta = _load_mod_meta(game_dir)
    meta.update(changes)
    for key in drop:
        meta.pop(key, None)
    _save_mod_meta(game_dir, meta)
    return meta


def _record_source(changes, drop, component, from_bundle):
    """Notes whether this install came from GitHub or from the copy shipped in
    Patch/, so the Setup row can say so. Removed rather than set to "github"
    on the normal path: absent already means "downloaded", and a launcher that
    predates this field would otherwise be indistinguishable from an offline
    install."""
    if from_bundle:
        changes[f"{component}_source"] = "bundled"
    else:
        drop.append(f"{component}_source")


def _get_redirect_location(url, timeout=10):
    """HEAD-requests a URL and returns the Location header of the *first*
    redirect hop, without following it. Used to read a GitHub 'latest
    release' download alias's resolved tag (e.g. 'v0.7.3') straight out of
    the redirect target, without downloading anything."""
    parsed = urlparse(url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    with _force_ipv4():
        conn = conn_cls(parsed.netloc, timeout=timeout)
        try:
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
            conn.request("HEAD", path, headers={"User-Agent": "TavernLauncher/1.0",
                                                 "Host": parsed.netloc})
            resp = conn.getresponse()
            resp.read()
            if 300 <= resp.status < 400:
                return resp.getheader("Location")
            return None
        finally:
            conn.close()


def _get_latest_release_tag(url):
    """Reads the release tag a GitHub 'latest' download alias currently
    resolves to (e.g. 'v1.5.1') out of the first redirect hop — no GitHub
    API call, no rate limit, and no need to download the asset itself.
    Recorded at install time purely so the Setup window can SHOW which
    release is installed; it is never the update check. The DLLs carry no
    usable PE version resource (TavernLib is stamped 1.0.0.0 forever, the
    patch inherits the game's own 0.0.0.1), so the release tag is the only
    human-readable version these components have."""
    loc = _get_redirect_location(url)
    if not loc:
        return None
    # .../releases/download/v0.7.3/MelonLoader.x64.zip -> "v0.7.3"
    parts = loc.rstrip("/").split("/")
    try:
        return parts[parts.index("download") + 1]
    except (ValueError, IndexError):
        return None


def _get_melonloader_latest_tag():
    """The current MelonLoader release tag (e.g. 'v0.7.3'). Unlike the other
    two components this one IS the update check, compared against the
    recorded melonloader_tag."""
    return _get_latest_release_tag(MELONLOADER_ZIP_URLS["x64"])


def _fetch_remote_fingerprint(url, timeout=10):
    """A lightweight 'has this file changed' check — HEAD for ETag (falls
    back to Last-Modified, then Content-Length), without downloading the
    file. This stays the update check for TavernLib and the patch even
    though their repos tag releases these days (the tags are recorded for
    display, see _get_latest_release_tag): the ETag tracks the asset's
    BYTES, so it also catches a release re-uploaded under an unchanged
    tag, which tag comparison reads as 'current' forever."""
    def _read(resp):
        h = resp.headers
        fp = h.get("ETag") or h.get("Last-Modified")
        if fp:
            return fp
        # Size fallback for a proxy that strips ETag/Last-Modified. On the
        # ranged-GET path below, Content-Length describes the 1-byte slice
        # (a constant "1" — useless as a fingerprint), so the full size has
        # to come out of Content-Range ("bytes 0-0/12345" -> "12345"). That
        # keeps every path here returning the same value the install-time
        # GET records as its own fallback (its Content-Length IS the full
        # size), so a fresh install on such a network converges to
        # 'current' instead of mismatching forever.
        content_range = h.get("Content-Range") or ""
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return total
        return h.get("Content-Length") or ""
    with _force_ipv4():
        req = urllib.request.Request(url, method="HEAD",
            headers={"User-Agent": "TavernLauncher/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                fp = _read(resp)
                if fp: return fp
        except Exception:
            pass
        # Fallback for hosts that don't support HEAD on the (often presigned)
        # redirect target: a 1-byte ranged GET still reveals the same headers.
        req = urllib.request.Request(url, headers={
            "User-Agent": "TavernLauncher/1.0", "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read(resp)


def _detect_exe_arch(exe_path):
    """Reads the PE header to tell whether the game exe is 32- or 64-bit,
    so we grab the matching MelonLoader build. Returns 'x64', 'x86', or
    None if it can't be determined (unusual/corrupt file, unknown arch)."""
    try:
        with open(exe_path, "rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset)
            if f.read(4) != b"PE\0\0":
                return None
            machine = struct.unpack("<H", f.read(2))[0]
            return {0x8664: "x64", 0x14c: "x86"}.get(machine)
    except Exception:
        return None


def _melonloader_installed(game_dir):
    return (os.path.isdir(os.path.join(game_dir, "MelonLoader")) and
            os.path.isfile(os.path.join(game_dir, "version.dll")))


def _tavernlib_installed(game_dir):
    return os.path.isfile(os.path.join(game_dir, "Plugins", TAVERNLIB_FILENAME))


@contextlib.contextmanager
def _force_ipv4():
    """Temporarily makes socket.getaddrinfo only return IPv4 results.
    Fixes a common real-world failure: a network where IPv6 is technically
    configured but the actual route is dead/blackholed, so anything that
    tries the (often-preferred) IPv6 address first just hangs instead of
    failing over. Browsers and curl dodge this automatically by racing both
    address families ("happy eyeballs"); plain urllib doesn't, so this
    nudges it into only ever trying IPv4."""
    _orig = socket.getaddrinfo
    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = _orig


def _urlopen_hard_timeout(req, connect_timeout=20, socket_timeout=20):
    """Runs urlopen() in a helper thread so a hung DNS lookup can't block
    forever — urlopen's own timeout= only bounds the socket connect/read
    once a connection attempt actually starts; DNS resolution happens
    before that and isn't covered by it at all. This is very likely what
    "stuck on Downloading MelonLoader, even as admin" actually was for at
    least some users: a permissions fix wouldn't touch a hung DNS lookup.
    If nothing happens within connect_timeout seconds, this gives up and
    raises rather than waiting on it — the abandoned attempt is a daemon
    thread, so it can't keep the app running even if it eventually returns."""
    result = {}
    def _do():
        try:
            result["resp"] = urllib.request.urlopen(req, timeout=socket_timeout)
        except Exception as e:
            result["error"] = e
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(connect_timeout)
    if t.is_alive():
        raise DownloadError(
            f"Connecting to {urlparse(req.full_url).netloc} took too long and was "
            "abandoned. This usually means DNS resolution or the connection itself "
            "is hanging on this machine — often a VPN, a misconfigured router, or "
            "security software silently intercepting it rather than refusing it "
            "outright. Worth trying: disable any active VPN, try a different "
            "network (e.g. a phone hotspot) to confirm, or temporarily disable "
            "antivirus/firewall and retry.")
    if "error" in result:
        raise result["error"]
    return result["resp"]


def _short_read_message(url, downloaded, total):
    """The message for a download that ended before Content-Length said it
    would. Worth being explicit that nothing was installed, because the
    symptom users actually reported for this was the opposite -- an install
    that looked like it completed, over a file that hadn't changed."""
    pct = int(downloaded * 100 / max(1, total))
    return (
        f"The download from {urlparse(url).netloc} ended early — got "
        f"{downloaded:,} of {total:,} bytes ({pct}%).\n\n"
        "The connection was cut part-way through rather than refused outright, "
        "which is usually antivirus, a VPN, or a school/corporate proxy "
        "interrupting the transfer. Nothing was installed and nothing was "
        "changed — the existing files are untouched.\n\n"
        "Worth trying:\n"
        "  • Try again — this often succeeds on a second attempt\n"
        "  • Try a different network (a phone hotspot is a quick test)\n"
        "  • Temporarily disable antivirus/VPN and retry\n"
        "  • Use Manual Install to download it in your browser instead")


def _download_with_progress(url, dest_path, on_progress,
                             connect_timeout=20, max_total_seconds=1800, chunk_size=1<<16,
                             require_length=False):
    """Downloads url to dest_path, reporting live progress and enforcing a
    real wall-clock cap on the whole operation — a plain urlopen timeout=
    only guards a single socket operation, so a connection that trickles
    data just fast enough to dodge that never trips it and looks like a
    permanent hang rather than a slow download. Returns the response
    headers on success (some callers use these, e.g. for an ETag). Raises
    DownloadError with a specific, actionable message on failure, and never
    leaves a partially-downloaded file at dest_path.

    A short read is a failure, not a success. read() returning b"" means
    "no more data is coming", NOT "the file is complete" -- a connection cut
    mid-transfer by a proxy, VPN or antivirus ends the loop exactly the same
    way a finished download does. Without the Content-Length check below,
    every caller then hashes the truncated file, gets a hash that of course
    matches itself, records it as verified, and installs a broken DLL while
    reporting success.

    require_length=True refuses a response with no usable Content-Length at
    all, BEFORE reading the body. The short-read check above is the only
    truncation detection a bare-file download has (a .zip at least fails to
    open; a .dll has no structure of its own to fail on), and it silently
    stops existing the moment the header is missing -- which GitHub never
    omits, but an intercepting proxy rewriting the response to chunked
    encoding does. For a download whose caller can't verify the result
    against an independent hash, no length means no truncation detection,
    so it's treated as a failure rather than downloaded blind."""
    start = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "TavernLauncher/1.0"})
    with _force_ipv4():
        try:
            resp = _urlopen_hard_timeout(req, connect_timeout=connect_timeout)
        except DownloadError:
            raise
        except urllib.error.URLError as e:
            raise DownloadError(
                f"Couldn't connect to {urlparse(url).netloc} — {getattr(e,'reason',e)}\n\n"
                "This is usually a network/firewall/antivirus issue on this machine, "
                "not something wrong with the launcher itself. Worth trying:\n"
                "  • Run the launcher as Administrator\n"
                "  • Temporarily disable antivirus/VPN and retry\n"
                "  • Check whether a firewall is blocking outbound HTTPS for this app")

        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        if require_length and total is None:
            resp.close()
            raise DownloadError(
                f"{urlparse(url).netloc} answered without saying how large the file "
                "is (no Content-Length). GitHub always sends one, so something "
                "between this machine and GitHub — usually a proxy or security "
                "software inspecting the connection — is rewriting the response. "
                "Without the expected size, a cut-off download would be "
                "undetectable, so nothing was downloaded.")
        downloaded = 0
        try:
            with resp, open(dest_path, "wb") as out:
                while True:
                    if time.time() - start > max_total_seconds:
                        raise DownloadError(
                            f"Download stalled for over {max_total_seconds // 60} minutes — giving up. "
                            "The connection may be extremely slow, or something is "
                            "silently throttling it (security software, a captive "
                            "portal, etc.) rather than blocking it outright.")
                    try:
                        chunk = resp.read(chunk_size)
                    except (OSError, http.client.HTTPException) as e:
                        # A connection dropped mid-body surfaces here — reset,
                        # TLS error, socket timeout, a short chunked read — and
                        # every one of those is the network's side of the line,
                        # exactly like a refused connect. It has to be
                        # DownloadError: _download_with_retries retries only
                        # that class, and the Setup window classifies on it for
                        # the manual-install handoff. Only the read is wrapped —
                        # out.write() failing is local (disk, permissions) and
                        # must keep failing immediately.
                        raise DownloadError(
                            f"The connection to {urlparse(url).netloc} was cut "
                            f"part-way through the download after {downloaded:,} "
                            f"bytes ({type(e).__name__}: {e}).\n\n"
                            "This is usually antivirus, a VPN, or a proxy "
                            "interrupting the transfer rather than refusing it. "
                            "Nothing was installed and nothing was changed.\n\n"
                            "Worth trying:\n"
                            "  • Try again — this often succeeds on a second attempt\n"
                            "  • Try a different network (a phone hotspot is a quick test)\n"
                            "  • Temporarily disable antivirus/VPN and retry\n"
                            "  • Use Manual Install to download it in your browser instead")
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / max(1, total))
                        on_progress(f"Downloading… {pct}%  ({downloaded//1024:,} / {total//1024:,} KB)")
                    else:
                        on_progress(f"Downloading… {downloaded//1024:,} KB")
                if total is not None and downloaded != total:
                    raise DownloadError(_short_read_message(url, downloaded, total))
        except Exception:
            try: os.remove(dest_path)
            except Exception: pass
            raise
        return dict(resp.headers)


DOWNLOAD_ATTEMPTS = 3


def _download_with_retries(url, dest_path, on_progress, attempts=DOWNLOAD_ATTEMPTS,
                           retry_delay=2.0, **kwargs):
    """_download_with_progress, tried up to `attempts` times before giving up.
    Only DownloadError is retried — that's the transient class (a dropped
    connection often succeeds on the next try, which is also the first thing
    _short_read_message tells the user to do manually). Anything else
    (permissions, disk) fails immediately: it would fail identically again.
    The exception that escapes after the last attempt says how many were made,
    so the caller's failure UI can honestly present this as "we already
    retried" rather than "try again"."""
    last = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            on_progress(f"Download failed — retrying (attempt {attempt} of {attempts})…")
            time.sleep(retry_delay)
        try:
            return _download_with_progress(url, dest_path, on_progress, **kwargs)
        except DownloadError as e:
            last = e
    raise DownloadError(f"{last}\n\nThis download was attempted {attempts} times "
                        "and failed every time.")


_PAYLOAD_MAGIC = {
    # (magic bytes, floor) — the floor only exists to reject something
    # magic-prefixed but absurdly small, e.g. an error page that happens to
    # start with the right two bytes; every real payload here is far larger.
    "dll": (b"MZ", 4096, "a Windows DLL"),
    "zip": (b"PK", 4096, "a .zip archive"),
}


def _verify_payload_type(path, url, kind):
    """Rejects a download whose CONTENT isn't the kind of file asked for —
    the check that catches an intercepting proxy serving a complete,
    correct-length block/login page instead of the file. Content-Length
    can't catch that (the substituted body matches its own length), and for
    a bare .dll neither can anything downstream: it gets hashed, installed,
    and recorded as good. Two bytes of magic kill the realistic version of
    that scenario, since a block page is HTML, not a PE image or zip.
    Deliberately NOT authentication — a deliberately substituted valid DLL
    still passes; only a pinned hash could catch that, and these downloads
    track a moving 'latest' with nothing published to pin against."""
    magic, floor, described = _PAYLOAD_MAGIC[kind]
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(len(magic))
    except OSError as e:
        raise DownloadError(f"Couldn't read back the downloaded file — {e}")
    if head != magic or size < floor:
        preview = head + (b"" if size <= len(magic) else b"...")
        raise DownloadError(
            f"The download from {urlparse(url).netloc} completed, but the result "
            f"isn't {described} (starts with {preview!r}, {size:,} bytes). "
            "Something between this machine and GitHub answered with a different "
            "file — usually a proxy, captive portal, or security software serving "
            "an error/login page instead of the real download. Nothing was "
            "installed.\n\nWorth trying:\n"
            "  • A different network (a phone hotspot is a quick test)\n"
            "  • Temporarily disable antivirus/VPN and retry\n"
            "  • Use Manual Install to download it in your browser instead")


def _open_zip_with_retry(path, retries=8, delay=1.0):
    """Windows sometimes briefly locks a freshly-downloaded file while
    antivirus real-time protection scans it — and a .zip containing DLLs
    is exactly the kind of file that gets scanned most aggressively. A
    plain zipfile.ZipFile() open can stall or fail unpredictably during
    that window, with no timeout of its own (this is local disk I/O, not
    network, so the download's own timeout doesn't cover it at all). This
    retries a few times with short pauses — up to ~8s total — before
    giving up for real, rather than hanging indefinitely or failing on
    what's usually just a few seconds of transient scanning."""
    last_err = None
    for _ in range(retries):
        try:
            return zipfile.ZipFile(path)
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(delay)
    raise RuntimeError(
        f"Couldn't open the downloaded file — {last_err}\n\n"
        "This can happen if antivirus is still scanning it. Try clicking "
        "Install again, or temporarily disable real-time scanning and retry.")


def _verify_extracted(zf, dest_dir):
    """Reads back every file extractall() was supposed to write and checks it
    against the CRC32 the archive already carries for it. Returns the list of
    entries that are missing or don't match — empty means the extraction
    genuinely landed.

    A presence check can't do this job. Controlled Folder Access and some
    antivirus make extractall() appear to succeed while writing nothing, and
    when MelonLoader is being *updated* rather than installed fresh, the
    previous version's files are still sitting there — so "is version.dll
    present?" passes, the new release tag gets recorded against the old files,
    and every status check from then on reports 'current'. The update silently
    never happened and nothing will ever notice. Comparing content against the
    archive is the only check that distinguishes the two."""
    bad = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        path = os.path.join(dest_dir, info.filename.replace("\\", "/"))
        try:
            crc = 0
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    crc = zlib.crc32(chunk, crc)
        except OSError:
            bad.append(info.filename)
            continue
        if crc != info.CRC:
            bad.append(info.filename)
    return bad


def _blocked_write_message(what, examples):
    """Shared wording for "we wrote it, then read it back, and it isn't what
    we wrote" — the signature of a silently-blocked write rather than a
    failed one."""
    return (
        f"{what} was written without any error, but reading it back afterward "
        f"shows it isn't what was just installed ({examples}).\n\n"
        "This means something on this PC silently blocked the write instead of "
        "refusing it — most commonly Windows' Controlled Folder Access, or "
        "antivirus real-time protection.\n\n"
        "Worth trying:\n"
        "  • Add an exclusion for the game's install folder in Windows Security\n"
        "  • Temporarily turn off Controlled Folder Access, then retry\n"
        "  • Run the launcher as Administrator\n"
        "  • Use Manual Install to place the files yourself")


def _melonloader_install_broken(game_dir):
    """Whether there is no MelonLoader install here worth protecting —
    missing, failing its recorded hash, or recorded incompletely. The same
    three readings _melonloader_status calls 'missing'/'damaged'/'unrecorded',
    but computed without touching the network, because the one caller
    (the bundled fallback) is by definition already offline."""
    if not _melonloader_installed(game_dir):
        return True
    meta = _load_mod_meta(game_dir)
    recorded = meta.get("melonloader_version_sha256")
    if _file_matches_recorded(os.path.join(game_dir, "version.dll"), recorded) is False:
        return True
    return not (meta.get("melonloader_tag") and recorded)


def _install_melonloader(game_dir, arch, on_progress):
    """Downloads and installs the latest official MelonLoader release, with
    retries, falling back to the copy shipped in Patch/ only when that copy
    is verified and would actually help (see tavern_shared.bundled). A
    download that fails with nothing usable to fall back to raises
    DownloadError, which the Setup window turns into manual-install
    instructions — the user's own browser is the fallback of last resort.

    The meta record (release tag + on-disk hash) is written only after the
    extracted files have been read back and verified against the archive's
    CRCs — a failed or blocked install must never update what we claim is
    installed."""
    url = MELONLOADER_ZIP_URLS.get(arch)
    if not url:
        raise RuntimeError(f"Unsupported or unrecognized game architecture ({arch}).")

    tag = None
    try: tag = _get_melonloader_latest_tag()
    except Exception: pass

    tmp_zip = os.path.join(tempfile.gettempdir(), "tavern_melonloader_dl.zip")
    try:
        try:
            _download_with_retries(url, tmp_zip, on_progress, require_length=True)
            # A structurally-valid zip is a strong integrity check in itself (a
            # truncated one loses its end-of-central-directory record and won't
            # open) — but check the magic first anyway, so a substituted HTML page
            # fails with "this isn't a zip, something intercepted the download"
            # rather than eight seconds of open-retries and an antivirus hint.
            _verify_payload_type(tmp_zip, url, "zip")
            source_zip, from_bundle = tmp_zip, False
        except DownloadError:
            # Only the x64 archive is shipped, and usable_payload's hash check
            # means an x86 request can't be served the wrong one by accident.
            source_zip = usable_payload(
                "melonloader", _melonloader_install_broken(game_dir),
                _load_mod_meta(game_dir).get("melonloader_tag")) if arch == "x64" else None
            if not source_zip:
                raise
            from_bundle = True
            tag = bundled_tag("melonloader")
            on_progress(f"Couldn't reach GitHub — installing the {tag} copy shipped with this launcher…")
        on_progress("Extracting MelonLoader…")
        with _open_zip_with_retry(source_zip) as zf:
            zf.extractall(game_dir)
            # Verified against the archive itself, not just checked for presence
            # -- see _verify_extracted for why presence is not enough here.
            on_progress("Verifying extracted files…")
            bad = _verify_extracted(zf, game_dir)
    finally:
        # tmp_zip only — never source_zip, which on the fallback path is the
        # shipped copy itself and has to survive for the next attempt.
        try: os.remove(tmp_zip)
        except Exception: pass

    if bad:
        shown = ", ".join(bad[:3]) + (f", and {len(bad)-3} more" if len(bad) > 3 else "")
        raise RuntimeError(_blocked_write_message(
            "MelonLoader", f"{len(bad)} file(s) wrong or missing: {shown}"))
    if not _melonloader_installed(game_dir):
        raise RuntimeError(_blocked_write_message(
            "MelonLoader", "the expected files aren't in the game folder"))

    changes, drop = {}, []
    if tag:
        changes["melonloader_tag"] = tag
    else:
        # The tag fetch failed, so which release this was is genuinely unknown
        # — drop any old tag rather than leave it describing the previous
        # install. Status then reports 'unknown' instead of a stale 'current'.
        drop.append("melonloader_tag")
    # Which release this is stays the honest answer either way, so unlike
    # TavernLib and the patch this needs no fingerprint sentinel: MelonLoader's
    # update check IS the tag comparison, and a shipped copy that happens to be
    # the current release genuinely is current. The source is recorded only so
    # the Setup row can say where the files came from.
    _record_source(changes, drop, "melonloader", from_bundle)
    # The tag says which release we fetched; this says what is actually on
    # disk right now, which is a different question and the only one that can
    # answer "has something changed these files since we installed them?".
    # version.dll is the shim the game itself loads, so if anything is going
    # to get quarantined, rolled back or overwritten, it's this.
    try:
        changes["melonloader_version_sha256"] = _sha256_file(
            os.path.join(game_dir, "version.dll"))
    except OSError:
        drop.append("melonloader_version_sha256")
    _update_mod_meta(game_dir, changes, drop)


def _tavernlib_install_broken(game_dir):
    """Whether there is no TavernLib install here worth protecting. The
    network-free half of _tavernlib_status, for the same reason as
    _melonloader_install_broken."""
    if not _tavernlib_installed(game_dir):
        return True
    meta = _load_mod_meta(game_dir)
    recorded = meta.get("tavernlib_sha256")
    if _file_matches_recorded(os.path.join(game_dir, "Plugins", TAVERNLIB_FILENAME),
                              recorded) is False:
        return True
    return not (recorded and meta.get("tavernlib_fingerprint")
                and meta.get("tavernlib_tag"))


def _install_tavernlib(game_dir, on_progress):
    """Downloads and installs the latest TavernLib.dll, with retries, falling
    back to the verified copy shipped in Patch/ when the download fails and
    that copy would help (see tavern_shared.bundled). Always swaps the result
    in atomically, so a failed/interrupted attempt can never leave a corrupt
    half-downloaded file in place. The meta record is written only after the
    installed file has been read back and matches what was staged — a failed
    install never updates the recorded hash."""
    plugins_dir = os.path.join(game_dir, "Plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    dest = os.path.join(plugins_dir, TAVERNLIB_FILENAME)
    tmp_dest = dest + ".download"

    try:
        try:
            headers = _download_with_retries(TAVERNLIB_DOWNLOAD_URL, tmp_dest, on_progress,
                                             require_length=True)
            _verify_payload_type(tmp_dest, TAVERNLIB_DOWNLOAD_URL, "dll")
            from_bundle = False
            # Content-Length as the last resort mirrors _fetch_remote_fingerprint
            # exactly: on a network whose proxy strips ETag/Last-Modified, the
            # remote check falls back to the file's size, so the install has to
            # record that same size or the two can never agree — a fresh install
            # would sit at 'unrecorded' (Automatic Setup reinstalling every run),
            # an existing one at a stale fingerprint reading 'outdated' forever.
            # require_length=True above guarantees a Content-Length exists, so
            # something is always recorded and one install always converges.
            fingerprint = (headers.get("ETag") or headers.get("Last-Modified")
                           or headers.get("Content-Length") or "")
        except DownloadError:
            source = usable_payload("tavernlib", _tavernlib_install_broken(game_dir),
                                    _load_mod_meta(game_dir).get("tavernlib_tag"))
            if not source:
                raise
            from_bundle = True
            tag = bundled_tag("tavernlib")
            on_progress(f"Couldn't reach GitHub — installing the {tag} copy shipped with this launcher…")
            # Staged through tmp_dest like a download so the atomic swap and
            # read-back verification below cover both paths identically.
            shutil.copyfile(source, tmp_dest)
            # Deliberately NOT a real fingerprint. GitHub's ETag is what the
            # update check compares against, and we never spoke to GitHub — so
            # this records a value that cannot match one, which means the first
            # check that does reach GitHub reads 'outdated' and pulls the real
            # release. Recording the shipped copy's own size here instead would
            # read 'current' forever against a proxy that strips ETags.
            fingerprint = f"bundled:{tag}"

        # Captured before the replace, since tmp_dest won't exist anymore
        # afterward — os.replace renames it, it doesn't leave a copy behind.
        expected_hash = _sha256_file(tmp_dest)
        os.replace(tmp_dest, dest)  # atomic on Windows — always a full swap, never a partial one
        if not os.path.isfile(dest) or _sha256_file(dest) != expected_hash:
            # A silently-blocked write (Controlled Folder Access is a
            # documented example) can leave os.replace appearing to succeed
            # with the old file — or nothing at all — actually still there.
            # Reading the result back and comparing is the only reliable way
            # to tell a real success apart from that.
            raise RuntimeError(_blocked_write_message(
                "TavernLib.dll", "the installed file doesn't match what was downloaded"))
    finally:
        try:
            if os.path.isfile(tmp_dest):
                os.remove(tmp_dest)
        except Exception:
            pass
    changes, drop = {}, []
    if fingerprint:
        changes["tavernlib_fingerprint"] = fingerprint
    else:
        # No usable header at all (can't happen while require_length holds,
        # but a stale fingerprint would read 'outdated' forever) — drop it,
        # same reasoning as the tag below.
        drop.append("tavernlib_fingerprint")
    # The fingerprint is GitHub's ETag — it answers "is a newer one published?"
    # and nothing else. This is the hash of the file we actually put on disk,
    # which is what answers "is the file we installed still the file that's
    # there?" — a question the ETag cannot address at all.
    changes["tavernlib_sha256"] = expected_hash
    # Display only (see _get_latest_release_tag) — dropped rather than left
    # stale if the tag can't be read, same as the MelonLoader tag. On the
    # fallback path the manifest already knows the tag, and asking GitHub for
    # it would just fail again on the network that sent us here.
    if from_bundle:
        tag = bundled_tag("tavernlib")
    else:
        tag = None
        try: tag = _get_latest_release_tag(TAVERNLIB_DOWNLOAD_URL)
        except Exception: pass
    if tag:
        changes["tavernlib_tag"] = tag
    else:
        drop.append("tavernlib_tag")
    _record_source(changes, drop, "tavernlib", from_bundle)
    _update_mod_meta(game_dir, changes, drop)


def _file_matches_recorded(path, recorded):
    """Whether path still hashes to what we recorded writing there. None when
    there's nothing recorded to compare against, so callers can tell "we
    checked and it's wrong" apart from "we have no baseline" -- these must
    never collapse into one answer, because only the first is a problem."""
    if not recorded:
        return None
    try:
        return _sha256_file(path) == recorded
    except OSError:
        return False


def _melonloader_status(game_dir):
    """Returns 'missing', 'damaged', 'outdated', 'unrecorded', 'unknown', or
    'current'.

    'unrecorded' and 'unknown' are deliberately separate answers to "can't
    compare", because they're fixed from opposite directions. 'unrecorded'
    means the install record itself is incomplete (installed by hand, or by
    a launcher too old to write these fields) — purely local knowledge, and
    a reinstall repairs it, so Automatic Setup treats it as work to do.
    'unknown' means the record is complete but the remote check didn't
    happen (GitHub unreachable) — reinstalling over that would just fail,
    so nothing treats it as work and nothing alarms over it.

    'damaged' is checked before anything to do with versions, and is purely
    local: the version tag we recorded describes the release we fetched, so
    it keeps reporting 'current' regardless of what later happens to the files
    on disk. Antivirus quarantining version.dll, a game update overwriting it,
    or a half-finished extract all leave the tag intact and the install
    broken."""
    if not _melonloader_installed(game_dir):
        return "missing"
    meta = _load_mod_meta(game_dir)
    if _file_matches_recorded(os.path.join(game_dir, "version.dll"),
                              meta.get("melonloader_version_sha256")) is False:
        return "damaged"
    installed_tag = meta.get("melonloader_tag")
    if not installed_tag or not meta.get("melonloader_version_sha256"):
        return "unrecorded"
    try:
        latest = _get_melonloader_latest_tag()
    except Exception:
        return "unknown"
    if not latest:
        return "unknown"
    return "current" if latest == installed_tag else "outdated"


def _tavernlib_status(game_dir):
    """Same six states as _melonloader_status, and 'damaged' matters here for
    the same reason: tavernlib_fingerprint is GitHub's ETag for the published
    file, so it answers "is a newer one out?" and is completely blind to the
    installed copy being truncated, quarantined or replaced. A full record is
    all three of hash (drift detection), fingerprint (update check) and tag
    (version display) — any of them absent is 'unrecorded', one reinstall
    away from all three working."""
    if not _tavernlib_installed(game_dir):
        return "missing"
    meta = _load_mod_meta(game_dir)
    if _file_matches_recorded(os.path.join(game_dir, "Plugins", TAVERNLIB_FILENAME),
                              meta.get("tavernlib_sha256")) is False:
        return "damaged"
    installed_fp = meta.get("tavernlib_fingerprint")
    if not (installed_fp and meta.get("tavernlib_sha256")
            and meta.get("tavernlib_tag")):
        return "unrecorded"
    try:
        latest_fp = _fetch_remote_fingerprint(TAVERNLIB_DOWNLOAD_URL)
    except Exception:
        return "unknown"
    if not latest_fp:
        return "unknown"
    return "current" if latest_fp == installed_fp else "outdated"


NEEDS_ATTENTION = ("missing", "outdated", "damaged")


# What Automatic Setup re-runs: everything alert-worthy, plus 'unrecorded' —
# an incomplete install record is repaired by exactly the reinstall Automatic
# Setup would do, and leaving it means no damage detection, no update checks
# and no version display for that component. 'unknown' stays excluded from
# both: it means the CHECK couldn't run, and reinstalling over an unreachable
# GitHub turns "everything's installed" into guaranteed download failures.
AUTO_SETUP_STATES = NEEDS_ATTENTION + ("unrecorded",)


def _mods_need_attention(game_dir):
    """True if either required mod (MelonLoader, TavernLib) is missing,
    outdated or damaged — the trigger for flashing the main window's Setup
    button (see
    each launcher's _refresh_setup_alert, which also folds in the patch
    check). Network failures during the update checks never trigger a false
    alarm on their own — only a real missing install (a purely local,
    always-reliable check) does that unconditionally.

    CircuitsVoiceChat is deliberately not considered any more. It is a
    community mod now, installed and updated through the Mod Manager like
    any other, so it has no place in the fixed Setup sequence."""
    return (_melonloader_status(game_dir) in NEEDS_ATTENTION or
            _tavernlib_status(game_dir)   in NEEDS_ATTENTION)

