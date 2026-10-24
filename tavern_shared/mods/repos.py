"""Source repositories: which ones are configured, and fetching/merging their
compiled repository.json indexes."""
import json
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse

from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.hostcfg import save_cfg
from tavern_shared.mods.manifest import _summary_from_dict
from tavern_shared.mods.version import _parse_version


# A raw-content base URL: repository.json and manifests/ hang off it.
DEFAULT_REPO = "https://raw.githubusercontent.com/ChatonIsHere/CommunityMods/main"


# User-added repos in the host config: {"Author/Repo": base url}, written only
# by add_repo. DEFAULT_REPO is implicit.
CFG_REPOS_KEY = "mod_repos"


# repository.json's own schema major, separate from the manifest's; any other
# index is skipped whole.
SUPPORTED_INDEX_MAJOR = 1


# In-memory index cache lifetime; callers pass force=True after a repo edit.
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
    except (ValueError, json.JSONDecodeError):
        raise ModManagerError(f"{url} did not return valid JSON.")


def _norm(url):
    return (url or "").rstrip("/")


def _is_default(url):
    return _norm(url) == _norm(DEFAULT_REPO)


def _repo_shorthand(url):
    """"Author/Repo" from a repo's base URL (its first two path segments).
    Identification only, as in a modlist's `repos`: never turned back into a
    URL except by looking up a repo already added under this key."""
    path = urlparse(_norm(url)).path.strip("/").split("/")
    return "/".join(path[:2]) if len(path) >= 2 else _norm(url)


_GITHUB_HOSTS = ("raw.githubusercontent.com", "github.com")


def _source_display(url):
    """A repo's label for people: "Author/Repo" on a GitHub host, the full URL
    anywhere else."""
    if urlparse(_norm(url)).netloc.lower() in _GITHUB_HOSTS:
        return _repo_shorthand(url)
    return _norm(url)


def _named_repos(cfg):
    """A copy of cfg[CFG_REPOS_KEY]: {shorthand: url}."""
    return dict(cfg.get(CFG_REPOS_KEY) or {})


def list_repos(cfg):
    """Configured repo base URLs, DEFAULT_REPO always first. Treat cfg as
    short-lived (load, mutate, save): load_cfg re-reads the file each call."""
    stored = [u for u in _named_repos(cfg).values() if not _is_default(u)]
    return [DEFAULT_REPO] + stored


def list_repos_named(cfg):
    """{shorthand: url} for every configured repo, DEFAULT_REPO included."""
    named = {k: v for k, v in _named_repos(cfg).items() if not _is_default(v)}
    return {_repo_shorthand(DEFAULT_REPO): DEFAULT_REPO, **named}


def add_repo(cfg, url):
    """Adds a user source repo after a live check that {url}/repository.json is
    a valid index, refusing duplicates and a shorthand already pointing
    elsewhere. This is the only way a URL becomes pullable; a shorthand in an
    imported modlist never is. Saves via save_cfg."""
    url = _norm(url)
    if not url:
        raise ModManagerError("Enter a repository URL.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ModManagerError("A repository URL must start with http:// or https://.")
    if _is_default(url) or url in [_norm(u) for u in list_repos(cfg)]:
        raise ModManagerError("That repository is already in your sources.")

    index = _get_json(f"{url}/repository.json")
    if not isinstance(index, dict) or "mods" not in index or not isinstance(index["mods"], dict):
        raise ModManagerError(
            "That URL didn't serve a valid mod index (no 'mods' object in "
            "repository.json). Double-check it's the raw-content base URL of a "
            "correctly structured repo.")

    shorthand = _repo_shorthand(url)
    named = _named_repos(cfg)
    if shorthand in named and _norm(named[shorthand]) != url:
        raise ModManagerError(
            f"'{shorthand}' is already registered pointing at a different URL "
            f"({named[shorthand]}); remove it first if you want to repoint it.")
    named[shorthand] = url
    cfg[CFG_REPOS_KEY] = named
    save_cfg(cfg)


def remove_repo(cfg, ref):
    """Removes a user repo by shorthand or URL. DEFAULT_REPO can't be removed."""
    if _is_default(ref) or _repo_shorthand(ref) == _repo_shorthand(DEFAULT_REPO):
        raise ModManagerError("The default Modding Tavern repository can't be removed.")
    ref_norm = _norm(ref)
    named = _named_repos(cfg)
    cfg[CFG_REPOS_KEY] = {k: v for k, v in named.items()
                          if k != ref and _norm(v) != ref_norm}
    save_cfg(cfg)


# Index fetching / merging

# base URL -> (fetched_at, list[ModSummary]), per repo.
_index_cache = {}


def _fetch_repo_index(base, force):
    """One repo's repository.json as list[ModSummary] tagged with its base,
    cached with a TTL. A fetch failure or unsupported index_version yields the
    stale cache or [] with a warning, never an error."""
    base = _norm(base)
    now = time.time()
    if not force:
        hit = _index_cache.get(base)
        if hit and (now - hit[0]) < _INDEX_TTL_SECONDS:
            return hit[1]

    try:
        index = _get_json(f"{base}/repository.json")
    except ModManagerError as e:
        _warn(f"skipping repo {base}: {e}")
        hit = _index_cache.get(base)
        return hit[1] if hit else []

    iv = index.get("index_version") if isinstance(index, dict) else None
    if iv != SUPPORTED_INDEX_MAJOR:
        _warn(f"index at {base} is index schema v{iv}, this build supports "
              f"v{SUPPORTED_INDEX_MAJOR}, skipped.")
        hit = _index_cache.get(base)
        return hit[1] if hit else []

    mods = index.get("mods", {}) if isinstance(index, dict) else {}
    out = []
    for mod_id, by_major in mods.items():
        if not isinstance(by_major, dict):
            continue
        for _major_key, entry in by_major.items():
            s = _summary_from_dict(entry, mod_id, base)
            if s is not None:
                out.append(s)
    _index_cache[base] = (now, out)
    return out


def fetch_indexes(repo_bases, force=False):
    """Every repo's index merged to one entry per (id, major): the one place
    cross-repo collisions are settled. DEFAULT_REPO wins a collision,
    otherwise the highest version does; different majors of one id aren't a
    collision. Cached per repo for _INDEX_TTL_SECONDS unless force."""
    merged = {}   # (id, major) -> (ModSummary, from_default: bool)
    for base in repo_bases:
        from_default = _is_default(base)
        for s in _fetch_repo_index(base, force):
            key = (s.id, s.major)
            if key not in merged:
                merged[key] = (s, from_default)
                continue
            cur, cur_default = merged[key]
            if from_default and not cur_default:
                merged[key] = (s, True)                     # default always wins
            elif cur_default and not from_default:
                continue                                    # keep the default
            elif _parse_version(s.highest()) > _parse_version(cur.highest()):
                merged[key] = (s, from_default)             # otherwise highest wins
    return [s for (s, _default) in merged.values()]
