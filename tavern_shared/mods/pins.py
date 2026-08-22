"""The client's always-on pin list and the per-server declined list. Both live
in the host app's own config dict."""
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.hostcfg import require_save_cfg, save_cfg
from tavern_shared.mods.version import _parse_version


# Config key for the client's always-on pin list: a flat list of
# "id" (track latest) / "id@version" (pin exact) strings, unioned into every
# join's active set regardless of which server is joined (plan_join). Same
# dict load_cfg()/save_cfg() already manage; see list_pinned/add_pin/remove_pin.
CFG_PINNED_KEY = "pinned_mods"


# Config key for recommended mods the user has said no to, as
# {server host: [mod id, ...]}. Only ever holds mods a server marks
# parity_required false, and only
# for the server they were declined on - a mod not wanted on one server is
# routinely wanted on another. Per host rather than per host:port, matching how
# tokens are keyed: one machine serving one community is one server.
CFG_DECLINED_KEY = "declined_mods"


def _parse_pin_entry(entry):
    """Parses a pinned_mods / modlist entry: "id" (track latest) or
    "id@version" (pin an exact version) - same syntax used everywhere a
    desired-mods list is written (the headless modlist's `mods`, the
    launcher's `pinned_mods`). Returns (mod_id, version_or_None)."""
    if "@" in entry:
        mod_id, version = entry.split("@", 1)
        return mod_id, version
    return entry, None


def list_pinned(cfg):
    """The launcher's always-on pin list: "id" or "id@version" strings, in
    cfg[CFG_PINNED_KEY]. Not repo-shorthand-keyed like mod_repos - a pin only
    ever needs a mod id and an optional exact version, so a flat list is
    enough."""
    return list(cfg.get(CFG_PINNED_KEY, []))


def list_declined(cfg, host):
    """Recommended mods the user has turned off for one server. Empty for a
    server never seen before, which is what makes installing the default."""
    return list((cfg.get(CFG_DECLINED_KEY) or {}).get(host, []))


def set_declined(cfg, host, mod_ids):
    """Records this server's declined set, replacing whatever was there - the
    window hands back the whole set each time, including choices taken back, so
    it's a write not a merge. Saves via require_save_cfg, NOT the soft save_cfg
    its siblings use: the window expects the whole set to stick, so an unwired
    config is a hard error here rather than a silently lost write. A server
    with nothing declined drops out entirely rather than leaving an empty list
    behind."""
    save = require_save_cfg()
    table = dict(cfg.get(CFG_DECLINED_KEY) or {})
    ids = sorted(set(mod_ids or []))
    if ids:
        table[host] = ids
    else:
        table.pop(host, None)
    cfg[CFG_DECLINED_KEY] = table
    save(cfg)
    return ids


def add_pin(cfg, entry):
    """Adds (or updates) a pin. entry is "id" or "id@version" - see
    _parse_pin_entry. Replaces any existing pin for the same mod id rather
    than allowing two pins for one mod to disagree. Saves via the injected
    save_cfg, same convention as add_repo/remove_repo.

    The shape is checked here rather than left to resolution, because a pin is
    unioned into EVERY join: a typo'd id accepted here is one that fails to
    resolve against every server the user ever visits, showing up as a puzzling
    row on each of them, long after the moment it could be connected to what
    was typed. Only the shape - whether a source actually carries this mod is a
    question for plan_join, which reports it without blocking the join."""
    entry = (entry or "").strip()
    if not entry:
        raise ModManagerError("Enter a mod id, e.g. Author.ModId or Author.ModId@1.2.0.")
    mod_id, version = _parse_pin_entry(entry)
    # The same '<github-user>.<github-repo>' split every by-id manifest fetch
    # depends on (see _fetch_manifest_leaf, which can't resolve anything else).
    if not mod_id or "." not in mod_id:
        raise ModManagerError(
            f"'{entry}' isn't a valid mod id. Ids look like Author.ModId, "
            f"optionally with an exact version: Author.ModId@1.2.0.")
    if version is not None:
        try:
            _parse_version(version)
        except ModManagerError as e:
            raise ModManagerError(
                f"'{entry}' doesn't pin a usable version: {e} Leave the "
                f"@version off to always track the latest release.")
    pinned = [p for p in list_pinned(cfg) if _parse_pin_entry(p)[0] != mod_id]
    pinned.append(entry)
    cfg[CFG_PINNED_KEY] = pinned
    save_cfg(cfg)


def remove_pin(cfg, mod_id):
    """Removes a mod's pin by id (with or without an @version suffix)."""
    mod_id, _version = _parse_pin_entry(mod_id)
    cfg[CFG_PINNED_KEY] = [p for p in list_pinned(cfg) if _parse_pin_entry(p)[0] != mod_id]
    save_cfg(cfg)
