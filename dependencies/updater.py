"""
Shared self-update logic for both TavernLauncher exes.

Checks GitHub Releases (ModdingTavern/TavernLauncher) for a newer version
and, if the user agrees, downloads the new build and installs it.

Release process this expects
-----------------------------
One zip per *app* per release (not one combined zip for both) — this
changed after the combined zip turned out to be ~84MB, meaning every
single-app update was downloading and extracting roughly double what it
actually needed, and a "stuck" report turned out to at least partly be a
plain size problem (see the notes further down). Attach two separate
assets to the release, named:
    TavernLauncher-Client-vX.Y.Z.zip
    TavernLauncher-Server-vX.Y.Z.zip
(matching the release's own tag, vX.Y.Z — bump APP_VERSION in the source
to match before building). Each zip contains just that app's own files,
flat at the top level, no wrapping subfolder needed anymore since the zip
itself is already app-specific:

    TavernLauncher - Client.exe
    Patch/
        <DLL files>
    addons/
        <addon folders>

addons/ is the SAME folder contents in both zips (an addon can matter to
either side, or both) -- whichever one a given install actually has gets
synced, file-by-file, on every update; see _sync_folder below. Nothing
here deletes a file it doesn't recognize, so a third-party addon someone
dropped in themselves (never part of any release) is never touched.

Because the asset filename embeds both the app name and the version, this
can't use the same "latest/download/<fixed-name>" trick end to end — the
filename literally changes every release. Instead:
  1. Resolve the *tag* from the plain "/releases/latest" alias (this one's
     always the same URL — no filename involved, so nothing to look up
     first).
  2. Compute the expected zip filename from that tag and the app folder.
  3. Use the filename-specific "latest/download/<name>" alias to get the
     actual asset URL, and verify it's real (see _asset_exists — GitHub's
     alias redirects blindly even for a name that doesn't exist, so the
     redirect landing somewhere isn't proof by itself).

This only ever does anything when frozen (a real built .exe) — running as
a plain .py script during development is always a no-op, since
sys.executable would just be the Python interpreter, not anything
meaningful to replace.

Self-update strategy (and why it changed)
-------------------------------------------
An earlier version of this had the currently-running process download the
update, then rename its own .old-suffixed self aside and rename a staged
copy into place — the standard "you can rename a running exe on Windows
even though you can't overwrite it" trick. Real-world reports of it
hanging at the file-replace step, even after retry-protecting every
individual file operation against transient locks, suggested something
retries alone couldn't fix: a process that downloads something and then
modifies its own executable is behaviorally indistinguishable from how a
lot of self-updating malware operates, which is exactly the pattern
antivirus/EDR *behavioral* heuristics are built to watch for.

That's still probably worth avoiding, so this keeps the "launch a staged
exe with a flag, let a fresh process do the actual install" approach:
  1. The currently-running app downloads its own release zip straight to
     a temp file on disk and extracts it to a staging directory.
  2. It launches the *staged* exe with a --finish-update flag, and exits.
  3. That fresh process — which made no network requests itself — waits
     briefly for the old process to release its own exe, renames the old
     one aside, renames itself into the final location, copies over
     Patch/, relaunches from the correct path, and exits.

But investigating a further "still stuck" report surfaced a second,
independent, much more mundane problem: the combined zip was ~84MB, and
this module's own download progress callback fired once per 64KB chunk —
over 1,300 separate callback invocations for that one file, each
scheduling a GUI update on the main thread from a background thread. And
extraction showed one single static "Extracting..." message for the
entire duration, with no visible movement until it was completely done.
Tens of megabytes of decompression taking 10-30+ seconds while showing
zero incremental feedback is indistinguishable from "hung" even when
working *perfectly*. Splitting into per-app zips halves the affected
size; throttling download progress and reporting extraction per-file (see
_download_to_file and _do_extract below) means there's now visible,
continuous movement throughout instead of one frozen-looking message.

Both apps call finish_update_if_requested() at the very top of their
entry point, before any GUI initialization — it's a no-op unless this
process was actually launched to finish an update.
"""

import sys
import os
import json
import time
import threading
import subprocess
import zipfile
import shutil
import tempfile
import http.client
import urllib.request
import urllib.error
import contextlib
import socket
from urllib.parse import urlparse, quote

REPO = "ModdingTavern/TavernLauncher"
FINISH_UPDATE_FLAG = "--finish-update"


def _parse_version(tag):
    """'v1.10.2' -> (1, 10, 2). Ignores anything non-numeric in each dot-
    separated chunk so odd/malformed tags don't crash the comparison —
    they just sort as lower rather than raising."""
    tag = (tag or "").strip().lstrip("vV")
    parts = []
    for chunk in tag.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


@contextlib.contextmanager
def _force_ipv4():
    """See the identical helper in att_client.py/att_server.py for the
    full explanation — briefly: works around networks where IPv6 is
    configured but the actual route is dead, which makes plain urllib
    hang instead of falling back the way a browser would."""
    _orig = socket.getaddrinfo
    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = _orig


def _get_redirect_location(url, timeout=10):
    """HEAD-requests a URL and returns the first redirect hop's Location,
    without following it — reads a GitHub 'latest release' alias's
    resolved tag straight out of the redirect, no API call, no rate limit."""
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


def _asset_exists(url, timeout=10):
    """True if url resolves (following redirects) to something real, not a
    404 — needed because GitHub's 'latest/download/<filename>' alias
    redirects to the tag-specific URL *regardless* of whether that filename
    actually exists in the release; the real validation only happens once
    you fetch the resolved URL. Without this check, a mistyped or missing
    asset name would silently look like "an update is available" every
    time, since the first redirect hop alone doesn't prove anything."""
    with _force_ipv4():
        req = urllib.request.Request(url, method="HEAD",
            headers={"User-Agent": "TavernLauncher/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False


def get_latest_tag():
    """Reads the current release tag from the redirect target of the
    plain '/releases/latest' alias — no GitHub API call, no rate limit,
    and unlike the asset-specific alias, this doesn't require already
    knowing a filename (which, for our own releases, contains the version
    number itself — so there's no fixed name to check against latest).
    Returns None on any failure."""
    try:
        loc = _get_redirect_location(f"https://github.com/{REPO}/releases/latest")
        if not loc:
            return None
        return loc.rstrip("/").split("/")[-1]  # .../releases/tag/v1.7.0 -> "v1.7.0"
    except Exception:
        return None


def check_for_update(current_version, app_folder):
    """Returns (latest_tag, zip_download_url) if a newer version is
    published, else None. Never raises — any network issue just means "no
    update found", which is the right behavior for a background startup
    check. app_folder ("Client" or "Server") is now part of the asset
    filename itself, since each app gets its own zip."""
    try:
        tag = get_latest_tag()
        if not tag:
            return None
        if _parse_version(tag) <= _parse_version(current_version):
            return None
        zip_filename = f"TavernLauncher-{app_folder}-{tag}.zip"
        url = f"https://github.com/{REPO}/releases/latest/download/{quote(zip_filename)}"
        if not _asset_exists(url):
            return None
        return tag, url
    except Exception:
        return None


def _urlopen_hard_timeout(req, connect_timeout=20, socket_timeout=20):
    """Same reasoning as the identical helper in the main launcher files:
    urlopen's timeout= doesn't cover DNS resolution, only the socket once a
    connection attempt starts, so a hung lookup can block forever without
    this. Runs the real call in a daemon thread and gives up waiting on it
    (not able to force-cancel it, but abandoning it is enough) if nothing
    happens in time."""
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
        raise RuntimeError(f"Connecting to {urlparse(req.full_url).netloc} took too long.")
    if "error" in result:
        raise result["error"]
    return result["resp"]


def _run_with_timeout(fn, timeout_seconds, what):
    """Runs fn() in a daemon thread and gives up waiting after
    timeout_seconds. A safety net against anything pathological hanging
    indefinitely with zero feedback — every step in the update flow that
    isn't already bounded by its own timeout gets wrapped in this."""
    result = {}
    def _do():
        try:
            result["value"] = fn()
        except Exception as e:
            result["error"] = e
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout_seconds)
    if t.is_alive():
        raise RuntimeError(f"{what} took longer than {timeout_seconds}s — giving up.")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _retry_on_lock(fn, what, retries=8, delay=1.0):
    """Windows sometimes briefly locks a freshly-downloaded or freshly-
    written file while antivirus real-time protection scans it — and a
    fresh .exe is exactly the kind of file that gets scanned most
    aggressively. Retries fn() a few times with short pauses — up to ~8s
    total — before giving up for real, rather than hanging indefinitely."""
    last_err = None
    for _ in range(retries):
        try:
            return fn()
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(delay)
    raise RuntimeError(
        f"Couldn't complete {what} — {last_err}\n\n"
        "This can happen if antivirus is still scanning the file. Try updating "
        "again in a few seconds, or temporarily disable real-time scanning and retry.")


def _open_zip_with_retry(path, retries=8, delay=1.0):
    """Same reasoning as _retry_on_lock, specifically for opening a
    just-downloaded zip file that might still be momentarily locked."""
    last_err = None
    for _ in range(retries):
        try:
            return zipfile.ZipFile(path)
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(delay)
    raise RuntimeError(
        f"Couldn't open the downloaded file — {last_err}\n\n"
        "This can happen if antivirus is still scanning it. Try updating "
        "again in a few seconds, or temporarily disable real-time scanning and retry.")


PARTIAL_UPDATE_MARKER = "partial_update.json"


def _marker_path(_install_dir=None):
    """Deliberately in AppData, not next to the exe. The marker exists to
    report that writing next to the exe was blocked, so writing the report
    itself into that same folder is the one place it is most likely to fail.
    Takes (and ignores) install_dir so the call sites read symmetrically.

    Duplicates tavern_shared.paths._tavern_data_dir rather than importing it:
    this module is stdlib-only on purpose, because finish_update_if_requested
    runs from the staging folder before the rest of the app is in place."""
    base = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    return os.path.join(base, "TheModdingTavern", PARTIAL_UPDATE_MARKER)


def _write_partial_update_marker(install_dir, failed):
    try:
        path = _marker_path(install_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "install_dir": install_dir,
                       "files": failed}, f)
    except Exception:
        pass


def _clear_partial_update_marker(install_dir):
    try:
        os.remove(_marker_path(install_dir))
    except OSError:
        pass


def read_partial_update_marker():
    """What the app calls on startup to find out whether the update it just
    applied actually replaced everything. Returns the marker dict, or None if
    the last update was clean. Clearing it is the caller's job (see
    clear_partial_update_marker) so it survives until it has been shown."""
    try:
        with open(_marker_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_partial_update_marker():
    _clear_partial_update_marker(None)


def _log_update_event(msg):
    """A plain append-only log file, separate from the in-app progress
    label — the whole point of a persistent, on-disk log is that it's
    still readable after the fact even if the app itself became
    unresponsive or was closed partway through, which an in-memory-only
    progress label can never offer."""
    try:
        path = os.path.join(tempfile.gettempdir(), "tavern_update_log.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _download_to_file(url, dest_path, on_progress, max_total_seconds=180):
    """Streams url to dest_path on disk, enforcing a real wall-clock cap
    on the whole operation (a plain urlopen timeout= only covers gaps
    *between* reads, not a single read() call that itself never returns —
    the loop re-checks elapsed time on every chunk specifically to close
    that gap). Progress callbacks are throttled to roughly 10/second by
    wall-clock time rather than firing once per 64KB chunk — for an 80MB+
    file that's the difference between a couple hundred GUI updates and
    well over a thousand queued onto the main thread from a background
    thread, which is easily enough of a backlog to make the UI appear to
    lag far behind (or stop responding to) what's actually happening."""
    start = time.time()
    last_report = 0.0
    req = urllib.request.Request(url, headers={"User-Agent": "TavernLauncher/1.0"})
    with _force_ipv4():
        resp = _urlopen_hard_timeout(req, connect_timeout=20)
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        downloaded = 0
        try:
            with resp, open(dest_path, "wb") as out:
                while True:
                    now = time.time()
                    if now - start > max_total_seconds:
                        raise RuntimeError(f"Download stalled for over {max_total_seconds}s — giving up.")
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if now - last_report >= 0.1:
                        last_report = now
                        if total:
                            on_progress(f"Downloading… {int(downloaded*100/max(1,total))}%  "
                                        f"({downloaded//1024//1024:,} / {total//1024//1024:,} MB)")
                        else:
                            on_progress(f"Downloading… {downloaded//1024:,} KB")
            # A short read is a failure, not a success: read() returning b""
            # means "no more data is coming", not "the file is complete", so a
            # connection cut mid-transfer ends the loop identically to a
            # finished one. This used to report a flat "100%" here without ever
            # comparing the counter to Content-Length, so a truncated download
            # announced itself as complete and then failed later as a corrupt
            # zip -- or, worse, extracted far enough to look like it worked.
            if total is not None and downloaded != total:
                pct = int(downloaded * 100 / max(1, total))
                raise RuntimeError(
                    f"The update download ended early — got {downloaded:,} of "
                    f"{total:,} bytes ({pct}%).\n\n"
                    "The connection was cut part-way through rather than refused "
                    "outright, which is usually antivirus, a VPN, or a proxy "
                    "interrupting the transfer. Nothing was installed — this "
                    "failed before anything was touched, so the current version "
                    "is genuinely unaffected.\n\n"
                    "Try again, or download the release manually from:\n"
                    f"https://github.com/{REPO}/releases/latest")
            if total:
                on_progress(f"Downloading… 100%  ({total//1024//1024:,} / {total//1024//1024:,} MB)")
        except Exception:
            try: os.remove(dest_path)
            except Exception: pass
            raise


def download_and_apply_update(download_url, app_folder, on_progress=None, max_total_seconds=180):
    """Downloads this app's own release zip to disk, extracts it to a
    staging directory, then launches the staged exe with --finish-update
    and exits immediately. See the module docstring for why the actual
    install happens in that fresh process rather than here. Raises
    RuntimeError with a user-facing message on failure; never returns
    normally on success (this process exits itself)."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update only works in a built .exe, not when running from source.")

    def _progress(msg):
        _log_update_event(msg)
        if on_progress:
            on_progress(msg)

    current_exe = sys.executable

    _progress("Downloading update…")
    tmp_zip = os.path.join(tempfile.gettempdir(), f"tavern_update_{app_folder.lower()}.zip")
    _download_to_file(download_url, tmp_zip, _progress, max_total_seconds=max_total_seconds)

    _progress("Extracting…")
    staging_dir = os.path.join(tempfile.gettempdir(), f"tavern_staged_{app_folder.lower()}")
    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        pass
    os.makedirs(staging_dir, exist_ok=True)

    def _do_extract():
        found_exe = None
        with _open_zip_with_retry(tmp_zip) as zf:
            entries = [n for n in zf.namelist() if not n.endswith("/")]
            total = len(entries)
            for i, name in enumerate(entries, start=1):
                dest = os.path.join(staging_dir, name.replace("\\", "/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(zf.read(name))
                _progress(f"Extracting… ({i} of {total}) {os.path.basename(name)}")
                if name.lower().endswith(".exe"):
                    found_exe = dest
        return found_exe

    # This is local disk work with no natural timeout of its own the way
    # the network download has — a generous cap as a safety net against
    # anything pathological, not an expected slow case now that each zip
    # only holds one app's own files.
    staged_exe = _run_with_timeout(_do_extract, 90, "Extracting the update")

    try: os.remove(tmp_zip)
    except Exception: pass

    if not staged_exe:
        raise RuntimeError(f"The downloaded update for {app_folder} didn't contain a .exe file.")

    _progress("Restarting to finish installing…")
    subprocess.Popen([staged_exe, FINISH_UPDATE_FLAG, current_exe], cwd=staging_dir)
    os._exit(0)


def finish_update_if_requested():
    """Call this at the very top of the app's entry point, before any GUI
    init. If this process was launched specifically to finish applying an
    update (see download_and_apply_update above), this handles that and
    never returns — it relaunches the real app from its final location and
    exits this (staging) process either way, success or failure. A no-op,
    returning normally, for an ordinary launch that isn't finishing an
    update."""
    if FINISH_UPDATE_FLAG not in sys.argv:
        return
    idx = sys.argv.index(FINISH_UPDATE_FLAG)
    if idx + 1 >= len(sys.argv):
        return  # malformed invocation somehow — fall through to a normal launch

    old_exe = sys.argv[idx + 1]
    staged_exe = sys.executable
    staging_dir = os.path.dirname(staged_exe)

    _log_update_event(f"finish_update_if_requested: old_exe={old_exe} staged_exe={staged_exe}")

    # os._exit() in the old process should release its own file handle
    # near-instantly — this is generous margin, not an expected wait.
    time.sleep(2)

    backup_path = old_exe + ".old"

    def _remove_old_backup():
        if os.path.exists(backup_path):
            os.remove(backup_path)
    try:
        _retry_on_lock(_remove_old_backup, what="removing the previous update's backup")
    except RuntimeError as e:
        _log_update_event(f"(non-fatal) {e}")

    try:
        def _rename_old_aside():
            if os.path.exists(old_exe):
                os.rename(old_exe, backup_path)
        _retry_on_lock(_rename_old_aside, what="renaming the old executable aside")

        def _move_self_into_place():
            shutil.move(staged_exe, old_exe)
        _retry_on_lock(_move_self_into_place, what="installing the new executable")
    except Exception as e:
        _log_update_event(f"FAILED: {e}")
        # Best-effort rollback so the old exe isn't left missing entirely.
        try:
            if os.path.exists(backup_path) and not os.path.exists(old_exe):
                os.rename(backup_path, old_exe)
        except Exception:
            pass
        sys.exit(1)

    def _files_differ(a, b):
        """True if a and b differ (by content) or either is missing --
        used to skip rewriting files that are already identical, which
        matters for addons/ specifically: this runs on EVERY update even
        when nothing in addons/ actually changed that release, and a
        needless rewrite means needless antivirus re-scanning of every
        single addon file, every single update, forever."""
        try:
            if os.path.getsize(a) != os.path.getsize(b):
                return True
            import hashlib
            def _hash(p):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                return h.hexdigest()
            return _hash(a) != _hash(b)
        except Exception:
            return True  # can't tell -- err toward updating rather than silently skipping

    def _sync_folder(src_dir, dst_dir, label):
        """Copies every file from src_dir into dst_dir (preserving the
        relative structure), skipping any file whose destination copy is
        already identical. Returns the list of "label/rel" paths that did NOT
        end up matching the source, which is the whole point: these used to be
        logged as "(non-fatal)" and then followed by an unconditional "Update
        applied successfully", so a locked or Controlled-Folder-Access-blocked
        Patch/ file meant the launcher updated, restarted, reported success,
        and carried on running against the old file. That is exactly the
        "update seemed to work but the file didn't change" symptom, and from
        the outside it is indistinguishable from the update working.

        Each copy is verified by reading it back, because shutil.copy2 raising
        nothing does not establish that anything was written.

        Deliberately never deletes anything from dst_dir that isn't present in
        src_dir -- a user's own third-party addon sitting in addons/ (never
        part of any official release) must never be touched by this, same
        reasoning as why this has never deleted anything from Patch/ either."""
        failed = []
        if not os.path.isdir(src_dir):
            return failed
        for root, _dirs, files in os.walk(src_dir):
            for fn in files:
                src_file = os.path.join(root, fn)
                rel = os.path.relpath(src_file, src_dir)
                dst_file = os.path.join(dst_dir, rel)
                if os.path.isfile(dst_file) and not _files_differ(src_file, dst_file):
                    continue
                os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                def _copy_one(s=src_file, d=dst_file):
                    shutil.copy2(s, d)
                try:
                    _retry_on_lock(_copy_one, what=f"updating {label}/{rel}")
                except RuntimeError as e:
                    _log_update_event(f"FAILED: {e}")
                    failed.append(f"{label}/{rel}")
                    continue
                if _files_differ(src_file, dst_file):
                    _log_update_event(
                        f"FAILED: {label}/{rel} copied without error but still "
                        "doesn't match the new version — the write was silently "
                        "blocked (Controlled Folder Access / antivirus).")
                    failed.append(f"{label}/{rel}")
        return failed

    install_dir = os.path.dirname(old_exe)

    # Patch/ files ship alongside the new exe the same way a code update
    # does — copy over whatever changed.
    failed = _sync_folder(os.path.join(staging_dir, "Patch"),
                          os.path.join(install_dir, "Patch"), "Patch")

    # addons/ similarly -- an addon author's release gets picked up here
    # automatically on the next update, same as any other code change,
    # without the user ever needing to manually re-download anything.
    # Requires the release zip to actually include an addons/ folder
    # alongside the .exe and Patch/ -- see BUILD_EXECUTABLES.bat / the
    # release packaging notes for what each release zip should contain.
    failed += _sync_folder(os.path.join(staging_dir, "addons"),
                           os.path.join(install_dir, "addons"), "addons")

    if failed:
        # The exe itself did update (we got past _move_self_into_place), so
        # this is a partial update, not a failed one -- saying "successfully"
        # here would be a lie, and saying "failed" would be a different one.
        # The marker file is what turns this into something the user actually
        # sees: nothing else in this process has a UI to report through, and
        # this log is not somewhere anyone looks unprompted.
        _log_update_event(f"PARTIAL UPDATE — {len(failed)} file(s) not replaced: "
                          + ", ".join(failed))
        _write_partial_update_marker(install_dir, failed)
    else:
        _clear_partial_update_marker(install_dir)
        _log_update_event("Update applied successfully, relaunching.")
    try:
        subprocess.Popen([old_exe])
    except Exception as e:
        _log_update_event(f"Couldn't relaunch: {e}")
    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        pass
    sys.exit(0)


def cleanup_previous_update():
    """Call once at startup, every launch. Removes the renamed-aside old
    exe from a previous update now that this (new) process isn't the one
    holding it open, plus any leftover staging directory or partial
    download from an update that finished (or failed) before it got a
    chance to clean up after itself. Safe no-op if there's nothing to
    clean up, and just skips quietly if something's somehow still locked
    — it'll get another chance next launch."""
    if not getattr(sys, "frozen", False):
        return
    old_path = sys.executable + ".old"
    if os.path.exists(old_path):
        try:
            os.remove(old_path)
        except Exception:
            pass
    for app_folder in ("client", "server"):
        staging_dir = os.path.join(tempfile.gettempdir(), f"tavern_staged_{app_folder}")
        if os.path.exists(staging_dir):
            try:
                shutil.rmtree(staging_dir, ignore_errors=True)
            except Exception:
                pass
        partial_zip = os.path.join(tempfile.gettempdir(), f"tavern_update_{app_folder}.zip")
        if os.path.exists(partial_zip):
            try:
                os.remove(partial_zip)
            except Exception:
                pass
