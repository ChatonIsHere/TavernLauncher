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


# A *base* URL, not a direct link to repository.json. Every repo source is
# identified by the raw-content root its files hang off of, so both the compiled
# index (f"{base}/repository.json") and a single manifest
# (f"{base}/manifests/{author}/{id}/latest.json") derive from the same entry.
#
# This constant is the only place the repo location is hard-coded, so renaming
# or moving the repo is a one-line change here.
DEFAULT_REPO = "https://raw.githubusercontent.com/ChatonIsHere/CommunityMods/main"


# Config key inside the dict load_cfg()/save_cfg() already read/write. No
# separate config file. Holds user-added source repos, keyed by their
# "Author/Repo" shorthand (see _repo_shorthand) mapping to the actual base URL;
# DEFAULT_REPO is implicit (always present, never stored as removable, see
# list_repos/remove_repo). A repo only ever lands in here through add_repo's
# live fetch-and-validate check - the shorthand form is for identification only
# (e.g. in a modlist's `repos` field) and is never enough on its own to add a
# new pullable source.
CFG_REPOS_KEY = "mod_repos"


# The index schema major this build understands, repository.json's own version,
# kept separate from the manifest one because the index (slim summaries) and the
# manifest (full record) are different schemas that can evolve independently. A
# repository.json whose index_version major we don't support is skipped whole (see
# _fetch_repo_index), so a future index format doesn't get mis-read.
SUPPORTED_INDEX_MAJOR = 1


# In-memory index cache TTL. Repeated UI refreshes shouldn't hammer GitHub, but
# the data should still feel live; add_repo/remove_repo pass force=True so a repo
# edit is reflected immediately regardless of this.
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
    """Derives a GitHub-style "Author/Repo" display identifier from a repo's
    base URL - the first two path segments (works for
    raw.githubusercontent.com/<author>/<repo>/<branch> regardless of branch
    name or depth beyond that; degrades to *some* stable identifier for a
    non-GitHub host too). Display/identification only: this is what a modlist's
    `repos` field lists, and it is NEVER resolved back into a URL except by
    looking an already-registered repo up by this same key - it can't be used
    to synthesize or guess a pullable URL."""
    path = urlparse(_norm(url)).path.strip("/").split("/")
    return "/".join(path[:2]) if len(path) >= 2 else _norm(url)


_GITHUB_HOSTS = ("raw.githubusercontent.com", "github.com")


def _source_display(url):
    """Human-facing label for a repo URL, distinct from _repo_shorthand's
    identifier role: "Author/Repo" for a GitHub host, since that's a
    recognizable convention there, but the full URL verbatim for anything
    else - a non-GitHub host has no such shorthand convention, so collapsing
    it the same way would just hide which source it actually is."""
    if urlparse(_norm(url)).netloc.lower() in _GITHUB_HOSTS:
        return _repo_shorthand(url)
    return _norm(url)


def _named_repos(cfg):
    """cfg[CFG_REPOS_KEY] as {shorthand: url}, migrating a pre-shorthand
    list-of-URLs shape in memory (older configs stored a plain list; new ones
    store the dict directly). Doesn't write back on its own - callers that
    mutate the result save via _save_cfg."""
    raw = cfg.get(CFG_REPOS_KEY, {})
    if isinstance(raw, list):
        return {_repo_shorthand(u): u for u in raw}
    return dict(raw)


def list_repos(cfg):
    """Repo base URLs from cfg[CFG_REPOS_KEY], with DEFAULT_REPO always present at
    the front: seeded on first run, re-inserted if ever missing (it isn't a
    removable entry; see remove_repo). cfg is the dict load_cfg()/save_cfg()
    already read/write. No separate config file.

    Concurrency note: load_cfg() re-reads the file on every call, so a long-lived
    cfg dict mutated here and saved can last-write-clobber a concurrent save
    elsewhere. Pre-existing property of the config pattern, low-stakes for a
    single-user GUI, but callers should treat cfg as short-lived (load, mutate,
    save) rather than caching it."""
    stored = [u for u in _named_repos(cfg).values() if not _is_default(u)]
    return [DEFAULT_REPO] + stored


def list_repos_named(cfg):
    """{shorthand: url} for every configured repo, DEFAULT_REPO included -
    what a modlist's `repos` field lists (the keys only) and anywhere else a
    repo needs its "Author/Repo" display identifier rather than its full URL."""
    named = {k: v for k, v in _named_repos(cfg).items() if not _is_default(v)}
    return {_repo_shorthand(DEFAULT_REPO): DEFAULT_REPO, **named}


def add_repo(cfg, url):
    """Adds a user source repo. Two guards before it's accepted:
      1. Structure check: GET {url}/repository.json and confirm it parses as the
         expected schema. A URL that doesn't serve a valid index is rejected
         here, not discovered later mid-resolve. This is the one per-repo trust
         check we do (is this actually a mod-index repo, laid out correctly).
      2. Not a duplicate, and its derived shorthand doesn't already point
         somewhere else (a repo renamed/moved by the same author would need its
         old entry removed first, rather than silently repointing it).
    The *warning* that this repo is unverified (not reviewed by Modding Tavern,
    the user is responsible for vetting it) is surfaced by the UI at the moment
    of adding, not enforced here. This is the ONLY way a URL becomes pullable -
    a shorthand appearing in an imported modlist is never enough on its own
    (see resolve_dependencies/fetch_indexes, which only ever see list_repos'
    output, never a modlist's `repos` field directly). On success, saves via
    the injected save_cfg."""
    url = _norm(url)
    if not url:
        raise ModManagerError("Enter a repository URL.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ModManagerError("A repository URL must start with http:// or https://.")
    if _is_default(url) or url in [_norm(u) for u in list_repos(cfg)]:
        raise ModManagerError("That repository is already in your sources.")

    # Structure check: must serve a valid repository.json in our schema.
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
    """Removes a repo from cfg[CFG_REPOS_KEY], matched by either its shorthand
    ("Author/Repo") or its full URL. Refuses DEFAULT_REPO (by either form);
    that entry is non-removable (and the UI never offers its remove button).
    It's also the collision-priority winner (its entry wins when two repos
    publish the same id+major), but otherwise fetched/searched identically to
    every other repo."""
    if _is_default(ref) or _repo_shorthand(ref) == _repo_shorthand(DEFAULT_REPO):
        raise ModManagerError("The default Modding Tavern repository can't be removed.")
    ref_norm = _norm(ref)
    named = _named_repos(cfg)
    cfg[CFG_REPOS_KEY] = {k: v for k, v in named.items()
                          if k != ref and _norm(v) != ref_norm}
    save_cfg(cfg)


# Index fetching / merging

# base URL -> (fetched_at, list[ModSummary]).  Per-base so one slow/broken repo
# doesn't invalidate the others' caches.
_index_cache = {}


def _fetch_repo_index(base, force):
    """Loads one repo's repository.json into a list[ModSummary] (one per
    (id, major)), tagging each with source_repo=base. Cached with a TTL; a repo
    that fails is returned as [] with a warning, never a hard failure. An index
    whose top-level index_version isn't one we support is skipped whole, the
    forward-compat hook that lets the index schema grow later."""
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
        # Serve a stale cache if we have one; otherwise nothing from this repo.
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
    """GETs {base}/repository.json from every configured repo, tags each
    ModSummary with its source_repo, and MERGES them into one resolved entry per
    (id, major). This is the single place cross-repo collisions are settled, so
    no consumer downstream re-applies the rule.

    On a collision (two repos publishing the same (id, major)):
    DEFAULT_REPO's entry wins if present, otherwise the one with the highest
    version among the remaining (user-added) repos. Entries sharing an id but
    differing in *major* (e.g. Acme.UtilKit 1.4.0 and 2.1.0) are NOT a collision;
    both are kept, since major-lock resolution needs them.

    Cached per-base with a ~5-minute TTL; force=True bypasses it (add_repo/
    remove_repo pass force so a repo edit is reflected immediately). A repo that
    fails to fetch is skipped with a warning; one bad third-party source
    shouldn't break the official one."""
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
