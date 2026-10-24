"""The offline copies of the Setup payloads shipped in Patch/, and the rule for
when one may stand in for a download that won't complete. A shipped copy is
used only if it matches the sha256 and size recorded in Patch/bundled.json,
only when it would leave the install better off (nothing worth protecting,
or a genuinely newer release), and is recorded under its real release tag
and marked as bundled."""
import json
import os
import sys

from tavern_shared.paths import _sha256_file

BUNDLED_MANIFEST_NAME = "bundled.json"


def bundled_dir():
    """The Patch/ folder next to the executable, or next to tavern_shared/ when
    running from source (paths._app_dir would point inside tavern_shared/)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "Patch")


def load_manifest():
    """Patch/bundled.json as a dict, or {} if absent or unreadable; a build may
    ship no offline copies at all."""
    try:
        with open(os.path.join(bundled_dir(), BUNDLED_MANIFEST_NAME),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def verified_payload(component):
    """The path to the shipped copy of `component`, or None if it has no entry,
    is missing, or fails its recorded size or sha256."""
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
    """A release tag ("v1.2.3") as a comparable tuple, or None when it isn't
    three numeric segments; callers treat None as "don't substitute". Imports
    lazily: tavern_shared.mods imports mod_install, which imports this."""
    if not isinstance(tag, str):
        return None
    from tavern_shared.mods.errors import ModManagerError
    from tavern_shared.mods.version import _parse_version
    try:
        return _parse_version(tag.strip().lstrip("vV"))
    except ModManagerError:
        return None


def usable_payload(component, install_broken, installed_tag):
    """The shipped copy of `component` if installing it now improves things,
    else None. install_broken is the caller's network-free "nothing here worth
    protecting" reading, and any verified copy beats that. Otherwise only a
    strictly newer tag than installed_tag qualifies; equal or unorderable tags
    refuse, so a working install is never downgraded or relabelled offline."""
    path = verified_payload(component)
    if not path:
        return None
    if install_broken:
        return path
    mine, theirs = _tag_order(bundled_tag(component)), _tag_order(installed_tag)
    if mine is None or theirs is None:
        return None
    return path if mine > theirs else None
