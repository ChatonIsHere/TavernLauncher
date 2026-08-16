"""
Applying the community Tavern patch (themoddingtavern.dll -> Root.Township.dll)
-- shared by both apps since either one can be the first to patch a given
game install, and the other should correctly see it as already done.
"""
import os

from tavern_shared.paths import _sha256_file
from tavern_shared.mod_install import (
    _download_with_retries, _load_mod_meta, _save_mod_meta,
    _verify_payload_type,
)

PATCH_DOWNLOAD_URL = "https://github.com/ModdingTavern/TavernDefaults/releases/latest/download/themoddingtavern.dll"


PATCH_SOURCE_FILENAME = "themoddingtavern.dll"


PATCH_TARGET_SUBDIR   = os.path.join("A Township Tale_Data", "Managed")


PATCH_TARGET_FILENAME = "Root.Township.dll"


def _patch_target_path(game_exe):
    """Full path where Root.Township.dll lives in the game's Managed folder."""
    game_dir = os.path.dirname(game_exe)
    return os.path.join(game_dir, PATCH_TARGET_SUBDIR, PATCH_TARGET_FILENAME)


def _patch_is_applied(game_exe):
    """True if the installed Root.Township.dll matches the hash apply_patch
    most recently confirmed writing there, recorded in the per-game-dir meta
    file (see _load_mod_meta).

    Comparing against a local reference copy (the launcher used to bundle
    one in Patch/) broke as soon as apply_patch used its GitHub download:
    the installed file matched what had genuinely just been applied, but not
    a bundled reference that was stale or missing, so this reported "not
    applied" immediately after a successful patch. Hashing what we ourselves
    last confirmed writing is correct regardless of what supplied it.

    The meta file lives in game_dir, not in per-launcher state, so if the
    client launcher already patched a given game install the server launcher
    (or vice versa) still sees it as done, as long as both point at the same
    game folder. No re-patching, no re-flashing."""
    game_dir = os.path.dirname(game_exe)
    dst = _patch_target_path(game_exe)
    recorded = _load_mod_meta(game_dir).get("patch_sha256")
    if not recorded or not os.path.isfile(dst):
        return False
    try:
        return _sha256_file(dst) == recorded
    except OSError:
        return False


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
        _download_with_retries(PATCH_DOWNLOAD_URL, tmp_dest, on_progress,
                               require_length=True)
        _verify_payload_type(tmp_dest, PATCH_DOWNLOAD_URL, "dll")

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
        # _patch_is_applied (any launcher, any time) can recognise it. Only
        # reached once the swap has been read back and verified above — a
        # failed install never gets its hash recorded as the applied one.
        meta = _load_mod_meta(game_dir)
        meta["patch_sha256"] = new_hash
        _save_mod_meta(game_dir, meta)

        return "current" if already_current else "downloaded"
    finally:
        try:
            if os.path.isfile(tmp_dest):
                os.remove(tmp_dest)
        except Exception:
            pass

