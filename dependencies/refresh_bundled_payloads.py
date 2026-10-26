"""Re-cuts Patch/ and Patch/bundled.json from each Setup payload's current
release, through the launcher's own download checks. Run by
BUILD_EXECUTABLES.bat."""
import json
import os
import sys
import tempfile

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


def refresh():
    """Swaps nothing in until every download has succeeded, so a failure
    leaves Patch/ matching its bundled.json."""
    dest_dir = bundled_dir()
    os.makedirs(dest_dir, exist_ok=True)
    manifest, staged = {}, []
    try:
        for component, url, filename, kind, extra in TARGETS:
            tag = _get_latest_release_tag(url)
            if not tag:
                # usable_payload could never use an untagged payload.
                raise RuntimeError(f"Couldn't resolve the release tag for {component} from {url}")
            fd, tmp = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=dest_dir)
            os.close(fd)
            staged.append((tmp, os.path.join(dest_dir, filename)))
            _download_with_retries(url, tmp, lambda _m: None)
            _verify_payload_type(tmp, url, kind)
            manifest[component] = dict(
                filename=filename, tag=tag, sha256=_sha256_file(tmp),
                size=os.path.getsize(tmp), **extra)

        fd, manifest_tmp = tempfile.mkstemp(prefix=".bundled.", suffix=".part", dir=dest_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        staged.append((manifest_tmp, os.path.join(dest_dir, BUNDLED_MANIFEST_NAME)))
        for tmp, dest in staged:
            os.replace(tmp, dest)
    finally:
        for tmp, _dest in staged:
            if os.path.exists(tmp):
                os.remove(tmp)

    for component, entry in manifest.items():
        print(f"  {component:12} {entry['tag']:10} {entry['size']:>12,} bytes")
    return manifest


if __name__ == "__main__":
    try:
        refresh()
    except Exception as e:
        print(f"  [WARN] Couldn't refresh the bundled payloads: {e}")
        print("  [WARN] Building with whatever Patch/ already contains.")
        sys.exit(1)
