"""The Setup payloads shipped in Patch/, and when one may stand in for a
download that won't complete."""
import json
import os
import sys

from tavern_shared.paths import _sha256_file

BUNDLED_MANIFEST_NAME = "bundled.json"


def bundled_dir():
    # From source, paths._app_dir would point inside tavern_shared/.
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "Patch")


def load_manifest():
    try:
        with open(os.path.join(bundled_dir(), BUNDLED_MANIFEST_NAME),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def verified_payload(component):
    """The shipped copy's path if it matches bundled.json's size and sha256."""
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
    entry = load_manifest().get(component)
    return entry.get("tag") if isinstance(entry, dict) else None


def _tag_order(tag):
    """"v1.2.3" as a tuple, else None. Imported lazily: tavern_shared.mods
    imports mod_install, which imports this."""
    if not isinstance(tag, str):
        return None
    from tavern_shared.mods.base import ModManagerError, _parse_version
    try:
        return _parse_version(tag.strip().lstrip("vV"))
    except ModManagerError:
        return None


def usable_payload(component, install_broken, installed_tag):
    """The shipped copy if installing it improves things: any verified copy
    beats a broken install, else only a strictly newer tag qualifies, so a
    working install is never downgraded or relabelled offline."""
    path = verified_payload(component)
    if not path:
        return None
    if install_broken:
        return path
    mine, theirs = _tag_order(bundled_tag(component)), _tag_order(installed_tag)
    if mine is None or theirs is None:
        return None
    return path if mine > theirs else None
