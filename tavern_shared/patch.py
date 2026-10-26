"""
Applying the community Tavern patch (themoddingtavern.dll -> Root.Township.dll)
-- shared by both apps since either one can be the first to patch a given
game install, and the other should correctly see it as already done.
"""
import os

from tavern_shared.mod_install import (
    NEEDS_ATTENTION, _fetch_remote_fingerprint, _install_dll_component,
    _load_mod_meta, _local_state, _remote_state,
)

PATCH_DOWNLOAD_URL = "https://github.com/ModdingTavern/TavernDefaults/releases/latest/download/themoddingtavern.dll"


PATCH_SOURCE_FILENAME = "themoddingtavern.dll"


PATCH_TARGET_SUBDIR   = os.path.join("A Township Tale_Data", "Managed")


PATCH_TARGET_FILENAME = "Root.Township.dll"


def _patch_target_path(game_exe):
    """Full path where Root.Township.dll lives in the game's Managed folder."""
    game_dir = os.path.dirname(game_exe)
    return os.path.join(game_dir, PATCH_TARGET_SUBDIR, PATCH_TARGET_FILENAME)


def _patch_local_state(game_exe):
    """Root.Township.dll exists in every game, so with no recorded hash it's
    the vanilla DLL: 'missing', which alerts, not 'unrecorded'."""
    meta = _load_mod_meta(os.path.dirname(game_exe))
    dst = _patch_target_path(game_exe)
    return meta, _local_state(
        os.path.isfile(dst) and bool(meta.get("patch_sha256")), dst, meta,
        "patch_sha256", ("patch_fingerprint", "patch_tag"))


def _patch_status(game_exe):
    meta, state = _patch_local_state(game_exe)
    return state or _remote_state(
        lambda: _fetch_remote_fingerprint(PATCH_DOWNLOAD_URL), meta["patch_fingerprint"])


def _patch_needs_attention(game_exe):
    return _patch_status(game_exe) in NEEDS_ATTENTION


def apply_patch(game_exe, on_progress=None):
    """Returns "downloaded" or "current"."""
    if on_progress is None:
        on_progress = lambda msg: None

    dst = _patch_target_path(game_exe)
    managed_dir = os.path.dirname(dst)
    if not os.path.isdir(managed_dir):
        raise RuntimeError(
            f"Game Managed folder not found:\n{managed_dir}\n\n"
            "Double-check the game exe path at the top of the launcher.")

    on_progress("Checking for the latest patch…")
    written = _install_dll_component(
        os.path.dirname(game_exe), "patch", PATCH_DOWNLOAD_URL, dst, PATCH_TARGET_FILENAME,
        on_progress, lambda: _patch_local_state(game_exe)[1] is not None,
        skip_if_current=True)
    return "downloaded" if written else "current"
