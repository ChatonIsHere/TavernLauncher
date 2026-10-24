"""Installing the two prerequisite mods both apps' Setup windows manage:
MelonLoader, which has to load before anything else, and TavernLib, which
makes a server's mod support work. Community mods go through
tavern_shared.mods."""
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
    """A download that failed on the network's side: refused, cut short,
    stalled, or not the requested file. Setup offers the manual-install route
    for these; a local failure such as a blocked write gets no such offer."""


def _mods_meta_path(game_dir):
    return os.path.join(game_dir, MODS_META_FILENAME)


def _load_mod_meta(game_dir):
    try:
        with open(_mods_meta_path(game_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_mod_meta(game_dir, meta):
    """Replaces the record atomically (temp file, then os.replace). Both
    launchers read this file, and one catching it half-written would see {}:
    "nothing installed", and Automatic Setup would reinstall everything."""
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
    """Applies field changes to a freshly re-read record, so two launchers
    installing different components into one game folder don't erase each
    other's fields."""
    meta = _load_mod_meta(game_dir)
    meta.update(changes)
    for key in drop:
        meta.pop(key, None)
    _save_mod_meta(game_dir, meta)
    return meta


def _record_source(changes, drop, component, from_bundle):
    """Records "<component>_source": "bundled" for an offline install and drops
    it otherwise; absent means downloaded."""
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
    """The release tag a GitHub 'latest' download alias redirects to (e.g.
    'v1.5.1'), read from the first hop without the API or the asset. Display
    only for TavernLib and the patch, whose DLLs carry no usable version."""
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
    """The current MelonLoader release tag; for MelonLoader the tag IS the
    update check."""
    return _get_latest_release_tag(MELONLOADER_ZIP_URLS["x64"])


def _fetch_remote_fingerprint(url, timeout=10):
    """'Has this file changed?' without downloading it: the ETag, else
    Last-Modified, else the full size. The update check for TavernLib and the
    patch, since an ETag also catches a re-upload under an unchanged tag."""
    def _read(resp):
        h = resp.headers
        fp = h.get("ETag") or h.get("Last-Modified")
        if fp:
            return fp
        # Size fallback for a proxy stripping both. The ranged GET's
        # Content-Length is its 1-byte slice, so the full size comes from
        # Content-Range: the same value an install records.
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


_ipv4_lock = threading.Lock()
_ipv4_users = 0
_ipv4_saved = None


@contextlib.contextmanager
def _force_ipv4():
    """Makes socket.getaddrinfo return only IPv4 results while any caller is
    inside. Fixes networks where IPv6 is configured but its route is dead:
    plain urllib tries the preferred IPv6 address and hangs instead of
    failing over, where browsers race both families.

    The patch is process-wide (the lookup can run on _urlopen_hard_timeout's
    helper thread), so it is reference-counted under a lock: installed by the
    first caller in, removed by the last one out, however threads overlap."""
    global _ipv4_users, _ipv4_saved
    with _ipv4_lock:
        if _ipv4_users == 0:
            _ipv4_saved = orig = socket.getaddrinfo

            def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
                return orig(host, port, socket.AF_INET, type, proto, flags)
            socket.getaddrinfo = _ipv4_only
        _ipv4_users += 1
    try:
        yield
    finally:
        with _ipv4_lock:
            _ipv4_users -= 1
            if _ipv4_users == 0:
                socket.getaddrinfo, _ipv4_saved = _ipv4_saved, None


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


_TIP_RETRY = "Try again — this often succeeds on a second attempt"
_TIP_NETWORK = "Try a different network (a phone hotspot is a quick test)"
_TIP_AV = "Temporarily disable antivirus/VPN and retry"
_TIP_ADMIN = "Run the launcher as Administrator"
_TIP_MANUAL = "Use Manual Install to download it in your browser instead"


def _worth_trying(*tips):
    """The bulleted "Worth trying:" block the download failures end with."""
    return "Worth trying:\n" + "\n".join(f"  • {tip}" for tip in tips)


def _short_read_message(url, downloaded, total):
    """The message for a download that ended before its Content-Length."""
    pct = int(downloaded * 100 / max(1, total))
    return (
        f"The download from {urlparse(url).netloc} ended early — got "
        f"{downloaded:,} of {total:,} bytes ({pct}%).\n\n"
        "The connection was cut part-way through rather than refused outright, "
        "which is usually antivirus, a VPN, or a school/corporate proxy "
        "interrupting the transfer. Nothing was installed and nothing was "
        "changed — the existing files are untouched.\n\n"
        + _worth_trying(_TIP_RETRY, _TIP_NETWORK, _TIP_AV, _TIP_MANUAL))


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

    Fewer bytes than Content-Length is a failure: a cut connection ends the
    read loop exactly like a finished download, and the truncated file would
    hash consistently and install as verified. require_length=True refuses a
    response with no Content-Length before reading the body, since truncation
    is then undetectable; callers with no independent hash ask for it."""
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
                "not something wrong with the launcher itself. "
                + _worth_trying(_TIP_ADMIN, _TIP_AV,
                                "Check whether a firewall is blocking outbound HTTPS for this app"))

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
        # Each call is a Tk event on the caller's side: report when the whole
        # percentage moves, or every ~1 MB when there's no length to divide by.
        last_pct, last_reported = -1, 0
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
                            + _worth_trying(_TIP_RETRY, _TIP_NETWORK, _TIP_AV, _TIP_MANUAL))
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / max(1, total))
                        if pct != last_pct:
                            last_pct = pct
                            on_progress(f"Downloading… {pct}%  ({downloaded//1024:,} / {total//1024:,} KB)")
                    elif downloaded - last_reported >= 1 << 20:
                        last_reported = downloaded
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
    """_download_with_progress, tried up to `attempts` times. Only DownloadError
    (the transient kind) is retried; the final error says how many attempts
    were made, so Setup can say it already retried."""
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
    # (magic bytes, size floor, description); the floor rejects a tiny error
    # body that happens to start with the right bytes.
    "dll": (b"MZ", 4096, "a Windows DLL"),
    "zip": (b"PK", 4096, "a .zip archive"),
}


def _verify_payload_type(path, url, kind):
    """Raises DownloadError unless the file starts with the magic bytes for
    `kind` and clears its size floor. Catches a proxy serving a complete,
    correct-length block page, which no length check can. Not authentication:
    these track a moving 'latest' with no published hash to pin."""
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
            "installed.\n\n" + _worth_trying(_TIP_NETWORK, _TIP_AV, _TIP_MANUAL))


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
    """The archive members that are missing or fail their CRC on disk; empty
    when the extraction landed. Presence isn't enough: a silently blocked
    extractall() during an update leaves the old files in place."""
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
    """Wording for a write that succeeded but reads back wrong: a silently
    blocked write, not a failed one."""
    return (
        f"{what} was written without any error, but reading it back afterward "
        f"shows it isn't what was just installed ({examples}).\n\n"
        "This means something on this PC silently blocked the write instead of "
        "refusing it — most commonly Windows' Controlled Folder Access, or "
        "antivirus real-time protection.\n\n"
        + _worth_trying("Add an exclusion for the game's install folder in Windows Security",
                        "Temporarily turn off Controlled Folder Access, then retry",
                        _TIP_ADMIN, "Use Manual Install to place the files yourself"))


def _install_melonloader(game_dir, arch, on_progress):
    """Installs the latest MelonLoader release for `arch`, falling back to the
    verified copy in Patch/ when usable_payload allows, and raising
    DownloadError when nothing usable is shipped (Setup offers manual install).
    The meta record is written only after the extracted files pass their CRC
    check."""
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
            # Magic first, so a substituted page fails as such rather than
            # as eight seconds of zip-open retries.
            _verify_payload_type(tmp_zip, url, "zip")
            source_zip, from_bundle = tmp_zip, False
        except DownloadError:
            # Only the x64 archive is shipped.
            source_zip = usable_payload(
                "melonloader", _melonloader_local_state(game_dir)[1] is not None,
                _load_mod_meta(game_dir).get("melonloader_tag")) if arch == "x64" else None
            if not source_zip:
                raise
            from_bundle = True
            tag = bundled_tag("melonloader")
            on_progress(f"Couldn't reach GitHub — installing the {tag} copy shipped with this launcher…")
        on_progress("Extracting MelonLoader…")
        with _open_zip_with_retry(source_zip) as zf:
            zf.extractall(game_dir)
            on_progress("Verifying extracted files…")
            bad = _verify_extracted(zf, game_dir)
    finally:
        # Never source_zip: on the fallback path it's the shipped copy.
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
        # Unknown release: drop the old tag rather than claim it.
        drop.append("melonloader_tag")
    # No bundled:<tag> sentinel needed: MelonLoader's update check is the tag
    # itself, and a shipped copy of the current release is current.
    _record_source(changes, drop, "melonloader", from_bundle)
    # version.dll is the shim the game loads, so it's what damage shows in.
    try:
        changes["melonloader_version_sha256"] = _sha256_file(
            os.path.join(game_dir, "version.dll"))
    except OSError:
        drop.append("melonloader_version_sha256")
    _update_mod_meta(game_dir, changes, drop)


def _install_dll_component(game_dir, component, url, dest, what, on_progress,
                           install_broken, skip_if_current=False):
    """The pipeline behind TavernLib and the patch, recorded under
    `component`_* keys in the game dir's meta file.

    Downloads with retries (Content-Length required: a bare DLL has no
    structure of its own to fail on when truncated) and checks it really is
    a DLL, recording GitHub's ETag as the update fingerprint. If the download
    keeps failing, the verified copy in Patch/ stands in when usable_payload
    allows (install_broken() is the caller's network-free reading), recorded
    with a "bundled:<tag>" fingerprint no real ETag can equal, so the first
    check that reaches GitHub reads 'outdated' and pulls the real release.
    Either way the file is staged beside dest, swapped in atomically and read
    back before anything is recorded. With skip_if_current, a dest already
    identical isn't rewritten (or rescanned by antivirus).

    Returns False if dest was already current, True if it was written.
    Raises DownloadError when nothing usable is shipped (Setup offers manual
    install), RuntimeError on a silently blocked write."""
    tmp_dest = dest + ".download"
    try:
        try:
            headers = _download_with_retries(url, tmp_dest, on_progress, require_length=True)
            _verify_payload_type(tmp_dest, url, "dll")
            from_bundle = False
            # Content-Length last, the same fallback _fetch_remote_fingerprint
            # uses, or a proxy stripping ETags means the two never agree.
            fingerprint = (headers.get("ETag") or headers.get("Last-Modified")
                           or headers.get("Content-Length") or "")
        except DownloadError:
            source = usable_payload(component, install_broken(),
                                    _load_mod_meta(game_dir).get(f"{component}_tag"))
            if not source:
                raise
            from_bundle = True
            on_progress(f"Couldn't reach GitHub — installing the {bundled_tag(component)} "
                        f"copy of {what} shipped with this launcher…")
            shutil.copyfile(source, tmp_dest)
            fingerprint = f"bundled:{bundled_tag(component)}"

        new_hash = _sha256_file(tmp_dest)
        current = skip_if_current and os.path.isfile(dest) and _sha256_file(dest) == new_hash
        if not current:
            os.replace(tmp_dest, dest)
            if not os.path.isfile(dest) or _sha256_file(dest) != new_hash:
                raise RuntimeError(_blocked_write_message(
                    what, "the installed file doesn't match what was downloaded"))
    finally:
        try:
            if os.path.isfile(tmp_dest):
                os.remove(tmp_dest)
        except Exception:
            pass

    if from_bundle:
        tag = bundled_tag(component)
    else:
        tag = None
        try: tag = _get_latest_release_tag(url)
        except Exception: pass
    changes, drop = {f"{component}_sha256": new_hash}, []
    for key, value in ((f"{component}_fingerprint", fingerprint), (f"{component}_tag", tag)):
        if value:
            changes[key] = value
        else:
            drop.append(key)    # dropped rather than left describing the last install
    _record_source(changes, drop, component, from_bundle)
    _update_mod_meta(game_dir, changes, drop)
    return not current


def _install_tavernlib(game_dir, on_progress):
    """Installs the latest TavernLib.dll into Plugins/ (_install_dll_component)."""
    plugins_dir = os.path.join(game_dir, "Plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    _install_dll_component(
        game_dir, "tavernlib", TAVERNLIB_DOWNLOAD_URL,
        os.path.join(plugins_dir, TAVERNLIB_FILENAME), TAVERNLIB_FILENAME, on_progress,
        lambda: _tavernlib_local_state(game_dir)[1] is not None)


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


def _local_state(installed, path, meta, sha_key, record_keys):
    """The network-free half of a Setup component's status: 'missing',
    'damaged' (path no longer hashes to meta[sha_key]), 'unrecorded' (one of
    record_keys absent, e.g. installed by hand or by an older launcher), or
    None for an intact install with a complete record. 'damaged' comes before
    anything about versions: a recorded tag or ETag describes the release
    fetched, not the files on disk now. The bundled fallback asks this
    offline to decide whether there is an install worth protecting."""
    if not installed:
        return "missing"
    if _file_matches_recorded(path, meta.get(sha_key)) is False:
        return "damaged"
    if not all(meta.get(k) for k in record_keys):
        return "unrecorded"
    return None


def _remote_state(latest, recorded):
    """'current'/'outdated' comparing `recorded` with what latest() reports
    is published, or 'unknown' when that can't be read. 'unknown' is never
    work for Automatic Setup: reinstalling over an unreachable GitHub fails."""
    try:
        published = latest()
    except Exception:
        return "unknown"
    if not published:
        return "unknown"
    return "current" if published == recorded else "outdated"


def _melonloader_local_state(game_dir):
    meta = _load_mod_meta(game_dir)
    return meta, _local_state(
        _melonloader_installed(game_dir), os.path.join(game_dir, "version.dll"), meta,
        "melonloader_version_sha256", ("melonloader_tag", "melonloader_version_sha256"))


def _tavernlib_local_state(game_dir):
    meta = _load_mod_meta(game_dir)
    return meta, _local_state(
        _tavernlib_installed(game_dir), os.path.join(game_dir, "Plugins", TAVERNLIB_FILENAME),
        meta, "tavernlib_sha256",
        ("tavernlib_sha256", "tavernlib_fingerprint", "tavernlib_tag"))


def _melonloader_status(game_dir):
    """'missing', 'damaged', 'unrecorded', then the release tag against
    GitHub's latest: 'current', 'outdated' or 'unknown'."""
    meta, state = _melonloader_local_state(game_dir)
    return state or _remote_state(_get_melonloader_latest_tag, meta["melonloader_tag"])


def _tavernlib_status(game_dir):
    """Same states as _melonloader_status, compared by the published file's
    ETag fingerprint rather than its tag (the fingerprint also catches a
    re-upload under an unchanged tag)."""
    meta, state = _tavernlib_local_state(game_dir)
    return state or _remote_state(
        lambda: _fetch_remote_fingerprint(TAVERNLIB_DOWNLOAD_URL), meta["tavernlib_fingerprint"])


NEEDS_ATTENTION = ("missing", "outdated", "damaged")


# What Automatic Setup re-runs: everything alert-worthy plus 'unrecorded',
# which a reinstall repairs. Never 'unknown': the check couldn't run, and
# reinstalling over an unreachable GitHub only fails.
AUTO_SETUP_STATES = NEEDS_ATTENTION + ("unrecorded",)


def _mods_need_attention(game_dir):
    """True if MelonLoader or TavernLib is missing, outdated or damaged: what
    flashes the Setup button (each launcher also folds in the patch). A
    failed update check never raises the alarm on its own."""
    return (_melonloader_status(game_dir) in NEEDS_ATTENTION or
            _tavernlib_status(game_dir)   in NEEDS_ATTENTION)

