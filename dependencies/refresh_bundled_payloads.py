"""Re-cuts Patch/ from the current published release of each Setup payload,
and rewrites Patch/bundled.json to describe exactly what it fetched.

Run at build time (BUILD_EXECUTABLES.bat does), because a bundled payload is
only worth shipping while it's current. The last time these files were
committed by hand they drifted: the copy of themoddingtavern.dll in the repo
was several hundred bytes off the published release, so every offline install
would have applied a patch that was already superseded -- and nothing would
have said so.

Fetches through the launcher's own _download_with_retries /
_get_latest_release_tag / _verify_payload_type, deliberately: what ships is
then fetched by the same code, with the same length and payload-type checks,
that an end user's install would use.

Patch/TavernModels.dll is left alone. It has no release URL to fetch from
(it's the plugin half of the custom_models addon) and nothing installs it, so
it's a shipped file rather than a fallback -- see tavern_shared/bundled.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tavern_shared.bundled import BUNDLED_MANIFEST_NAME, bundled_dir
from tavern_shared.paths import _sha256_file
from tavern_shared.mod_install import (
    MELONLOADER_ZIP_URLS, TAVERNLIB_DOWNLOAD_URL,
    _download_with_retries, _get_latest_release_tag, _verify_payload_type,
)
from tavern_shared.patch import PATCH_DOWNLOAD_URL

# (manifest key, url, filename in Patch/, payload kind, extra manifest fields)
TARGETS = [
    ("melonloader", MELONLOADER_ZIP_URLS["x64"], "MelonLoader.x64.zip", "zip", {"arch": "x64"}),
    ("tavernlib",   TAVERNLIB_DOWNLOAD_URL,      "TavernLib.dll",        "dll", {}),
    ("patch",       PATCH_DOWNLOAD_URL,          "themoddingtavern.dll", "dll", {}),
]


def _quiet(message):
    """Swallows the per-chunk progress lines; a build log doesn't want 300
    percentage updates, and the failures all raise rather than report."""


def refresh():
    dest_dir = bundled_dir()
    os.makedirs(dest_dir, exist_ok=True)
    manifest = {}
    for component, url, filename, kind, extra in TARGETS:
        dest = os.path.join(dest_dir, filename)
        tag = _get_latest_release_tag(url)
        if not tag:
            # Without a tag the payload can't be ordered against an installed
            # version, so usable_payload would refuse to ever use it. Shipping
            # it anyway would be dead weight that looks like a fallback.
            raise RuntimeError(f"Couldn't resolve the release tag for {component} from {url}")
        _download_with_retries(url, dest, _quiet, require_length=True)
        _verify_payload_type(dest, url, kind)
        manifest[component] = dict(
            filename=filename, tag=tag, sha256=_sha256_file(dest),
            size=os.path.getsize(dest), **extra)
        print(f"  {component:12} {tag:10} {os.path.getsize(dest):>12,} bytes")

    with open(os.path.join(dest_dir, BUNDLED_MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return manifest


if __name__ == "__main__":
    try:
        refresh()
    except Exception as e:
        # Non-fatal by design: a build on a machine that can't reach GitHub
        # should still produce working executables, shipping whatever Patch/
        # already holds. Those payloads are verified against the manifest that
        # describes them, so an untouched Patch/ stays internally consistent --
        # it just means the offline fallback is as old as the last successful
        # refresh. The caller decides what to do with the exit code.
        print(f"  [WARN] Couldn't refresh the bundled payloads: {e}")
        print("  [WARN] Building with whatever Patch/ already contains.")
        sys.exit(1)
