"""Source repositories: the configured ones, and their merged indexes."""
import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from urllib.parse import urlparse

from tavern_shared.mods.base import ModManagerError, _parse_version, _warn, save_cfg
from tavern_shared.mods.manifest import _summary_from_dict


DEFAULT_REPO = "https://raw.githubusercontent.com/ModdingTavern/TavernMods/main"
# {"Author/Repo": base url} of user-added repos; DEFAULT_REPO is implicit.
CFG_REPOS_KEY = "mod_repos"
SUPPORTED_INDEX_MAJOR = 2
_INDEX_TTL_SECONDS = 300
_MAX_JSON_BYTES = 8 * 1024 * 1024    # TavernLib's MaxJsonBytes


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "TavernLauncher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(_MAX_JSON_BYTES + 1)
        if len(body) > _MAX_JSON_BYTES:
            raise ModManagerError(f"{url} is over {_MAX_JSON_BYTES // (1024 * 1024)} MB; refusing it.")
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ModManagerError(f"{urlparse(url).netloc} returned HTTP {e.code} for {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ModManagerError(
            f"Couldn't reach {urlparse(url).netloc}: {getattr(e, 'reason', e)}")
    except ValueError:
        raise ModManagerError(f"{url} did not return valid JSON.")


def _norm(url):
    return (url or "").rstrip("/")


def _is_default(url):
    return _norm(url) == _norm(DEFAULT_REPO)


def _repo_shorthand(url):
    """"Author/Repo" from a base URL's first two path segments. Never turned
    back into a URL except by looking up a repo already added under it."""
    path = urlparse(_norm(url)).path.strip("/").split("/")
    return "/".join(path[:2]) if len(path) >= 2 else _norm(url)


def _repo_owner(url):
    """The GitHub owner a source belongs to ("ModdingTavern"), or None off GitHub."""
    if urlparse(_norm(url)).netloc.lower() not in ("raw.githubusercontent.com", "github.com"):
        return None
    path = urlparse(_norm(url)).path.strip("/").split("/")
    return path[0] if path and path[0] else None


def is_own_mod(mod_id, source_repo):
    """Whether a mod is by the owner of the source it comes from: its id's
    owner part ("ModdingTavern.TavernEssentials") is the source's GitHub owner."""
    owner = _repo_owner(source_repo)
    return bool(owner) and mod_id.split(".", 1)[0].lower() == owner.lower()


def _source_display(url):
    """"Author/Repo" on a GitHub host, the full URL anywhere else."""
    if urlparse(_norm(url)).netloc.lower() in ("raw.githubusercontent.com", "github.com"):
        return _repo_shorthand(url)
    return _norm(url)


def _user_repos(cfg):
    return {k: v for k, v in (cfg.get(CFG_REPOS_KEY) or {}).items() if not _is_default(v)}


def list_repos(cfg):
    """Configured repo base URLs, DEFAULT_REPO first."""
    return [DEFAULT_REPO] + list(_user_repos(cfg).values())


def add_repo(cfg, url):
    """Adds a source after checking it serves a v2 index: the only way a URL
    becomes pullable."""
    url = _norm(url)
    if not url:
        raise ModManagerError("Enter a repository URL.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ModManagerError("A repository URL must start with http:// or https://.")
    if url in [_norm(u) for u in list_repos(cfg)]:
        raise ModManagerError("That repository is already in your sources.")

    index = _get_json(f"{url}/repository.json")
    if not isinstance(index, dict) or not isinstance(index.get("mods"), dict):
        raise ModManagerError(
            "That URL didn't serve a valid mod index (no 'mods' object in "
            "repository.json). Double-check it's the raw-content base URL of a "
            "correctly structured repo.")
    if not _is_supported(index):
        raise ModManagerError(
            f"That repository's index is schema v{index.get('index_version')}, and "
            f"this launcher reads v{SUPPORTED_INDEX_MAJOR} only, so it would list "
            f"nothing from it.")

    shorthand = _repo_shorthand(url)
    named = dict(cfg.get(CFG_REPOS_KEY) or {})
    if shorthand in named and _norm(named[shorthand]) != url:
        raise ModManagerError(
            f"'{shorthand}' is already registered pointing at a different URL "
            f"({named[shorthand]}); remove it first if you want to repoint it.")
    named[shorthand] = url
    cfg[CFG_REPOS_KEY] = named
    save_cfg(cfg)


def remove_repo(cfg, ref):
    """By shorthand or URL; DEFAULT_REPO can't be removed."""
    if _is_default(ref) or _repo_shorthand(ref) == _repo_shorthand(DEFAULT_REPO):
        raise ModManagerError("The default Modding Tavern repository can't be removed.")
    cfg[CFG_REPOS_KEY] = {k: v for k, v in (cfg.get(CFG_REPOS_KEY) or {}).items()
                          if k != ref and _norm(v) != _norm(ref)}
    save_cfg(cfg)


def _is_supported(index):
    # An int, as TavernLib reads it: 2.0 == 2 in Python.
    iv = index.get("index_version")
    return isinstance(iv, int) and iv == SUPPORTED_INDEX_MAJOR


@dataclass
class Replacement:
    """One entry of a source's `replaced`: old_id is retired in favour of by."""
    old_id: str
    by: str
    note: str
    source_repo: str


class ModIndex(list):
    """fetch_indexes' ModSummaries, with every source's replacements."""

    def __init__(self, summaries=(), replaced=()):
        super().__init__(summaries)
        self.replaced = list(replaced)


def replacement_for(index, mod_id, source_repo=None):
    """The Replacement of mod_id. With source_repo, only that source's counts:
    a source can retire only what it served. Without, DEFAULT_REPO's wins,
    then the first source's."""
    reps = [r for r in getattr(index, "replaced", ()) if r.old_id == mod_id]
    if source_repo is not None:
        return next((r for r in reps if _norm(r.source_repo) == _norm(source_repo)), None)
    return next((r for r in reps if _is_default(r.source_repo)), reps[0] if reps else None)


def replacement_of_unlisted(index, mod_id):
    """The Replacement to use for an id named without a source (a dependency,
    a pin, a modlist entry): only when no source lists the id and one lists
    its replacement."""
    listed = {s.id for s in index}
    if mod_id in listed:
        return None
    rep = replacement_for(index, mod_id)
    return rep if rep and rep.by in listed else None


def _replacements_from(index, base):
    raw = index.get("replaced")
    out = []
    for old_id, entry in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(entry, dict) and isinstance(entry.get("by"), str) and entry["by"] != old_id:
            note = entry.get("note")
            out.append(Replacement(old_id, entry["by"], note if isinstance(note, str) else "", base))
    return out


_index_cache = {}   # base -> (fetched_at, (list[ModSummary], list[Replacement]))


def _fetch_repo_index(base, force):
    """One repo's (ModSummaries, Replacements), cached for _INDEX_TTL_SECONDS.
    A failed fetch or unsupported index warns and yields the stale cache or
    nothing."""
    base = _norm(base)
    now = time.time()
    hit = _index_cache.get(base)
    if not force and hit and now - hit[0] < _INDEX_TTL_SECONDS:
        return hit[1]
    try:
        index = _get_json(f"{base}/repository.json")
        if not isinstance(index, dict) or not _is_supported(index):
            iv = index.get("index_version") if isinstance(index, dict) else None
            raise ModManagerError(f"index schema v{iv}, this build supports "
                                  f"v{SUPPORTED_INDEX_MAJOR}")
    except ModManagerError as e:
        _warn(f"skipping repo {base}: {e}")
        return hit[1] if hit else ([], [])

    out = []
    mods = index.get("mods")
    for mod_id, by_major in (mods.items() if isinstance(mods, dict) else ()):
        if isinstance(by_major, dict):
            out += [s for s in (_summary_from_dict(e, mod_id, base)
                                for e in by_major.values() if isinstance(e, dict))
                    if s is not None]
    _index_cache[base] = (now, (out, _replacements_from(index, base)))
    return _index_cache[base][1]


def fetch_indexes(repo_bases, force=False):
    """Every repo's index merged to one entry per (id, major): DEFAULT_REPO
    wins a collision, otherwise the highest version does. Carries every
    repo's replacements."""
    merged = {}   # (id, major) -> (ModSummary, from_default)
    replaced = []
    for base in repo_bases:
        from_default = _is_default(base)
        summaries, reps = _fetch_repo_index(base, force)
        replaced += reps
        for s in summaries:
            key = (s.id, s.major)
            cur = merged.get(key)
            if (cur is None or (from_default and not cur[1])
                    or (from_default == cur[1]
                        and _parse_version(s.highest()) > _parse_version(cur[0].highest()))):
                merged[key] = (s, from_default)
    return ModIndex((s for s, _default in merged.values()), replaced)
