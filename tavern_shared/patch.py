"""
Applying the community Tavern patch (themoddingtavern.dll -> Root.Township.dll)
-- shared by both apps since either one can be the first to patch a given
game install, and the other should correctly see it as already done.
"""
import os

from tavern_shared.paths import _sha256_file
from tavern_shared.mod_install import (
    NEEDS_ATTENTION, _download_with_retries, _fetch_remote_fingerprint,
    _file_matches_recorded, _get_latest_release_tag, _load_mod_meta,
    _save_mod_meta, _verify_payload_type,
)

PATCH_DOWNLOAD_URL = "https://github.com/ModdingTavern/TavernDefaults/releases/latest/download/themoddingtavern.dll"


PATCH_SOURCE_FILENAME = "themoddingtavern.dll"


PATCH_TARGET_SUBDIR   = os.path.join("A Township Tale_Data", "Managed")


PATCH_TARGET_FILENAME = "Root.Township.dll"


def _patch_target_path(game_exe):
    """Full path where Root.Township.dll lives in the game's Managed folder."""
    game_dir = os.path.dirname(game_exe)
    return os.path.join(game_dir, PATCH_TARGET_SUBDIR, PATCH_TARGET_FILENAME)


def _patch_status(game_exe):
    """The same six states as _melonloader_status/_tavernlib_status, so the
    Setup window can say "Update available" about the patch instead of a
    binary applied/not — before this, a newly published patch release read
    as "Up to date" forever, because nothing ever compared against GitHub.

    "Applied" means the installed Root.Township.dll matches the hash
    apply_patch most recently confirmed writing there, recorded in the
    per-game-dir meta file (see _load_mod_meta) — never a comparison
    against some local reference copy (the launcher used to bundle one,
    and comparing against it reported "not applied" immediately after a
    successful GitHub download). The meta file lives in game_dir, not in
    per-launcher state, so if the client launcher already patched a given
    game install the server launcher (or vice versa) still sees it as
    done, as long as both point at the same game folder.

    One reading differs from the other two: Root.Township.dll always exists
    in an unmodified game (it's the game's own file being replaced), so
    presence proves nothing. With no recorded hash the file is just the
    vanilla DLL and the answer is 'missing' (never patched), not 'unknown'
    (patched, but by a launcher too old to record what it did). Those need
    to stay apart: 'missing' flashes the Setup alert unconditionally,
    'unknown' never does.

    - missing:    no Root.Township.dll at all, or nothing recorded as applied
    - damaged:    the file no longer matches what apply_patch last confirmed
                  writing — a game update overwrote it, or antivirus ate it
    - outdated:   applied and intact, but GitHub's fingerprint for the
                  published file has moved (the ETag tracks the asset's
                  bytes, so it catches a re-upload a tag comparison misses)
    - unrecorded: applied and intact, but the install record is incomplete —
                  fingerprint or tag recorded by a launcher too old to write
                  them; purely local, repaired by the reinstall Automatic
                  Setup runs for this state
    - unknown:    full record, but GitHub is unreachable to compare against;
                  deliberately not an alarm and not auto-reinstalled
    - current:    applied, intact, and matches what GitHub is serving"""
    game_dir = os.path.dirname(game_exe)
    dst = _patch_target_path(game_exe)
    meta = _load_mod_meta(game_dir)
    if not os.path.isfile(dst) or not meta.get("patch_sha256"):
        return "missing"
    if _file_matches_recorded(dst, meta.get("patch_sha256")) is False:
        return "damaged"
    installed_fp = meta.get("patch_fingerprint")
    if not (installed_fp and meta.get("patch_tag")):
        return "unrecorded"
    try:
        latest_fp = _fetch_remote_fingerprint(PATCH_DOWNLOAD_URL)
    except Exception:
        return "unknown"
    if not latest_fp:
        return "unknown"
    return "current" if latest_fp == installed_fp else "outdated"


def _patch_needs_attention(game_exe):
    """Whether the patch state should flash the main window's Setup alert,
    by the same rule _mods_need_attention applies to MelonLoader/TavernLib:
    missing/outdated/damaged do, and a failed update *check* (unknown) never
    raises a false alarm on its own."""
    return _patch_status(game_exe) in NEEDS_ATTENTION


def apply_patch(game_exe, on_progress=None):
    """Downloads themoddingtavern.dll from GitHub (with retries — no bundled
    fallback; see _install_melonloader for why it was removed) and installs
    it as Root.Township.dll. Skips the write if already up to date.

    Returns "downloaded" or "current".
    Raises DownloadError when the download itself keeps failing (the Setup
    window turns that into manual-install instructions), or RuntimeError on
    a local failure — including a silent Controlled Folder Access block,
    caught by reading the file back and comparing. The recorded hash is only
    ever updated after that read-back confirms the install landed."""
    if on_progress is None:
        on_progress = lambda msg: None

    game_dir = os.path.dirname(game_exe)
    dst = _patch_target_path(game_exe)
    managed_dir = os.path.dirname(dst)
    if not os.path.isdir(managed_dir):
        raise RuntimeError(
            f"Game Managed folder not found:\n{managed_dir}\n\n"
            "Double-check the game exe path at the top of the launcher.")

    tmp_dest = dst + ".download"
    try:
        on_progress("Checking for the latest patch…")
        headers = _download_with_retries(PATCH_DOWNLOAD_URL, tmp_dest, on_progress,
                                         require_length=True)
        _verify_payload_type(tmp_dest, PATCH_DOWNLOAD_URL, "dll")
        fingerprint = headers.get("ETag") or headers.get("Last-Modified") or ""

        new_hash = _sha256_file(tmp_dest)
        already_current = os.path.isfile(dst) and _sha256_file(dst) == new_hash
        if not already_current:
            # Not already exactly what we'd install — swap it in. (If it
            # already matches, skip the write entirely rather than rewriting,
            # and re-triggering AV scanning of, a file that's already
            # correct.)
            os.replace(tmp_dest, dst)  # atomic on Windows — always a full swap, never a partial one
            if not os.path.isfile(dst) or _sha256_file(dst) != new_hash:
                raise RuntimeError(
                    "The file was written without any error, but checking it afterward "
                    "shows it doesn't match what was just installed. This usually means "
                    "something on this PC silently blocked the write — most commonly "
                    "Windows' Controlled Folder Access, or antivirus real-time protection. "
                    "Try adding an exclusion for the game's install folder in Windows "
                    "Security (or your antivirus), or temporarily disabling Controlled "
                    "Folder Access, then try again.")

        # Record what is now confirmed to be sitting at dst, so
        # _patch_status (any launcher, any time) can recognise it. Only
        # reached once the swap has been read back and verified above — a
        # failed install never gets its hash recorded as the applied one.
        # The fingerprint is GitHub's ETag for the published file — it
        # answers _patch_status's "is a newer one out?" question, which the
        # hash of what's on disk cannot (see _install_tavernlib).
        meta = _load_mod_meta(game_dir)
        meta["patch_sha256"] = new_hash
        if fingerprint:
            meta["patch_fingerprint"] = fingerprint
        # Display only (see _get_latest_release_tag) — dropped rather than
        # left stale if the tag can't be read.
        tag = None
        try: tag = _get_latest_release_tag(PATCH_DOWNLOAD_URL)
        except Exception: pass
        if tag:
            meta["patch_tag"] = tag
        else:
            meta.pop("patch_tag", None)
        _save_mod_meta(game_dir, meta)

        return "current" if already_current else "downloaded"
    finally:
        try:
            if os.path.isfile(tmp_dest):
                os.remove(tmp_dest)
        except Exception:
            pass

