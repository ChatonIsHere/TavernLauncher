"""The client's always-on pins and per-server declined recommendations."""
from tavern_shared.mods.base import ModManagerError, _parse_version, save_cfg


CFG_PINNED_KEY = "pinned_mods"        # ["id" | "id@version"]
CFG_DECLINED_KEY = "declined_mods"    # {server host: [mod id]}, keyed like tokens


def _parse_pin_entry(entry):
    """"id" or "id@version" -> (id, version or None); also a modlist entry.
    A bare "id@" means latest, as in TavernLib."""
    mod_id, _sep, version = entry.partition("@")
    return mod_id, version or None


def list_pinned(cfg):
    return list(cfg.get(CFG_PINNED_KEY, []))


def list_declined(cfg, host):
    return list((cfg.get(CFG_DECLINED_KEY) or {}).get(host, []))


def set_declined(cfg, host, mod_ids):
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
    """Adds or replaces a mod's pin. Its shape is checked here because a pin
    joins every server's plan, where a typo would fail far from its source."""
    entry = (entry or "").strip()
    if not entry:
        raise ModManagerError("Enter a mod id, e.g. Author.ModId or Author.ModId@1.2.0.")
    mod_id, version = _parse_pin_entry(entry)
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
    cfg[CFG_PINNED_KEY] = [p for p in list_pinned(cfg)
                           if _parse_pin_entry(p)[0] != mod_id] + [entry]
    save_cfg(cfg)


def remove_pin(cfg, mod_id):
    mod_id = _parse_pin_entry(mod_id)[0]
    cfg[CFG_PINNED_KEY] = [p for p in list_pinned(cfg) if _parse_pin_entry(p)[0] != mod_id]
    save_cfg(cfg)
