"""The offline copies of the Setup payloads shipped in Patch/, and the rule
deciding when one may stand in for a download that won't complete.

The history here matters, because this is the second attempt. The original
fallback took whatever file happened to be sitting in Patch/ the moment a
download failed, installed it, and reported plain success -- so a stale
loader went in silently, and the recorded version described a release that
had never been fetched. That was removed rather than fixed.

What makes this version safe is three rules it did not have:

  * Nothing is used unverified. Every payload's sha256 and byte count are
    recorded in Patch/bundled.json at the moment it was fetched, and a copy
    that doesn't match is treated as absent -- a half-synced update or a
    file antivirus has been at is not a fallback, it's a different file.

  * Nothing is used blindly. A shipped copy is a snapshot from whenever the
    build was cut, so it stands in only when it would actually leave the
    install better off: when there is no working install to protect, or
    when the snapshot is genuinely newer than what's already there. It
    never downgrades a working install just because GitHub was unreachable
    for a moment, and when it has nothing to offer the caller raises and
    the Setup window opens manual-install instructions instead.

  * Nothing is reported as something it isn't. The real release tag travels
    with the payload, so an offline install records the version it actually
    installed and says on the Setup row that it came from the shipped copy.
    See _install_tavernlib for the "bundled:<tag>" fingerprint that makes
    the next online check pull the real thing.

Patch/TavernModels.dll is deliberately not listed in the manifest: nothing
installs it (it's the plugin half of the custom_models addon, placed by
hand), so it is a shipped file rather than a fallback for anything.
"""
import json
import os
import sys

from tavern_shared.paths import _sha256_file

BUNDLED_MANIFEST_NAME = "bundled.json"


def bundled_dir():
    """The Patch/ folder that ships next to the executable.

    Deliberately not paths._app_dir(): that returns the folder holding
    tavern_shared/ when running from source, so it resolves to
    tavern_shared/Patch and every lookup here silently misses in dev --
    which is how the first fallback went untested outside a frozen build.
    Same convention addon_loader.addons_dir() already uses for addons/."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "Patch")


def load_manifest():
    """Patch/bundled.json as a dict, or {} if it's absent or unreadable.

    Absent is a normal state, not an error: a build that ships no offline
    copies is exactly the download-only behaviour this replaced, and every
    caller already has to handle "no usable payload" anyway."""
    try:
        with open(os.path.join(bundled_dir(), BUNDLED_MANIFEST_NAME),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def verified_payload(component):
    """The path to the shipped copy of `component`, or None.

    None covers every way it could be unusable -- no manifest entry, no
    file, wrong size, wrong hash -- because the caller does the same thing
    in all of them. The size check is redundant against the hash and runs
    first anyway: it rejects a truncated 20MB zip without reading it."""
    entry = load_manifest().get(component)
    if not isinstance(entry, dict):
        return None
    filename, sha256 = entry.get("filename"), entry.get("sha256")
    if not filename or not sha256:
        return None
    path = os.path.join(bundled_dir(), filename)
    try:
        if os.path.getsize(path) != entry.get("size"):
            return None
        return path if _sha256_file(path) == sha256 else None
    except OSError:
        return None


def bundled_tag(component):
    """The release tag the shipped copy was fetched from, or None."""
    entry = load_manifest().get(component)
    return entry.get("tag") if isinstance(entry, dict) else None


def _tag_order(tag):
    """A release tag as a comparable tuple, or None if it isn't orderable.

    GitHub tags carry a leading 'v' that _parse_version rejects, and the
    launcher's own version grammar is deliberately strict (exactly three
    numeric segments, no pre-release suffixes). Anything outside that is
    None rather than a guess -- callers treat "can't order these" as "don't
    substitute", which is the safe direction.

    Imported here rather than at module scope to break a cycle: mod_install
    imports this module, and tavern_shared.mods (the package _parse_version
    lives in) imports mod_install on the way through mods.install. By the
    time anything calls this, both are fully loaded."""
    if not isinstance(tag, str):
        return None
    from tavern_shared.mods.errors import ModManagerError
    from tavern_shared.mods.version import _parse_version
    try:
        return _parse_version(tag.strip().lstrip("vV"))
    except ModManagerError:
        return None


def usable_payload(component, install_broken, installed_tag):
    """The shipped copy of `component` if installing it now would leave this
    machine better off than it is, else None.

    install_broken is the caller's local, network-free reading of "there is
    nothing here worth protecting" -- missing, failing its recorded hash, or
    installed without a complete record. Any verified copy beats those, so
    it wins outright.

    Otherwise there IS a working install and the only reason to overwrite it
    is a genuinely newer snapshot. Equal tags are refused as well as older
    ones: reinstalling the same release fixes nothing and would relabel a
    real online install as an offline one. If either tag can't be ordered,
    that's a refusal too -- the caller falls through to raising, and the
    user gets manual-install instructions rather than a silent downgrade."""
    path = verified_payload(component)
    if not path:
        return None
    if install_broken:
        return path
    mine, theirs = _tag_order(bundled_tag(component)), _tag_order(installed_tag)
    if mine is None or theirs is None:
        return None
    return path if mine > theirs else None
