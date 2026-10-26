"""Source repositories: the configured ones, and their merged indexes."""
import json
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse

from tavern_shared.mods.base import ModManagerError, _parse_version, _warn, save_cfg
from tavern_shared.mods.manifest import _summary_from_dict


DEFAULT_REPO = "https://raw.githubusercontent.com/ModdingTavern/TavernMods/main"
# {"Author/Repo": base url} of user-added repos; DEFAULT_REPO is implicit.
CFG_REPOS_KEY = "mod_repos"
SUPPORTED_INDEX_MAJOR = 2
_INDEX_TTL_SECONDS = 300


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "TavernLauncher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
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
    if index.get("index_version") != SUPPORTED_INDEX_MAJOR:
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


_index_cache = {}   # base -> (fetched_at, list[ModSummary])


def _fetch_repo_index(base, force):
    """One repo's ModSummaries, cached for _INDEX_TTL_SECONDS. A failed fetch
    or unsupported index warns and yields the stale cache or []."""
    base = _norm(base)
    now = time.time()
    hit = _index_cache.get(base)
    if not force and hit and now - hit[0] < _INDEX_TTL_SECONDS:
        return hit[1]
    try:
        index = _get_json(f"{base}/repository.json")
        iv = index.get("index_version") if isinstance(index, dict) else None
        if iv != SUPPORTED_INDEX_MAJOR:
            raise ModManagerError(f"index schema v{iv}, this build supports "
                                  f"v{SUPPORTED_INDEX_MAJOR}")
    except ModManagerError as e:
        _warn(f"skipping repo {base}: {e}")
        return hit[1] if hit else []

    out = []
    for mod_id, by_major in index.get("mods", {}).items():
        if isinstance(by_major, dict):
            out += [s for s in (_summary_from_dict(e, mod_id, base) for e in by_major.values())
                    if s is not None]
    _index_cache[base] = (now, out)
    return out


def fetch_indexes(repo_bases, force=False):
    """Every repo's index merged to one entry per (id, major): DEFAULT_REPO
    wins a collision, otherwise the highest version does."""
    merged = {}   # (id, major) -> (ModSummary, from_default)
    for base in repo_bases:
        from_default = _is_default(base)
        for s in _fetch_repo_index(base, force):
            key = (s.id, s.major)
            cur = merged.get(key)
            if (cur is None or (from_default and not cur[1])
                    or (from_default == cur[1]
                        and _parse_version(s.highest()) > _parse_version(cur[0].highest()))):
                merged[key] = (s, from_default)
    return [s for s, _default in merged.values()]
