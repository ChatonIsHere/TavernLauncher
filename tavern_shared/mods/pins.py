"""The client's always-on pin list and the per-server declined list. Both live
in the host app's own config dict."""
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.hostcfg import save_cfg
from tavern_shared.mods.version import _parse_version


# ["id" | "id@version"], unioned into every join's active set.
CFG_PINNED_KEY = "pinned_mods"


# {server host: [mod id, ...]} of declined recommendations. Per host, not
# host:port, matching how tokens are keyed.
CFG_DECLINED_KEY = "declined_mods"


def _parse_pin_entry(entry):
    """"id" (track latest) or "id@version" (exact), the syntax of pinned_mods and
    a modlist's `mods`. Returns (mod_id, version_or_None)."""
    if "@" in entry:
        mod_id, version = entry.split("@", 1)
        return mod_id, version
    return entry, None


def list_pinned(cfg):
    """The always-on pins: "id" / "id@version" strings."""
    return list(cfg.get(CFG_PINNED_KEY, []))


def list_declined(cfg, host):
    """The recommended mods declined for one server host."""
    return list((cfg.get(CFG_DECLINED_KEY) or {}).get(host, []))


def set_declined(cfg, host, mod_ids):
    """Replaces one server's declined set (the window hands back the whole set)
    and saves. A server with nothing declined drops out of the table."""
    table = dict(cfg.get(CFG_DECLINED_KEY) or {})
    ids = sorted(set(mod_ids or []))
    if ids:
        table[host] = ids
    else:
        table.pop(host, None)
    cfg[CFG_DECLINED_KEY] = table
    save_cfg(cfg)
    return ids


def add_pin(cfg, entry):
    """Adds or replaces the pin for a mod id, and saves. The shape is checked
    here because a pin joins every server's plan: a typo accepted now would
    fail far from where it was typed. Whether any source carries the mod is
    plan_join's question."""
    entry = (entry or "").strip()
    if not entry:
        raise ModManagerError("Enter a mod id, e.g. Author.ModId or Author.ModId@1.2.0.")
    mod_id, version = _parse_pin_entry(entry)
    # The '<github-user>.<github-repo>' split every manifest fetch depends on.
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
    """Removes a mod's pin by id (an @version suffix is ignored), and saves."""
    mod_id, _version = _parse_pin_entry(mod_id)
    cfg[CFG_PINNED_KEY] = [p for p in list_pinned(cfg) if _parse_pin_entry(p)[0] != mod_id]
    save_cfg(cfg)
