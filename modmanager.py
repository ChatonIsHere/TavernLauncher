"""
Community mod manager for TavernLauncher.

Lives next to att_client.py / att_server.py. Everything about fetching a mod
index, resolving dependencies, and putting a verified .dll on disk lives HERE.
The two mono files never talk to GitHub, hash anything, or touch Mods/ for
index-driven mods directly.

Two on-disk shapes are contracts any other installer of these mods must match
byte-for-byte: the per-mod install layout + its record, and by-id resolution
(latest.json / latest.<major>.json). Keep those two shapes stable.

Each mod installs into its OWN folder, Mods/<id>/, and its record is written as
Mods/<id>/manifest.json inside that folder. That filename is deliberate: recent
MelonLoader only scans a Mods/ subfolder that contains a manifest.json (it checks
existence only, never the content), so the record doubles as the marker that makes
the folder load. The record is the fetched manifest verbatim plus install fields
(source_repo, package, libraries). It deliberately does NOT record the .dll's
filename: every operation works on the folder (install swaps it, uninstall deletes
it, update replaces it), so nothing ever needs to find the file by name. Uninstall
is "delete the folder".

A single .dll mod (package="dll") keeps the name it was published under (from the
download_url, traversal-guarded); a bundle (package="zip") is an archive extracted
into Mods/<id>/, so a mod can ship several DLLs plus Unity asset bundles /
addressables. Either way the download is one artifact, verified against one sha256
before anything is written.

Libraries are separate: a mod's library_dependencies install FLAT into UserLibs/
(never bundled into a mod folder), each with its own UserLibs/<filename>.meta.json
sidecar. A mod's record lists the library filenames it pins ("libraries"), which is
how uninstall_mod reference-counts UserLibs/ files: a library is deleted only when
no remaining mod still lists it. Native/unmanaged DLLs MUST be libraries, because
MelonLoader only registers UserLibs/ (not mod folders) as a native search path.

Host-provided surface (borrowed, never re-implemented here): the download/hash/
config helpers (_download_with_progress, _sha256_file, load_cfg, save_cfg), which
already exist and are tested in each mono file, plus the launcher's palette and
small widget factories. The launcher wires all of it once at startup with a
single set_helpers(host) call, handing this module its own module object; see
set_helpers for the exact attribute list.

The community-mods UI (the Community Mods and Manage Sources windows) also lives
here so both launchers share one copy instead of duplicating it. Those windows
are Tk, but they own no styling: they read the palette and widget factories the
host wired in, so this module carries the behaviour and the launcher keeps its
look.
"""

import os
import sys
import json
import time
import shutil
import hashlib
import zipfile
import tempfile
import threading
import contextlib
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import messagebox, simpledialog, filedialog
from dataclasses import dataclass
from urllib.parse import urlparse


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

# A backstop distinct from cycle detection: a real cycle (A -> B -> A) is caught
# regardless of this number, but a long strictly-acyclic chain would still
# recurse once per link, and a broken/adversarial chain doesn't have to be a
# cycle to be a problem. Kept deliberately low: real chains are 1 to 2 hops,
# occasionally 3; anything hitting this cap is almost certainly a mistake. Turns
# a pathological chain into one clear "too deep" error instead of a raw
# RecursionError.
MAX_DEPENDENCY_DEPTH = 5

# The manifest schema major this build understands. A per-version manifest whose
# manifest_version has a *different* major is skipped rather than mis-parsed. This
# is the forward-compat hook that lets the manifest schema grow later (multi-file
# mods, new fields) without an old launcher guessing at a shape it doesn't get.
# Bump this only alongside a real manifest schema change.
SUPPORTED_MANIFEST_MAJOR = 1

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

# Zip-bundle extraction caps (package="zip"). The official repo's validator
# (CommunityMods/tools/modindex.py) checks the same intent against the archive's
# header sizes; these run at install time against ACTUAL decompressed bytes, so
# they also protect the one path that never saw the validator: a mod from an
# unreviewed third-party repo. Extraction aborts (into throwaway staging) the
# moment either cap is crossed, so a decompression bomb can't fill the disk.
_ZIP_MAX_ENTRIES = 2000
_ZIP_MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024   # 500 MB extracted, summed


def _tavern_data_dir():
    """Same shared appdata folder both mono files already use for their own
    config (%AppData%/TheModdingTavern) - a small, dependency-free copy rather
    than a set_helpers()-borrowed one, since it's a pure function of the
    environment, not host state. Home for the client mod cache."""
    base = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    return os.path.join(base, "TheModdingTavern")


class ModManagerError(Exception):
    """One error type the UI can catch and show verbatim. Every raise below
    carries a specific, actionable message (which mods, which versions, which
    repo) rather than a generic failure."""


# Host-provided surface (wired once from the mono file)
# Everything this module borrows from its host launcher instead of re-defining:
# the download/hash/config helpers (already tested in att_client.py /
# att_server.py), the palette, and the small widget factories the windows render
# with. Populated by set_helpers(host); None until then, and no window is ever
# built before the host wires them. The names match the host's on purpose, so the
# window classes below read BG/_btn/... exactly as they did in the launcher.

_download_with_progress = None
_sha256_file = None
_load_cfg = None
_save_cfg = None
BG = SURF = BORDER = AMBER = AMBERDIM = PARCH = MUTED = GREEN = RED = CYAN = None
_btn = _mk_tree = _mk_scrollbar = _enable_dark_titlebar = None

# Attribute names read off the host, as (host attr, our global). Most match; the
# config pair is public on the host but private here.
_HOST_ATTRS = (
    ("_download_with_progress", "_download_with_progress"),
    ("_sha256_file", "_sha256_file"),
    ("load_cfg", "_load_cfg"),
    ("save_cfg", "_save_cfg"),
    ("BG", "BG"), ("SURF", "SURF"), ("BORDER", "BORDER"),
    ("AMBER", "AMBER"), ("AMBERDIM", "AMBERDIM"), ("PARCH", "PARCH"),
    ("MUTED", "MUTED"), ("GREEN", "GREEN"), ("RED", "RED"), ("CYAN", "CYAN"),
    ("_btn", "_btn"), ("_mk_tree", "_mk_tree"), ("_mk_scrollbar", "_mk_scrollbar"),
    ("_enable_dark_titlebar", "_enable_dark_titlebar"),
)


def set_helpers(host):
    """Wire this module to its host launcher, done once at startup. `host` is the
    launcher module (or any namespace) exposing the attributes in _HOST_ATTRS: the
    download/hash helpers, load_cfg/save_cfg, the colour palette, and the widget
    factories the community-mods windows use. Each is copied only if the host
    actually has it, so a caller can wire just part of the surface (the tests pass
    a namespace with only the download + hash helpers)."""
    g = globals()
    for src, dst in _HOST_ATTRS:
        if hasattr(host, src):
            g[dst] = getattr(host, src)


def _require(helper, name):
    if helper is None:
        raise ModManagerError(
            f"modmanager is missing its '{name}' helper. The launcher must call "
            f"modmanager.set_helpers(...) at startup before using mod features.")
    return helper


def _warn(msg):
    """Non-fatal problems (a skipped unknown-major manifest, an unreachable
    third-party repo) print to stderr rather than raising, so one bad source
    doesn't break the others. Kept tiny on purpose."""
    try:
        print(f"[modmanager] {msg}", file=sys.stderr)
    except Exception:
        pass


# Version + path primitives

def _parse_version(v):
    """Major-lock resolution compares versions constantly and the stdlib has no
    semver, so this is the one place versions become comparable.
    Splits "MAJOR.MINOR.PATCH" into an int tuple so ordering is numeric, not
    lexical. The trap this closes is that "1.10.0" < "1.9.0" as strings but
    1.10 > 1.9 as versions.

    Pre-release/build metadata ("1.2.0-rc1", "1.2.0+build") is NOT supported: a
    version that isn't exactly three numeric segments is rejected outright
    (below), so ordering never has to guess how to rank a pre-release tag."""
    if not isinstance(v, str):
        raise ModManagerError(f"Version must be a string, got {v!r}")
    parts = v.strip().split(".")
    if len(parts) != 3:
        raise ModManagerError(
            f"Version {v!r} isn't MAJOR.MINOR.PATCH: exactly three numeric "
            f"segments are required (no pre-release/build metadata).")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ModManagerError(
            f"Version {v!r} has a non-numeric segment; only digits are allowed.")


def _major(v):
    return _parse_version(v)[0]


def _safe_basename(name):
    """Last-line path-traversal guard, applied to mod.filename and every
    library_dependencies.filename right before it touches disk. Raises if name
    has any directory part, contains '/' or '\\', or is '.' / '..'.

    Re-checked here at write time regardless of any upstream validation: a
    manifest fetched from an unreviewed third-party repo can't be trusted to
    carry a clean filename, so install_mod/install_library_dependency verify it
    themselves."""
    if not name or name in (".", ".."):
        raise ModManagerError(f"Unsafe filename {name!r}.")
    if "/" in name or "\\" in name:
        raise ModManagerError(f"Filename {name!r} must not contain a path separator.")
    if os.path.basename(name) != name:
        raise ModManagerError(f"Filename {name!r} must be a bare basename.")
    return name


# Data model

@dataclass
class LibraryDependency:
    """A pinned, exact-version support assembly a mod links against, installed to
    UserLibs/. Concentus.dll, the codec a voice-chat mod needs, is the motivating
    case: a genuine third-party open-source project, not published by the mod
    author and not in this ecosystem's index at all. No id, no version-range
    resolution, no separate manifest, just "here's the exact file I tested
    against." Distinct from `dependencies`, which is for depending on other
    *mods*."""
    name: str            # display label only, not a resolvable id
    download_url: str
    sha256: str
    filename: str        # required; no id to default a name from


def parity_required(d):
    """Whether a client joining a server that runs this mod must match its exact
    version. Read off a manifest, an install record, an index entry, or a
    handshake entry - every shape the field travels in, so one place decides
    what it means.

    True is what the server enforces (TavernLib's ModParity.ValidateClient), for
    mods whose two halves are one system: a voice codec, a network protocol,
    anything where both sides exchange data they have to agree on. False means
    the server won't block a join over it, so the client is offered the mod,
    defaulted to installing, and may decline - for mods whose halves work
    independently, like a client performance tweak that happens to ship a server
    piece.

    Mandatory, with no default: every shape that carries this field is written
    by tooling that knows about it, so a missing one means the data predates the
    field or came from something that doesn't implement it, and guessing on its
    behalf is exactly what would let a required mod be silently treated as
    optional. Callers decide what to do with the error - a manifest becomes
    unresolvable, an index entry or an install record is skipped with a warning.

    Only a literal False relaxes it, so a present-but-malformed value (a string,
    a None, a 0) still reads as required rather than being trusted."""
    if "parity_required" not in d:
        raise ModManagerError(
            f"'{d.get('id', '?')}' has no parity_required field. It's required: "
            f"true if a client joining a server running this mod must have this "
            f"exact version, false if the server shouldn't block the join over "
            f"it. A mod installed before this field existed needs reinstalling.")
    return d["parity_required"] is not False


@dataclass
class ModManifest:
    manifest_version: int                   # schema version; unknown-major is skipped at parse
    id: str                                 # "<github-user>.<github-repo>", path-safe (single segment)
    name: str
    version: str                            # exactly "MAJOR.MINOR.PATCH"
    author: str
    description: str
    client_side: bool
    server_side: bool                       # at least one of client_side/server_side is True
    dependencies: dict                      # other MODS: id -> min version, SemVer major-locked
    library_dependencies: list             # pinned UserLibs/ files (list[LibraryDependency])
    download_url: str
    sha256: str
    source_repo: str                        # injected by fetch_indexes, NOT read from the file
    package: str = "dll"                    # "dll" (single file) or "zip" (extracted bundle)
    parity_required: bool = True            # see parity_required() above

    def install_name(self):
        """The basename this mod's single .dll lands under in Mods/<id>/. Nothing
        ever looks the file up by name: every operation works on the Mods/<id>/
        folder, so the file just keeps the name it was published under, taken from
        download_url. Two guards still apply, because a manifest from an unreviewed
        repo can't be trusted: _safe_basename rejects any traversal (a '..', a path
        separator, an empty/'.'/'..' name), and the result must end in '.dll' or
        MelonLoader won't load it; either failure falls back to f"{id}.dll" (id is
        already a validated safe segment). Only meaningful for package="dll"; a zip
        bundle names its own files."""
        base = os.path.basename(urlparse(self.download_url).path)
        try:
            safe = _safe_basename(base)
        except ModManagerError:
            safe = ""
        return safe if safe.lower().endswith(".dll") else f"{self.id}.dll"


@dataclass
class ModSummary:
    """One (id, major) row from a repository.json index. This is ALL the index
    carries now: enough to browse and to pick a version, but none of the
    install-critical data. download_url/sha256/dependencies live only in the
    per-version manifest (manifests/<author>/<repo>/<version>.json), which is
    fetched on demand, so those facts have exactly one home and can't drift
    between the index and the manifest. `versions` is every release in this major;
    the highest is the default install and max(versions) drives resolution."""
    id: str
    name: str
    author: str
    description: str
    client_side: bool
    server_side: bool
    versions: list                          # every version string in THIS major
    source_repo: str                        # injected by fetch_indexes
    parity_required: bool = True            # carried here too, so browsing can tell
                                             # a required mod from a recommended one

    def highest(self):
        return max(self.versions, key=_parse_version)

    @property
    def major(self):
        return _major(self.highest())


def _library_from_dict(d):
    for k in ("name", "download_url", "sha256", "filename"):
        if k not in d:
            raise ModManagerError(f"library_dependencies entry is missing '{k}'.")
    return LibraryDependency(
        name=str(d["name"]),
        download_url=str(d["download_url"]),
        sha256=str(d["sha256"]).lower(),
        filename=str(d["filename"]),
    )


def _manifest_from_dict(d, source_repo):
    """Builds a ModManifest from a parsed manifest object. Returns None (with a
    warning) if manifest_version's major isn't one we support; the caller drops
    it rather than mis-parsing a future shape. Raises ModManagerError only for a
    manifest that claims a supported version but is structurally broken."""
    mv = d.get("manifest_version")
    if not isinstance(mv, int):
        _warn(f"manifest for {d.get('id', '?')} has no integer manifest_version, skipped.")
        return None
    if mv != SUPPORTED_MANIFEST_MAJOR:
        _warn(f"manifest for {d.get('id', '?')} is schema v{mv}, this build "
              f"supports v{SUPPORTED_MANIFEST_MAJOR}, skipped.")
        return None

    package = str(d.get("package", "dll")).lower()
    if package not in ("dll", "zip"):
        _warn(f"manifest for {d.get('id', '?')} has unknown package type "
              f"{package!r} (expected 'dll' or 'zip'), skipped.")
        return None

    try:
        mod_id = str(d["id"])
        libs = [_library_from_dict(x) for x in d.get("library_dependencies", [])]
        return ModManifest(
            manifest_version=mv,
            id=mod_id,
            name=str(d.get("name", mod_id)),
            version=str(d["version"]),
            author=str(d.get("author", mod_id.split(".", 1)[0])),
            description=str(d.get("description", "")),
            client_side=bool(d["client_side"]),
            server_side=bool(d["server_side"]),
            dependencies=dict(d.get("dependencies", {}) or {}),
            library_dependencies=libs,
            download_url=str(d["download_url"]),
            sha256=str(d["sha256"]).lower(),
            source_repo=source_repo,
            package=package,
            parity_required=parity_required(d),
        )
    except KeyError as e:
        raise ModManagerError(f"manifest for {d.get('id', '?')} is missing field {e}.")


# Tiny HTTP JSON GET (repository.json / manifests only)
# NOT the file downloader; that's the host's _download_with_progress, with its
# wall-clock cap and progress reporting for large binaries. These are small
# text fetches, so a plain urlopen with a timeout is the right, minimal tool.

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


# Repo sources (config-backed, not GitHub calls)

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
    if _save_cfg is not None:
        _save_cfg(cfg)


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
    if _save_cfg is not None:
        _save_cfg(cfg)


# Index fetching / merging

# base URL -> (fetched_at, list[ModSummary]).  Per-base so one slow/broken repo
# doesn't invalidate the others' caches.
_index_cache = {}


def _summary_from_dict(d, mod_id, source_repo):
    """Builds a ModSummary from one repository.json (id, major) entry. Returns
    None (with a warning) if it has no usable versions; the index is discovery
    data, so a broken entry is skipped, not fatal."""
    raw = d.get("versions")
    if not isinstance(raw, list):
        _warn(f"index entry for {mod_id} in {source_repo} has no versions list, skipped.")
        return None
    versions = []
    for v in raw:
        try:
            _parse_version(v)
            versions.append(str(v))
        except ModManagerError:
            _warn(f"index entry for {mod_id} lists a bad version {v!r}, ignored.")
    if not versions:
        return None
    try:
        required = parity_required(d)
    except ModManagerError:
        # One malformed entry shouldn't cost the whole index. Skipped rather
        # than guessed at, same as an entry with no usable versions.
        _warn(f"index entry for {mod_id} has no parity_required field, skipped.")
        return None
    return ModSummary(
        id=mod_id,
        name=str(d.get("name", mod_id)),
        author=str(d.get("author", mod_id.split(".", 1)[0])),
        description=str(d.get("description", "")),
        client_side=bool(d.get("client_side", False)),
        server_side=bool(d.get("server_side", False)),
        versions=versions,
        source_repo=source_repo,
        parity_required=required,
    )


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


# Dependency resolution

def resolve_dependencies(roots, summary_index, fetch_manifest):
    """Resolves the full dependency *closure* of one OR MORE root mods, returning
    a flat, deduplicated list of ModManifests (the roots themselves not included;
    the caller already has them).

    Two inputs beyond the roots:
      - summary_index: the collision-resolved list[ModSummary] from fetch_indexes.
        Used only to PICK a dependency's version within a major (the index knows
        which versions exist; the manifest itself is not in the index).
      - fetch_manifest(id, version, source_repo) -> ModManifest: loads the chosen
        version's manifest on demand, so its own dependencies/libraries can be
        walked. Only manifests actually in the closure are fetched.

    Taking a LIST of roots is deliberate: the point of diamond-conflict detection
    is catching when two *independent* required mods (e.g. every mod a server
    requires) pull in incompatible versions of a shared dependency. A single-mod
    install just passes [mod].

    Minimum-Required-Version-with-Major-Lock: for each id -> min_version, keep the
    versions the index lists in the required major that are >= the running
    minimum, take the highest, and fetch that one. This picks a version within a
    major; it does NOT re-arbitrate repos (fetch_indexes already did).

    Three failure modes, each raising a specific, actionable message:
      - Circular dependency (A -> B -> A): detected via the chain of ids being
        resolved; raises naming the cycle.
      - Chain too deep: a strictly acyclic chain longer than MAX_DEPENDENCY_DEPTH.
      - Diamond conflict: two branches need the same id at incompatible majors;
        raises naming both requiring mods and both constraints."""
    summ = {}            # (id, major) -> ModSummary
    for s in summary_index:
        summ[(s.id, s.major)] = s

    root_ids = {r.id for r in roots}
    chosen = {}          # id -> ModManifest currently selected
    req_major = {}       # id -> locked major
    req_min = {}         # id -> highest min-version tuple required so far
    req_by = {}          # id -> (mod name, constraint str) of first requirer, for messages

    def add_requirement(dep_id, min_v, requirer, chain):
        try:
            mv = _parse_version(min_v)
        except ModManagerError as e:
            raise ModManagerError(
                f"{requirer} requires {dep_id} at an invalid version {min_v!r}: {e}")
        major = mv[0]

        if dep_id in req_major and req_major[dep_id] != major:
            prev_name, prev_c = req_by[dep_id]
            raise ModManagerError(
                f"Dependency conflict on '{dep_id}': {prev_name} needs {prev_c} "
                f"(major {req_major[dep_id]}) but {requirer} needs {min_v} "
                f"(major {major}). These majors can't be satisfied together.")

        new_min = mv if dep_id not in req_min else max(req_min[dep_id], mv)
        req_major[dep_id] = major
        req_min[dep_id] = new_min
        req_by.setdefault(dep_id, (requirer, min_v))

        summary = summ.get((dep_id, major))
        available = summary.versions if summary else []
        candidates = [v for v in available if _parse_version(v) >= new_min]
        if not candidates:
            have = ", ".join(sorted(available)) or "none"
            raise ModManagerError(
                f"{requirer} needs '{dep_id}' >= {'.'.join(map(str, new_min))} "
                f"(major {major}), but no such version is available "
                f"(available: {have}). Is the right repository added?")
        pick_version = max(candidates, key=_parse_version)

        prev = chosen.get(dep_id)
        if prev is None or prev.version != pick_version:
            manifest = fetch_manifest(dep_id, pick_version, summary.source_repo)
            chosen[dep_id] = manifest
            visit(manifest, chain + [dep_id])

    def visit(mod, chain):
        if len(chain) > MAX_DEPENDENCY_DEPTH:
            raise ModManagerError(
                f"Dependency chain too deep (> {MAX_DEPENDENCY_DEPTH}): "
                f"{' -> '.join(chain)}. This is almost certainly a mistake in the "
                f"mods' dependency data.")
        for dep_id, min_v in sorted((mod.dependencies or {}).items()):
            if dep_id in chain:
                raise ModManagerError(
                    f"Circular dependency: {' -> '.join(chain + [dep_id])}.")
            add_requirement(dep_id, min_v, mod.name or mod.id, chain)

    for root in roots:
        visit(root, [root.id])

    # The roots themselves are the caller's; never hand them back even if one
    # root happens to be a dependency of another.
    return [m for mid, m in chosen.items() if mid not in root_ids]


def _repo_order(repo_bases, prefer_repo):
    """prefer_repo first (the repo a previous fetch/install already used, the
    common case with no searching), then the rest of repo_bases, de-duplicated."""
    order = []
    if prefer_repo:
        order.append(prefer_repo)
    for b in repo_bases:
        if _norm(b) not in [_norm(x) for x in order]:
            order.append(b)
    return order


def _fetch_manifest_leaf(repo_bases, mod_id, leaf, prefer_repo=None, what=None):
    """Fetches one manifest file (leaf, e.g. "1.0.4.json" or "latest.1.json") from
    manifests/<author>/<repo>/ across the configured repos in preference order,
    returning the first that parses. <author>/<repo> is mod_id split on the first
    '.', which requires mod_id to be a well-formed '<github-user>.<github-repo>'
    (guarded just below). Raises if no repo has it."""
    if "." not in mod_id:
        raise ModManagerError(
            f"Mod id {mod_id!r} isn't '<github-user>.<github-repo>'.")
    author, repo = mod_id.split(".", 1)
    last_err = None
    for base in _repo_order(repo_bases, prefer_repo):
        url = f"{_norm(base)}/manifests/{author}/{repo}/{leaf}"
        try:
            data = _get_json(url)
        except ModManagerError as e:
            last_err = e
            continue
        m = _manifest_from_dict(data, _norm(base))
        if m is not None:
            return m
    raise ModManagerError(
        f"Couldn't find {what or mod_id} in any configured repository."
        + (f" Last error: {last_err}" if last_err else ""))


def _fetch_manifest(repo_bases, mod_id, version, prefer_repo=None):
    """Loads the full manifest for an EXACT version, the on-demand fetch the slim
    index leans on: the index names which versions exist, this pulls the one
    chosen. Immutable per-version file, so it's safe to cache within a resolve."""
    return _fetch_manifest_leaf(
        repo_bases, mod_id, f"{version}.json", prefer_repo,
        what=f"mod '{mod_id}' version {version}")


def resolve_mod_by_id(repo_bases, mod_id, prefer_repo=None, major=None):
    """Fetches a single mod's manifest by id via the latest*.json pointer files,
    without downloading the full repository.json. If major is given, fetches
    latest.<major>.json; otherwise latest.json."""
    leaf = f"latest.{major}.json" if major is not None else "latest.json"
    what = f"mod '{mod_id}'" + (f" (major {major})" if major is not None else "")
    return _fetch_manifest_leaf(repo_bases, mod_id, leaf, prefer_repo, what=what)


# Installation

# The record filename inside each mod folder. MUST be exactly "manifest.json":
# recent MelonLoader only recurses into a Mods/ subfolder that contains a file by
# this name (existence check only, content unread), so this record is also the
# marker that makes the folder load. See the module docstring.
RECORD_NAME = "manifest.json"

# A disabled mod keeps its folder and files but has its record renamed to this,
# so the folder no longer contains a "manifest.json" and MelonLoader stops
# scanning it (a first-level Mods/ subfolder with no manifest.json isn't loaded),
# while this module still finds the folder by <id> and reads the record. Enabling
# renames it back. The folder is NOT renamed, so the by-id path stays stable and
# the folder isn't hidden from listing the way a dot-prefixed one would be.
DISABLED_RECORD_NAME = "manifest.disabled.json"


def _mods_base(game_dir):
    # The Mods/ path WITHOUT creating it, for building paths in read-only code
    # (status/listing) that shouldn't have the side effect of making folders.
    return os.path.join(game_dir, "Mods")


def _mods_dir(game_dir):
    d = _mods_base(game_dir)
    os.makedirs(d, exist_ok=True)
    return d


def _userlibs_dir(game_dir):
    d = os.path.join(game_dir, "UserLibs")
    os.makedirs(d, exist_ok=True)
    return d


def _mod_dir_path(game_dir, mod_id):
    # A mod's own folder: Mods/<id>/. mod_id is guarded as a single path segment
    # (an id from an unreviewed repo can't be trusted to be path-safe).
    return os.path.join(_mods_base(game_dir), _safe_basename(mod_id))


def _mod_record_path(game_dir, mod_id):
    # The record + MelonLoader marker inside the mod's folder (enabled state).
    return os.path.join(_mod_dir_path(game_dir, mod_id), RECORD_NAME)


def _disabled_record_path(game_dir, mod_id):
    # Where the record lives while the mod is disabled (no manifest.json present,
    # so MelonLoader skips the folder).
    return os.path.join(_mod_dir_path(game_dir, mod_id), DISABLED_RECORD_NAME)


def _staging_path(game_dir, mod_id):
    # Where a mod is assembled before being swapped into place atomically. Dot-
    # prefixed so MelonLoader ignores it even if a crash leaves one behind.
    return os.path.join(_mods_base(game_dir), f".{_safe_basename(mod_id)}.installing")


def _library_sidecar_path(game_dir, filename):
    # A library's record sits in UserLibs/ beside its .dll, keyed by filename
    # (libraries have no id). Kept next to the artifact it describes so each
    # folder is self-contained.
    return os.path.join(_userlibs_dir(game_dir), f"{filename}.meta.json")


def _write_sidecar(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# Malware scanning happens at the source, not on each user's machine.
# CommunityMods/tools/modindex.py VirusTotal-scans every submitted file, and
# every code-bearing member of a zip bundle, before it can merge into the index,
# backed by human PR review. The sha256 check below is what ties the bytes on
# this disk to the ones that were scanned and reviewed.
#
# A repo the user added themselves is covered by that hash check and the
# add-time warning only. Nothing scans it, which is the trade the
# unverified-source warning exists to state plainly.


@contextlib.contextmanager
def _verified_download(url, sha256, work_dir, on_progress):
    """Download `url` to a temp file inside `work_dir` and verify its sha256,
    then hand the temp path to the caller to move (single file) or extract
    (zip). The temp is always created in work_dir, never the system temp dir, so
    a follow-up os.replace within the same tree is a same-volume atomic rename;
    routing through %TEMP% (usually C:) would make that a cross-volume move,
    which fails with WinError 17 when the game is on another drive. The temp is
    always cleaned up, so a failed verify/move leaves nothing behind."""
    download = _require(_download_with_progress, "_download_with_progress")
    hashfn = _require(_sha256_file, "_sha256_file")

    os.makedirs(work_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tavern_dl_", suffix=".part", dir=work_dir)
    os.close(fd)
    try:
        download(url, tmp, on_progress)
        actual = hashfn(tmp)
        if actual.lower() != (sha256 or "").lower():
            raise ModManagerError(
                f"Downloaded file failed its checksum: expected {sha256}, got "
                f"{actual}. The file may be corrupted or tampered with; nothing "
                f"was installed.")
        yield tmp
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _download_verify_move(url, sha256, dest_path, on_progress):
    """Download, verify, then atomically move one file into dest_path. Never
    leaves a half-written or unverified file behind. The temp lives in
    dest_path's own directory so the move is a same-volume rename."""
    dest_dir = os.path.dirname(dest_path)
    with _verified_download(url, sha256, dest_dir, on_progress) as tmp:
        os.replace(tmp, dest_path)


def _safe_extract_zip(zip_path, dest_dir):
    """Extract a .zip into dest_dir, refusing any member that would escape it
    (zip-slip) or that is a symlink, and capping entry count + total decompressed
    size so a decompression bomb can't fill the disk. This is the one new attack
    surface bundles add, and it matters because a mod can come from an unreviewed
    source: a member named '..\\..\\evil.dll' or an absolute path would otherwise
    write anywhere on disk. Every member's resolved path must stay within dest_dir,
    and the running decompressed total must stay under _ZIP_MAX_TOTAL_UNCOMPRESSED;
    the first unsafe/oversized entry raises, having written only whatever came
    before it (the caller builds into a throwaway staging dir, so a raise discards
    the partial extract)."""
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise ModManagerError(
                f"This mod archive has {len(infos)} entries, over the "
                f"{_ZIP_MAX_ENTRIES} limit; refusing to extract it.")
        written = 0
        for info in infos:
            name = info.filename
            # Absolute paths and drive letters never belong in a mod archive.
            if name.startswith(("/", "\\")) or (len(name) >= 2 and name[1] == ":"):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: absolute path.")
            # Symlinks (unix mode in the high 16 bits of external_attr) could
            # point outside the folder; refuse them outright.
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: symlink.")
            rel = name.replace("/", os.sep).replace("\\", os.sep)
            target = os.path.realpath(os.path.join(dest_root, rel))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: escapes the mod folder.")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Copy in chunks against the ACTUAL decompressed size, not the header's
            # declared file_size (which a bomb can understate), aborting the moment
            # the running total crosses the cap.
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _ZIP_MAX_TOTAL_UNCOMPRESSED:
                        raise ModManagerError(
                            f"This mod archive extracts to more than "
                            f"{_ZIP_MAX_TOTAL_UNCOMPRESSED // (1024 * 1024)} MB; "
                            f"refusing to extract it (possible zip bomb).")
                    dst.write(chunk)


def _mod_record(mod):
    """The record written to Mods/<id>/manifest.json: the fetched manifest
    verbatim (so status/uninstall need no re-fetch) plus install fields. Kept a
    plain JSON object on purpose: it's also MelonLoader's folder marker, and a
    future MelonLoader that parses it shouldn't choke. Keep this shape stable so
    any other installer can read it too.

    Note there is NO placed-dll filename here: every operation works on the
    Mods/<id>/ folder (install swaps it, uninstall deletes it, update replaces it),
    so nothing ever needs to find the .dll by name; recording it would be dead
    weight."""
    return {
        "manifest_version": mod.manifest_version,
        "id": mod.id,
        "name": mod.name,
        "version": mod.version,
        "author": mod.author,
        "description": mod.description,
        "client_side": mod.client_side,
        "server_side": mod.server_side,
        # Recorded so a server can report it in the handshake without re-fetching
        # the manifest, and so the client can tell what a mod it already has is.
        "parity_required": mod.parity_required,
        "dependencies": dict(mod.dependencies or {}),
        "library_dependencies": [
            {"name": l.name, "download_url": l.download_url,
             "sha256": l.sha256, "filename": l.filename}
            for l in (mod.library_dependencies or [])],
        "download_url": mod.download_url,
        "sha256": mod.sha256,
        "source_repo": mod.source_repo,
        "package": mod.package,
        # The libraries this mod pins, by filename. uninstall_mod uses these to
        # reference-count UserLibs/ files so an orphaned library is removed while
        # one another installed mod still lists stays put.
        "libraries": [_safe_basename(l.filename)
                      for l in (mod.library_dependencies or [])],
    }


def _reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def install_mod(game_dir, mod, on_progress):
    """Installs one mod into its own folder, Mods/<id>/, and writes the record +
    MelonLoader marker Mods/<id>/manifest.json inside it.

    package="dll": mod.download_url is a single .dll placed under the name it was
    published with (mod.install_name(), traversal-guarded); nothing looks it up by
    name afterwards.
    package="zip": it's an archive extracted into Mods/<id>/, so the mod can ship
    several DLLs plus Unity asset bundles / addressables. Either way one artifact
    is downloaded and verified against mod.sha256 before anything is written.

    The mod is assembled in a throwaway staging folder and swapped into place only
    once it's complete, so a failed download/verify/extract never leaves a partial
    or half-upgraded mod folder, and any pre-existing install is replaced
    atomically."""
    mod_dir = _mod_dir_path(game_dir, mod.id)
    staging = _staging_path(game_dir, mod.id)
    _reset_dir(staging)
    try:
        if mod.package == "zip":
            with _verified_download(mod.download_url, mod.sha256, staging, on_progress) as tmp:
                _safe_extract_zip(tmp, staging)
        else:
            _download_verify_move(mod.download_url, mod.sha256,
                                  os.path.join(staging, mod.install_name()), on_progress)
        _write_sidecar(os.path.join(staging, RECORD_NAME), _mod_record(mod))

        # Swap staging -> Mods/<id>/ atomically, clearing any prior install first.
        if os.path.isdir(mod_dir):
            shutil.rmtree(mod_dir)
        os.replace(staging, mod_dir)
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def collect_library_dependencies(mods):
    """Gathers every library_dependencies entry across a resolved set of mods
    (typically the top-level mod plus resolve_dependencies' result), deduplicated
    by filename (no id to graph by, so filename stands in for identity). Same
    filename + matching sha256/download_url collapses to one (install once); same
    filename + different sha256/download_url raises a conflict naming both mods
    and both URLs, the library equivalent of a diamond conflict, by exact match
    since there's no version-range concept for a pinned third-party file."""
    by_filename = {}     # filename -> (LibraryDependency, owning mod name)
    for mod in mods:
        for lib in (mod.library_dependencies or []):
            key = _safe_basename(lib.filename)
            if key not in by_filename:
                by_filename[key] = (lib, mod.name)
                continue
            existing, owner = by_filename[key]
            if (existing.sha256.lower() != lib.sha256.lower()
                    or _norm(existing.download_url) != _norm(lib.download_url)):
                raise ModManagerError(
                    f"Library conflict on '{lib.filename}': {owner} pins "
                    f"{existing.download_url} (sha {existing.sha256[:12]}...) but "
                    f"{mod.name} pins {lib.download_url} (sha {lib.sha256[:12]}...). "
                    f"They can't both be installed.")
    return [lib for (lib, _owner) in by_filename.values()]


def install_library_dependency(game_dir, lib, on_progress):
    """Same shape as install_mod: downloads lib.download_url, verifies against
    lib.sha256, writes <game_dir>/UserLibs/<safe filename>. Sidecar goes to
    <game_dir>/UserLibs/<filename>.meta.json, beside the .dll it describes
    (filename is the key, no id) recording {filename, sha256, download_url}: no
    source_repo (never came from a configured index) and no version (nothing to
    compare against; a library is either at its pinned hash or needs reinstalling,
    there's no "outdated" state for it)."""
    name = _safe_basename(lib.filename)
    dest = os.path.join(_userlibs_dir(game_dir), name)
    _download_verify_move(lib.download_url, lib.sha256, dest, on_progress)
    _write_sidecar(_library_sidecar_path(game_dir, name), {
        "filename": name,
        "sha256": lib.sha256,
        "download_url": lib.download_url,
    })


def install_mod_closure(game_dir, mod, index, repo_bases, on_progress):
    """Installs a mod AND its full closure, the flow a single COMMUNITY MODS
    click runs. `mod` is the ModSummary the user picked; its highest version's
    manifest is fetched, then resolve_dependencies walks the graph fetching each
    dependency's manifest, and collect_library_dependencies gathers the pinned
    libraries. Every cycle/depth/diamond/library conflict is surfaced (and every
    manifest fetched) BEFORE anything touches disk. A mod with no dependencies is
    just the degenerate case: empty closure, one install_mod.

    Manifests fetched during a single closure are cached, so a diamond (two mods
    needing the same dependency) fetches it once."""
    fetched = {}     # (id, version) -> ModManifest

    def fetch(mod_id, version, source_repo):
        key = (mod_id, version)
        if key not in fetched:
            fetched[key] = _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)
        return fetched[key]

    root = fetch(mod.id, mod.highest(), mod.source_repo)
    deps = resolve_dependencies([root], index, fetch)
    all_mods = [root] + deps
    libs = collect_library_dependencies(all_mods)     # raises on conflict before any download

    total = len(all_mods) + len(libs)
    step = {"n": 0}

    def stepped(label):
        def report(msg):
            on_progress(f"[{step['n']}/{total}] {label}: {msg}")
        return report

    for m in all_mods:
        step["n"] += 1
        install_mod(game_dir, m, stepped(m.name))
    for lib in libs:
        step["n"] += 1
        install_library_dependency(game_dir, lib, stepped(lib.name))


def uninstall_mod(game_dir, mod_id):
    """Removes an installed community mod: its whole Mods/<id>/ folder. Each
    library the mod pinned is then removed from UserLibs/ too, UNLESS another
    still-installed mod's record lists it: shared libraries stay, orphaned ones go.
    Returns True if anything was deleted, False if nothing was installed under that
    id."""
    meta = _read_mod_record(game_dir, mod_id)
    removed = False
    libs = [_safe_basename(str(x)) for x in (meta.get("libraries") if meta else []) or []]

    mod_dir = _mod_dir_path(game_dir, mod_id)
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        removed = True

    if libs:
        # After removing this mod's record, gather what the remaining mods still
        # pin, then drop any library nobody else needs.
        still_needed = set()
        for other in list_installed_mods(game_dir):
            for x in other.get("libraries", []):
                still_needed.add(_safe_basename(str(x)))
        for lib in libs:
            if lib in still_needed:
                continue
            if _remove_library(game_dir, lib):
                removed = True
    return removed


def _remove_library(game_dir, filename):
    """Delete a UserLibs/ library and its sidecar."""
    removed = False
    for path in (os.path.join(_userlibs_dir(game_dir), filename),
                 _library_sidecar_path(game_dir, filename)):
        if os.path.isfile(path):
            os.remove(path)
            removed = True
    return removed


# Status / listing

def _read_sidecar(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_mod_record(game_dir, mod_id):
    """The install record for a mod, from whichever state it's in: the enabled
    folder record (Mods/<id>/manifest.json), then the disabled one
    (Mods/<id>/manifest.disabled.json). A record is only accepted if it carries an
    "id" (a bare manifest.json marker or a library sidecar without one isn't a mod
    record). Returns None if neither has it."""
    for path in (_mod_record_path(game_dir, mod_id),
                 _disabled_record_path(game_dir, mod_id)):
        rec = _read_sidecar(path)
        if rec and rec.get("id"):
            return rec
    return None


def disable_mod(game_dir, mod_id):
    """Disable an installed folder-layout mod WITHOUT uninstalling it: rename its
    Mods/<id>/manifest.json to manifest.disabled.json. The folder then has no
    manifest.json, so MelonLoader stops scanning it and the mod stops loading,
    while every file stays in place. Returns True if it was enabled and is now
    disabled; False if there was nothing enabled to disable (already disabled or
    not installed)."""
    src = _mod_record_path(game_dir, mod_id)
    if not os.path.isfile(src):
        return False
    os.replace(src, _disabled_record_path(game_dir, mod_id))
    return True


def enable_mod(game_dir, mod_id):
    """Re-enable a disabled mod: rename manifest.disabled.json back to
    manifest.json so MelonLoader scans the folder again. Returns True if it was
    disabled and is now enabled; False if there was nothing disabled to enable."""
    src = _disabled_record_path(game_dir, mod_id)
    if not os.path.isfile(src):
        return False
    os.replace(src, _mod_record_path(game_dir, mod_id))
    return True


def mod_status(game_dir, mod_id, index):
    """missing / outdated / current / unknown, same shape as
    _melonloader_status, read from the mod's install record (Mods/<id>/manifest.json
    or its disabled variant; see _read_mod_record).

    "outdated" = the installed version is lower than the highest available across
    all repos (any major counts; a new major still shows as an available update,
    since the user is prompted before any update actually applies, so surfacing
    it is informative, not automatic). "current" = it equals that
    highest. "unknown" = installed but no longer present in any configured index
    (e.g. its repo was removed), nothing to compare against."""
    meta = _read_mod_record(game_dir, mod_id)
    if not meta or "version" not in meta:
        return "missing"
    available = [s for s in index if s.id == mod_id]
    if not available:
        return "unknown"
    highest = max(_parse_version(v) for s in available for v in s.versions)
    try:
        installed = _parse_version(meta["version"])
    except ModManagerError:
        return "unknown"
    return "current" if installed >= highest else "outdated"


def list_mods(index, side):
    """The merged index (list[ModSummary]) collapsed to one row per id (highest
    major), filtered to the caller's side: side="client" keeps client_side mods,
    side="server" keeps server_side ones; att_client and att_server each pass
    their own, so a server-only mod never shows in the client's browse list and
    vice versa. The only listing accessor the UI needs (libraries never appear in
    an index; they're inline library_dependencies on a parent manifest)."""
    best = {}    # id -> ModSummary with the highest version seen
    for s in index:
        if side == "client" and not s.client_side:
            continue
        if side == "server" and not s.server_side:
            continue
        cur = best.get(s.id)
        if cur is None or _parse_version(s.highest()) > _parse_version(cur.highest()):
            best[s.id] = s
    return sorted(best.values(), key=lambda s: s.name.lower())


def list_installed_mods(game_dir):
    """Every installed mod's record, in both enabled/disabled states: the per-mod
    folders (enabled Mods/<id>/manifest.json or disabled
    Mods/<id>/manifest.disabled.json). A record counts only if it carries an "id"
    and a "parity_required", so a library sidecar, a bare marker, or a record
    written before parity_required existed is ignored. Each returned record gains
    an in-memory "enabled" value, not written to disk: True (loaded) or False
    (disabled). Every installed mod's client_side/server_side flags are available
    from these records, so this is the read side for both the UI's status refresh
    and whatever assembles the server's mod list for the pre-join check.
    uninstall_mod also uses it to see which libraries the remaining mods still
    pin.

    A pre-parity_required record is skipped rather than assumed required,
    because assuming is what would let a mod the server actually requires be
    reported as something a client may decline. Skipping is recoverable: the mod
    reads as not installed, so the next plan reinstalls it and the new record
    carries the field."""
    out = {}     # id -> record
    mods_dir = _mods_base(game_dir)
    if not os.path.isdir(mods_dir):
        return []
    for name in os.listdir(mods_dir):
        full = os.path.join(mods_dir, name)
        if not os.path.isdir(full):
            continue
        # Skip our own staging/ignore folders (dot/tilde).
        if name.startswith((".", "~")):
            continue
        rec = _read_sidecar(os.path.join(full, RECORD_NAME))
        enabled = True
        if not (rec and rec.get("id")):
            rec = _read_sidecar(os.path.join(full, DISABLED_RECORD_NAME))
            enabled = False
        if rec and rec.get("id"):
            if "parity_required" not in rec:
                _warn(f"installed mod {rec['id']} has a record with no "
                      f"parity_required field, so it predates that field and is "
                      f"being ignored; reinstall it to fix.")
                continue
            rec["enabled"] = enabled       # in-memory only; not on disk
            out[rec["id"]] = rec
    return list(out.values())


def handshake_snapshot(game_dir):
    """Fingerprint of every currently-ENABLED installed mod, for the ping/pong
    mod-sync check. Disabled mods are excluded: they aren't loaded, so a
    joining client shouldn't be asked to match them. Returns
    (mods_hash, mods_count, mods_list):
    - mods_hash: sha256 of the sorted "id@version" list, joined with newlines.
      A client caches this per server; an unchanged hash means it already has
      the full list and skips the extra round trip.
    - mods_count: len(mods_list), sent alongside the hash in the pong so a
      client can sanity-check its cache without decoding anything.
    - mods_list: every entry as {"id", "version", "client_side", "server_side",
      "parity_required", "source_repo"}, sorted by id: what the separate,
      length-prefixed
      "mods_list" request sends on a cache miss. source_repo is a HINT only:
      plan_join uses it to tell a client which repo a required
      mod not resolvable from any of the client's configured repos would come
      from, so the user can add it explicitly; it's never resolved into a
      pull on its own, same rule as everywhere else this field appears (the
      modlist's `repos`, the rejection payload's `missing` entries)."""
    mods = sorted((m for m in list_installed_mods(game_dir) if m.get("enabled")),
                  key=lambda m: m["id"])
    # parity_required is part of the fingerprint, not just the list. A server
    # can flip a mod between required and recommended without its version
    # moving, and a client caches this whole list against this hash - so leaving
    # it out would let a client keep planning against the old answer until it
    # restarted, and skip a mod that had since become mandatory.
    fingerprint = "\n".join(
        f"{m['id']}@{m.get('version', '')}@{'req' if parity_required(m) else 'opt'}"
        for m in mods)
    mods_hash = hashlib.sha256(fingerprint.encode()).hexdigest()
    mods_list = [
        {"id": m["id"], "version": m.get("version", ""),
         "client_side": bool(m.get("client_side", False)),
         "server_side": bool(m.get("server_side", False)),
         "parity_required": parity_required(m),
         "source_repo": m.get("source_repo", "")}
        for m in mods
    ]
    return mods_hash, len(mods_list), mods_list


# Client mod cache
#
# A single client joins many servers with different required mods, so with
# the render step wired in, Mods/ stops being permanent storage and becomes a
# per-launch rendering of one server's required set.
# Downloaded mods live here instead, keyed by id+version so multiple versions
# of the same mod coexist - switching servers moves files instead of
# re-downloading. Libraries are keyed by filename+sha256, not filename alone,
# since two different mods can pin the same filename at different exact
# builds and both need to stay cached.

def _cache_base():
    return os.path.join(_tavern_data_dir(), "mod_cache")


def _cache_mod_dir(mod_id, version):
    return os.path.join(_cache_base(), _safe_basename(mod_id), _safe_basename(version))


def _cache_library_dir(filename, sha256):
    return os.path.join(_cache_base(), "_libraries", _safe_basename(filename), sha256)


def cache_store_mod(game_dir, mod_id):
    """Copies a mod currently on disk (Mods/<id>/, enabled or disabled) into
    the cache under its recorded version, if not already there. Returns the
    version stored, or None if the mod isn't actually installed. Safe to call
    repeatedly - a version already in the cache is left untouched."""
    rec = _read_mod_record(game_dir, mod_id)
    if not rec or not rec.get("version"):
        return None
    version = rec["version"]
    dest = _cache_mod_dir(mod_id, version)
    if os.path.isdir(dest):
        return version

    src = _mod_dir_path(game_dir, mod_id)
    staging = dest + ".caching"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    # The cache doesn't track enabled/disabled (that's a Mods/-only concept) -
    # normalize to the plain record name so cache_restore_mod always finds it.
    disabled = os.path.join(staging, DISABLED_RECORD_NAME)
    if os.path.isfile(disabled):
        os.replace(disabled, os.path.join(staging, RECORD_NAME))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(staging, dest)
    return version


def cache_store_library(game_dir, filename):
    """Copies a UserLibs/ library currently on disk into the cache under its
    recorded sha256, if not already there. Returns the sha256 stored, or None
    if the library or its sidecar isn't actually present."""
    src = os.path.join(_userlibs_dir(game_dir), filename)
    sidecar = _library_sidecar_path(game_dir, filename)
    meta = _read_sidecar(sidecar)
    if not os.path.isfile(src) or not meta or not meta.get("sha256"):
        return None
    sha256 = meta["sha256"]
    dest = _cache_library_dir(filename, sha256)
    if os.path.isdir(dest):
        return sha256

    staging = dest + ".caching"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    shutil.copy2(src, os.path.join(staging, filename))
    shutil.copy2(sidecar, os.path.join(staging, f"{filename}.meta.json"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(staging, dest)
    return sha256


def clear_mod_cache():
    """Deletes the entire client mod cache (every cached mod version and
    library). Mods/ itself is untouched - this only resets the
    switch-servers-without-redownloading optimization, forcing the next
    install or join to fetch fresh from source instead of reusing a local
    copy. The fix for a cache entry that's gone stale, e.g. one written
    before a manifest field parity_required() now requires existed - since
    the cache is keyed on id+version alone, a version bump upstream is the
    only thing that normally invalidates an entry."""
    base = _cache_base()
    if os.path.isdir(base):
        shutil.rmtree(base)


def adopt_installed_mods(game_dir):
    """Copies everything currently installed in Mods/ (and the UserLibs/
    libraries they pin) into the cache, without touching Mods/ itself.
    Idempotent - already-cached versions are skipped - so it's safe to call on
    every join; att_client calls it from _reconcile_mods_before_launch, before
    any plan is computed or applied.

    Running it before a render is what makes the cache's "switching servers is
    a file move, never a re-download" promise actually hold. Anything that
    installed straight to Mods/ (the Community Mods window, or an older
    launcher with no cache) is otherwise unknown to the cache, so rendering a
    different version over it destroys the only copy of the version that was
    there. Returns the list of mod ids newly adopted (already-cached ones don't
    count)."""
    adopted = []
    for rec in list_installed_mods(game_dir):
        version = rec.get("version")
        if not version:
            continue
        already_cached = os.path.isdir(_cache_mod_dir(rec["id"], version))
        if cache_store_mod(game_dir, rec["id"]) and not already_cached:
            adopted.append(rec["id"])
        for filename in rec.get("libraries", []):
            cache_store_library(game_dir, filename)
    return adopted


def cache_restore_mod(game_dir, mod_id, version):
    """Copies a cached mod version into Mods/<id>/, enabled, replacing
    whatever's currently there (assembled in staging, swapped in atomically -
    same pattern install_mod uses). Raises if that exact version isn't
    cached."""
    src = _cache_mod_dir(mod_id, version)
    if not os.path.isdir(src):
        raise ModManagerError(f"'{mod_id}' {version} isn't in the local cache.")

    dest = _mod_dir_path(game_dir, mod_id)
    staging = _staging_path(game_dir, mod_id)
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(_mods_base(game_dir), exist_ok=True)
    os.replace(staging, dest)


def cache_restore_library(game_dir, filename, sha256):
    """Copies a cached library (and its sidecar) into UserLibs/, unless it's
    already there with a matching hash. Raises if that exact
    filename+sha256 isn't cached."""
    dest = os.path.join(_userlibs_dir(game_dir), filename)
    existing = _read_sidecar(_library_sidecar_path(game_dir, filename))
    if os.path.isfile(dest) and existing and existing.get("sha256") == sha256:
        return

    src = _cache_library_dir(filename, sha256)
    if not os.path.isdir(src):
        raise ModManagerError(f"Library '{filename}' ({sha256[:12]}...) isn't in the local cache.")
    shutil.copy2(os.path.join(src, filename), dest)
    shutil.copy2(os.path.join(src, f"{filename}.meta.json"), _library_sidecar_path(game_dir, filename))


# Pre-join reconciliation: plan_join
#
# plan_join is the resolution CORE only: given the server's required mods, it
# computes what the active set should be and diffs it against what's already
# cached/active. It never touches disk beyond read-only lookups (what's
# cached, what's currently enabled in Mods/) and never fetches, installs, or
# uninstalls anything itself - applying a JoinPlan is render_active_set.

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
    it's a write not a merge. Saves via the injected save_cfg, same convention
    as add_repo/add_pin. A server with nothing declined drops out entirely
    rather than leaving an empty list behind."""
    save = _require(_save_cfg, "save_cfg")
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
    save_cfg, same convention as add_repo/remove_repo."""
    entry = (entry or "").strip()
    if not entry:
        raise ModManagerError("Enter a mod id, e.g. Author.ModId or Author.ModId@1.2.0.")
    mod_id, _version = _parse_pin_entry(entry)
    if not mod_id:
        raise ModManagerError(f"'{entry}' isn't a valid mod id.")
    pinned = [p for p in list_pinned(cfg) if _parse_pin_entry(p)[0] != mod_id]
    pinned.append(entry)
    cfg[CFG_PINNED_KEY] = pinned
    if _save_cfg is not None:
        _save_cfg(cfg)


def remove_pin(cfg, mod_id):
    """Removes a mod's pin by id (with or without an @version suffix)."""
    mod_id, _version = _parse_pin_entry(mod_id)
    cfg[CFG_PINNED_KEY] = [p for p in list_pinned(cfg) if _parse_pin_entry(p)[0] != mod_id]
    if _save_cfg is not None:
        _save_cfg(cfg)


@dataclass
class PlanEntry:
    """One mod in the computed active set."""
    mod_id: str
    version: str
    source_repo: str
    reason: str        # "required" (server needs it) | "dependency" | "pinned"
    cached: bool       # already in the local cache at this exact version
    active: bool       # Mods/<id>/ is already enabled at exactly this version - a no-op
    manifest: object   # the resolved ModManifest - render_active_set installs straight
                        # from this rather than re-fetching it a second time


@dataclass
class JoinPlan:
    entries: list           # list[PlanEntry] - the full computed active set
    to_deactivate: list      # mod ids enabled in Mods/ that aren't in the active set
    libraries: list         # list[LibraryDependency] the active set needs
    missing: list           # [(mod_id, version)] required by the server, unresolvable from
                             # any repo the client has configured - blocks launching
    needs_repo: list        # [(mod_id, version, source_repo)] resolvable, but only from a
                             # repo the client hasn't added - not blocking on
                             # its own; surfaced so the user can add the repo and re-check
    pin_conflicts: list      # [(mod_id, pinned_version, server_version)] - a pinned mod's
                             # exact version disagrees with what the server requires; the
                             # user decides whether to unpin or accept the server's version
    declined: list = None    # [(mod_id, version)] mods this server runs and doesn't require
                             # that the user has already said no to. Not in the active set
                             # and not touched on disk; carried so the comparison can show
                             # them and let the choice be taken back

    def __post_init__(self):
        if self.declined is None:
            self.declined = []

    @property
    def blocking(self):
        """True if this plan can't safely be applied at all - some mod the
        server actually requires isn't resolvable from anywhere the client
        knows of. needs_repo alone never blocks: it's
        information for the user to act on, not a launch stopper by itself."""
        return bool(self.missing)


def plan_join(game_dir, server_mods, index, repo_bases, pinned=None, declined=None):
    """Computes active = required(server) UNION recommended(server, not declined)
    UNION resolve_dependencies(those) UNION pinned, and diffs it against what's
    cached/active - the render PLAN, not the render itself.

    server_mods: entries as returned by the ping/pong handshake's full list
    (handshake_snapshot's mods_list, or the equivalent fetched over the wire) -
    {"id","version","client_side","server_side","parity_required","source_repo"}.
    Only client_side entries concern the client at all; server_side-only entries
    are ignored here. Of those, parity_required splits them two ways:

      true   the server enforces this exact version on join, so it goes in the
             active set and a client that can't resolve it is blocked.
      false  the server won't block over it. Still planned in by default, at the
             server's version, so the common case is that everyone matches - but
             the user can decline, and a declined mod is left out entirely
             rather than installed, updated, or deactivated.

    index: the client's own merged index (fetch_indexes(repo_bases)) - used to
    pick each dependency's version within a major.
    repo_bases: the client's configured repos (list_repos(cfg)) - the only
    repos anything is actually fetched from.
    pinned: "id" / "id@version" strings (the launcher's always-on set) -
    unioned in regardless of which server is being joined.
    declined: mod ids the user has already said no to for THIS server. Only a
    mod the server marks parity_required false can be declined; a declined id
    the server actually requires is ignored, since no client-side choice can
    make that join work."""
    pinned = pinned or []
    declined = set(declined or [])

    client_mods = [m for m in server_mods if m.get("client_side")]
    required = {m["id"]: m["version"] for m in client_mods
                if parity_required(m)}
    recommended = {m["id"]: m["version"] for m in client_mods
                   if not parity_required(m)}
    hinted_repo = {m["id"]: m.get("source_repo") for m in server_mods if m.get("source_repo")}

    # A decline only ever applies to a mod the server marks optional. Declining
    # something required would just move the rejection from here to the server.
    skipped = [(mid, ver) for mid, ver in recommended.items() if mid in declined]

    roots = []
    missing = []
    needs_repo = []
    reason = {}         # mod_id -> "required" | "recommended" | "pinned", first tag wins
    exact_pin = {}      # mod_id -> pinned version (None if pinned to latest)

    def resolve_root(mod_id, version, tag):
        try:
            manifest = _fetch_manifest(repo_bases, mod_id, version)
        except ModManagerError:
            hint = hinted_repo.get(mod_id)
            if hint and _norm(hint) not in [_norm(b) for b in repo_bases]:
                needs_repo.append((mod_id, version, hint))
            elif tag == "recommended":
                # Unresolvable and not actually required: silently doing without
                # it beats blocking or nagging over a mod the server allows the
                # client not to have.
                pass
            else:
                missing.append((mod_id, version))
            return
        roots.append(manifest)
        reason[mod_id] = tag

    for mod_id, version in required.items():
        resolve_root(mod_id, version, "required")

    for mod_id, version in recommended.items():
        if mod_id not in declined:
            resolve_root(mod_id, version, "recommended")

    for entry in pinned:
        mod_id, version = _parse_pin_entry(entry)
        exact_pin[mod_id] = version
        if mod_id in reason:
            continue     # already resolved as a required root
        if version is None:
            matches = [s for s in index if s.id == mod_id]
            if not matches:
                missing.append((mod_id, "latest"))
                continue
            version = max((v for s in matches for v in s.versions), key=_parse_version)
        resolve_root(mod_id, version, "pinned")

    pin_conflicts = [
        (mod_id, pinned_version, required[mod_id])
        for mod_id, pinned_version in exact_pin.items()
        if mod_id in required and pinned_version and pinned_version != required[mod_id]
    ]

    def fetch_manifest(mod_id, version, source_repo):
        return _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)

    dependencies = resolve_dependencies(roots, index, fetch_manifest) if roots else []
    for dep in dependencies:
        reason.setdefault(dep.id, "dependency")

    all_mods = roots + dependencies
    installed = {rec["id"]: rec for rec in list_installed_mods(game_dir)}

    entries = []
    for mod in all_mods:
        cached = os.path.isdir(_cache_mod_dir(mod.id, mod.version))
        rec = installed.get(mod.id)
        active = bool(rec and rec.get("enabled") and rec.get("version") == mod.version)
        entries.append(PlanEntry(mod_id=mod.id, version=mod.version, source_repo=mod.source_repo,
                                  reason=reason.get(mod.id, "required"), cached=cached, active=active,
                                  manifest=mod))

    active_ids = {e.mod_id for e in entries}
    # Declining is "don't add it", never "take it away". A mod the user already
    # chose to have and the server doesn't require stays exactly as it is.
    to_deactivate = [mod_id for mod_id, rec in installed.items()
                      if rec.get("enabled") and mod_id not in active_ids
                      and mod_id not in declined]

    libraries = collect_library_dependencies(all_mods) if all_mods else []

    return JoinPlan(entries=entries, to_deactivate=to_deactivate, libraries=libraries,
                    missing=missing, needs_repo=needs_repo, pin_conflicts=pin_conflicts,
                    declined=skipped)


# Client-vs-server comparison
#
# A JoinPlan says what to DO. This says what the two sides currently look like
# and how each mod differs, which is what a person actually needs to see before
# agreeing to it. Pure: derived from a plan plus what's on disk, no display and
# no network, so it's testable on its own and the window below only renders it.

# What will happen to each mod. The first six change the client; the rest are
# informational or blocking.
ACTION_INSTALL     = "install"      # not here at all, needs downloading
ACTION_ACTIVATE    = "activate"     # in the local cache at this version, a file move
ACTION_UPDATE      = "update"       # installed at a lower version
ACTION_DOWNGRADE   = "downgrade"    # installed higher; the server pins an exact version
ACTION_ENABLE      = "enable"       # installed at the right version, currently disabled
ACTION_DEACTIVATE  = "deactivate"   # active here, not part of this server's set
ACTION_MATCH       = "match"        # already correct, nothing to do
ACTION_SKIPPED     = "skipped"      # recommended, and the user has said no to it here
ACTION_SERVER_ONLY = "server-only"  # the server runs it, clients never need it
ACTION_MISSING     = "missing"      # required, and no configured repo has it
ACTION_NEEDS_REPO  = "needs-repo"   # resolvable, but only from a repo not added here

# Sort weight: problems first, then real changes, then the quiet rows.
_ACTION_ORDER = {
    ACTION_MISSING: 0, ACTION_NEEDS_REPO: 0,
    ACTION_INSTALL: 1, ACTION_ACTIVATE: 1, ACTION_UPDATE: 1,
    ACTION_DOWNGRADE: 1, ACTION_ENABLE: 1,
    ACTION_DEACTIVATE: 2, ACTION_MATCH: 3,
    ACTION_SKIPPED: 4, ACTION_SERVER_ONLY: 5,
}


@dataclass
class DiffRow:
    """One mod, as it stands on each side and what would change."""
    mod_id: str
    server_version: str     # "" when the server doesn't run this mod
    client_version: str     # "" when it isn't installed here
    client_enabled: bool    # only meaningful when client_version is set
    action: str             # one of the ACTION_* values above
    reason: str             # "required" | "recommended" | "dependency" | "pinned" | ""
    optional: bool          # True when the user may overrule this row
    source_repo: str
    note: str = ""          # extra detail, e.g. a pin disagreeing with the server
    cached: bool = False     # this exact version is already in the local cache, so
                             # applying it is a file move rather than a download
    name: str = ""           # display name; falls back to mod_id wherever no
                             # manifest or install record was around to supply one

    @property
    def recommended(self):
        """A mod this server runs but doesn't require. The user's call in either
        direction, and the reason a row can be overruled towards NOT installing
        something, rather than only towards keeping something."""
        return self.reason == "recommended" or self.action == ACTION_SKIPPED

    @property
    def downloads(self):
        """True when applying this row actually fetches bytes. The only part of
        a plan that takes real time, so it's what the summary counts and what
        each row is labelled with."""
        return (not self.cached) and self.action in (
            ACTION_INSTALL, ACTION_UPDATE, ACTION_DOWNGRADE)

    @property
    def blocking(self):
        return self.action == ACTION_MISSING


def build_mod_diff(game_dir, server_mods, plan):
    """The full comparison behind a JoinPlan: one DiffRow per mod on either
    side, sorted problems-first. server_mods is the handshake list; plan is what
    plan_join computed from it.

    Two kinds of row are marked optional, both because the server's parity check
    can't fail over them. Deactivations: a mod the server doesn't run at all can
    stay active here without affecting the join. Recommendations: a mod the
    server does run but doesn't require, which it won't block over either
    way. Everything else is what the server actually enforces, so it isn't
    presented as a choice."""
    installed = {r["id"]: r for r in list_installed_mods(game_dir)}
    required = {m["id"]: m.get("version", "") for m in server_mods if m.get("client_side")}
    conflicts = {mid: (pv, sv) for mid, pv, sv in plan.pin_conflicts}

    rows = []
    for e in plan.entries:
        rec = installed.get(e.mod_id)
        cv = rec.get("version", "") if rec else ""
        enabled = bool(rec.get("enabled")) if rec else False
        if e.active:
            action = ACTION_MATCH
        elif rec is None:
            action = ACTION_ACTIVATE if e.cached else ACTION_INSTALL
        elif cv == e.version:
            action = ACTION_ENABLE          # right version on disk, just switched off
        else:
            try:
                older = _parse_version(cv) < _parse_version(e.version)
            except ModManagerError:
                older = True                # unparseable installed version: treat as stale
            action = ACTION_UPDATE if older else ACTION_DOWNGRADE
        pv_sv = conflicts.get(e.mod_id)
        recommended = e.reason == "recommended"
        if pv_sv:
            note = f"your pin says {pv_sv[0]}, the server requires {pv_sv[1]}"
        elif recommended:
            note = ("this server recommends it but doesn't require it; skipping "
                    "it won't stop you joining")
        else:
            note = ""
        rows.append(DiffRow(
            mod_id=e.mod_id, server_version=required.get(e.mod_id, ""),
            client_version=cv, client_enabled=enabled, action=action,
            reason=e.reason, optional=recommended and not e.active,
            source_repo=e.source_repo, note=note, cached=e.cached,
            name=e.manifest.name))

    for mod_id in plan.to_deactivate:
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version="", client_version=rec.get("version", ""),
            client_enabled=True, action=ACTION_DEACTIVATE, reason="", optional=True,
            source_repo=rec.get("source_repo", ""),
            note="this server doesn't run it; keeping it on won't block your join",
            name=rec.get("name", mod_id)))

    seen = {r.mod_id for r in rows}
    for mod_id, version in plan.declined:
        if mod_id in seen:
            continue
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version=version,
            client_version=rec.get("version", ""),
            client_enabled=bool(rec.get("enabled")), action=ACTION_SKIPPED,
            reason="recommended", optional=True,
            source_repo=rec.get("source_repo", ""),
            note="you've chosen not to install this one for this server",
            name=rec.get("name", mod_id)))
        seen.add(mod_id)

    for m in server_mods:
        if m["id"] in seen or m.get("client_side"):
            continue
        rows.append(DiffRow(
            mod_id=m["id"], server_version=m.get("version", ""), client_version="",
            client_enabled=False, action=ACTION_SERVER_ONLY, reason="",
            optional=False, source_repo=m.get("source_repo", ""),
            note="runs on the server only", name=m.get("name", m["id"])))
        seen.add(m["id"])

    for mod_id, version in plan.missing:
        if mod_id in seen:
            continue
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version=version, client_version=rec.get("version", ""),
            client_enabled=bool(rec.get("enabled")), action=ACTION_MISSING,
            reason="required", optional=False, source_repo="",
            note="not in any source you've added", name=rec.get("name", mod_id)))
        seen.add(mod_id)

    for mod_id, version, repo in plan.needs_repo:
        if mod_id in seen:
            continue
        rec = installed.get(mod_id, {})
        rows.append(DiffRow(
            mod_id=mod_id, server_version=version, client_version="",
            client_enabled=False, action=ACTION_NEEDS_REPO, reason="required",
            optional=False, source_repo=repo,
            note=f"add {_repo_shorthand(repo)} as a source, then check again",
            name=rec.get("name", mod_id)))
        seen.add(mod_id)

    # Sorted by display name, not id, so the on-screen order matches what's
    # actually shown - a mod_id-sorted list would look shuffled once the
    # window renders names instead of ids.
    return sorted(rows, key=lambda r: (_ACTION_ORDER.get(r.action, 9), r.name.lower()))


def _prune_orphaned_libraries(game_dir):
    """After the active set changes, removes any UserLibs/ library no longer
    pinned by a currently-ENABLED mod's record. Each library was already
    cache_store_library'd when its owning mod was installed/adopted, so
    nothing is lost - it just isn't present in UserLibs/ until something
    needs it again.

    Enabled rather than merely installed, because a disabled mod isn't loading
    and neither should its libraries be. Enabling it again restores them: the
    library pass runs over the whole active set, after the pass that enables."""
    still_needed = set()
    for rec in list_installed_mods(game_dir):
        if rec.get("enabled"):
            still_needed.update(rec.get("libraries", []))

    libs_dir = _userlibs_dir(game_dir)
    for name in os.listdir(libs_dir):
        if not name.endswith(".meta.json"):
            continue
        filename = name[:-len(".meta.json")]
        if filename in still_needed:
            continue
        cache_store_library(game_dir, filename)
        for path in (os.path.join(libs_dir, filename), os.path.join(libs_dir, name)):
            try:
                os.remove(path)
            except OSError:
                pass


def render_plan_steps(plan):
    """How many items render_active_set will work through for this plan: one per
    mod it changes, per library it checks, and per mod it deactivates. Exactly
    the set that reports through on_step, so a caller can size a progress bar
    before starting and have it end full."""
    return (len([e for e in plan.entries if not e.active])
            + len(plan.libraries) + len(plan.to_deactivate))


def render_active_set(game_dir, plan, on_progress=None, on_step=None):
    """Applies a JoinPlan to disk - the actual render step. Restores from
    cache wherever cached (a file move), downloads only what's
    genuinely missing, then deactivates anything no longer needed - never
    deleting a mod outright (it's cached first) and never re-downloading
    something already cached. Raises ModManagerError immediately if
    plan.blocking; callers must check that themselves before deciding whether
    to show a plan at all, same as install_mod_closure surfaces resolution
    errors before touching disk.

    on_progress takes a line of human-readable detail. on_step brackets each
    item with on_step(item_id, "start") and on_step(item_id, "done"): mod ids
    for mods, filenames for libraries, one pair per item counted by
    render_plan_steps. A library already active at the pinned content still
    reports both, so the count a caller sized against is always reached."""
    if plan.blocking:
        raise ModManagerError(
            "This plan has required mods that couldn't be resolved from any "
            "configured repo; fix that before applying it.")
    progress = on_progress or (lambda *_a: None)
    step = on_step or (lambda *_a: None)

    for entry in plan.entries:
        if entry.active:
            continue
        step(entry.mod_id, "start")
        rec = _read_mod_record(game_dir, entry.mod_id)
        disabled_here = (bool(rec) and rec.get("version") == entry.version
                         and os.path.isfile(_disabled_record_path(game_dir, entry.mod_id)))
        if disabled_here:
            # Already on disk at exactly this version, just switched off. Renaming
            # the record back is the whole job: no copy out of the cache, no
            # download, and it works with no network at all. The counterpart to
            # deactivating by disabling below.
            progress(f"Enabling {entry.mod_id} {entry.version}")
            enable_mod(game_dir, entry.mod_id)
        elif entry.cached:
            progress(f"Installing {entry.mod_id} {entry.version} (cached)")
            cache_restore_mod(game_dir, entry.mod_id, entry.version)
        else:
            progress(f"Downloading {entry.mod_id} {entry.version}")
            install_mod(game_dir, entry.manifest, progress)
            cache_store_mod(game_dir, entry.mod_id)
        step(entry.mod_id, "done")

    for lib in plan.libraries:
        filename = _safe_basename(lib.filename)
        step(filename, "start")
        dest = os.path.join(_userlibs_dir(game_dir), filename)
        existing = _read_sidecar(_library_sidecar_path(game_dir, filename))
        if os.path.isfile(dest) and existing and existing.get("sha256") == lib.sha256:
            step(filename, "done")
            continue     # already active with the exact pinned content
        if os.path.isdir(_cache_library_dir(filename, lib.sha256)):
            progress(f"Installing library {filename} (cached)")
            cache_restore_library(game_dir, filename, lib.sha256)
        else:
            progress(f"Downloading library {filename}")
            install_library_dependency(game_dir, lib, progress)
            cache_store_library(game_dir, filename)
        step(filename, "done")

    for mod_id in plan.to_deactivate:
        step(mod_id, "start")
        progress(f"Deactivating {mod_id}")
        # Disabled, not uninstalled: the record is renamed so MelonLoader skips
        # the folder, and every file stays exactly where it is. The mod keeps its
        # place in the installed list and comes back on with a rename rather than
        # a download. Cached first all the same, so a later version switch is a
        # file move whichever way it goes.
        cache_store_mod(game_dir, mod_id)
        disable_mod(game_dir, mod_id)
        step(mod_id, "done")

    _prune_orphaned_libraries(game_dir)


def resolve_missing_mods(missing, index, repo_bases):
    """Resolves a rejection payload's `missing` entries
    ({"id","version","source_repo"}) into installable manifests, for the
    post-reject recovery flow: after a join is denied for a mod mismatch, the
    launcher runs this to fetch exactly the mods+versions the server said
    were wrong, installs them, and rejoins. `source_repo` is a HINT only:
    resolution always goes through the client's own configured repos
    (repo_bases), never a URL
    taken from the payload; an entry resolvable nowhere raises naming its
    hint if it has one, same missing/needs_repo distinction plan_join uses.

    Returns (roots, dependencies): roots is one ModManifest per resolved
    missing entry, in order; dependencies is resolve_dependencies' closure
    over all of them (the same shape install_mod_closure computes for a
    single root, generalized to plan_join's multi-root case)."""
    roots = []
    unresolved = []
    for entry in missing:
        mod_id, version = entry["id"], entry["version"]
        try:
            roots.append(_fetch_manifest(repo_bases, mod_id, version))
        except ModManagerError:
            hint = entry.get("source_repo")
            if hint and _norm(hint) not in [_norm(b) for b in repo_bases]:
                unresolved.append(f"{mod_id} {version} (available from {hint}, "
                                  f"which you haven't added as a source)")
            else:
                unresolved.append(f"{mod_id} {version} (not found in any of "
                                  f"your configured sources)")

    if unresolved:
        raise ModManagerError("Could not resolve: " + "; ".join(unresolved))

    def fetch_manifest(mod_id, version, source_repo):
        return _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)

    dependencies = resolve_dependencies(roots, index, fetch_manifest) if roots else []
    return roots, dependencies


# Modlist import/export
#
# One {schema, repos, mods} shape serves three uses with zero conversion: a
# launcher (client or launcher-run server) exports/imports it as a shareable
# file; a headless server's /modlist config (TavernLib's ModsListConfig) IS
# this same file. `repos` is always informational only, a shorthand naming
# where the pack's author expects its
# mods to come from, never resolved into a URL or added as a source by any
# importer; resolution only ever uses the importer's OWN configured repos.

MODLIST_SCHEMA = 1


def export_modlist(game_dir, cfg, pin_versions=False):
    """Every currently-ENABLED installed mod, in the canonical modlist shape.
    pin_versions=False (default) writes each as a bare id, so importing it
    later tracks whatever's latest at that point (a living "modpack"
    reference); pin_versions=True writes "id@version" for every entry, an
    exact reproducible snapshot of what's installed right now. `repos` is
    list_repos_named(cfg)'s shorthands - this launcher's configured sources,
    for a human reading the file or a pack curator documenting it."""
    entries = []
    for m in sorted(list_installed_mods(game_dir), key=lambda r: r["id"]):
        if not m.get("enabled"):
            continue
        entries.append(f"{m['id']}@{m['version']}" if pin_versions else m["id"])
    return {
        "schema": MODLIST_SCHEMA,
        "repos": sorted(list_repos_named(cfg).keys()),
        "mods": entries,
    }


@dataclass
class ImportPlan:
    roots: list           # list[ModManifest], one per resolved modlist entry
    dependencies: list     # list[ModManifest], resolve_dependencies' closure
    unresolved: list       # [(mod_id, version_or_None)] - not found in any configured repo
    hinted_repos: list     # the modlist's own informational `repos` field, for display

    @property
    def blocking(self):
        """True if anything the modlist listed couldn't be found at all -
        apply_import refuses to run until this is empty, same as plan_join's
        `missing` blocks a join."""
        return bool(self.unresolved)


def import_modlist(modlist, index, repo_bases):
    """Resolves a modlist's `mods` entries against the importer's OWN
    configured repos only (repo_bases) - the file's `repos` field is never
    treated as a source to fetch from or silently add. Same
    by-id / exact-version resolution install already uses. An entry not
    resolvable from any configured repo is reported unresolved (with the
    modlist's hinted `repos` surfaced alongside it, so the user knows what to
    go add) rather than silently skipped or auto-added from its shorthand."""
    if modlist.get("schema") != MODLIST_SCHEMA:
        raise ModManagerError(
            f"This modlist is schema {modlist.get('schema')!r}; this launcher "
            f"only understands schema {MODLIST_SCHEMA}.")

    roots = []
    unresolved = []
    for entry in modlist.get("mods", []):
        mod_id, version = _parse_pin_entry(entry)
        try:
            roots.append(_fetch_manifest(repo_bases, mod_id, version) if version
                        else resolve_mod_by_id(repo_bases, mod_id))
        except ModManagerError:
            unresolved.append((mod_id, version))

    def fetch_manifest(mod_id, ver, source_repo):
        return _fetch_manifest(repo_bases, mod_id, ver, prefer_repo=source_repo)

    dependencies = resolve_dependencies(roots, index, fetch_manifest) if roots else []
    return ImportPlan(roots=roots, dependencies=dependencies, unresolved=unresolved,
                      hinted_repos=list(modlist.get("repos", [])))


def apply_import(game_dir, plan, on_progress=None):
    """Installs everything import_modlist resolved - every root plus their
    dependency closure plus every collected library - in one confirmed batch
    (install_mod_closure's flow, generalized to a modlist's many roots).
    Raises if plan.blocking; callers must check that themselves first, same
    as render_active_set does for a JoinPlan."""
    if plan.blocking:
        raise ModManagerError(
            "This modlist has mods that couldn't be resolved from any "
            "configured source; fix that before importing it.")
    progress = on_progress or (lambda *_a: None)
    all_mods = plan.roots + plan.dependencies
    for mod in all_mods:
        progress(f"Installing {mod.id} {mod.version}")
        install_mod(game_dir, mod, progress)
    for lib in collect_library_dependencies(all_mods):
        progress(f"Installing library {lib.filename}")
        install_library_dependency(game_dir, lib, progress)


# Community-mods UI
#
# The Community Mods and Manage Sources windows. Both launchers import these, so
# there's one copy instead of two. They take all their styling from the palette
# and widget factories the host wired in via set_helpers(host); see the module
# docstring. The two loader-presence checks gate install: a community mod needs
# MelonLoader + TavernLib on disk first.

TAVERNLIB_FILENAME = "TavernLib.dll"


def _melonloader_installed(game_dir):
    return (os.path.isdir(os.path.join(game_dir, "MelonLoader")) and
            os.path.isfile(os.path.join(game_dir, "version.dll")))


def _tavernlib_installed(game_dir):
    return os.path.isfile(os.path.join(game_dir, "Plugins", TAVERNLIB_FILENAME))


class ModSourcesWindow(tk.Toplevel):
    """Thin manager for the user's community repositories: a listbox plus
    add/remove, calling straight into modmanager.list_repos/add_repo/remove_repo.
    The default Modding Tavern repo is shown but can't be removed. Adding a repo
    shows the unverified-source warning first."""
    def __init__(self, parent, on_change=None):
        super().__init__(parent)
        self.title("Mod Sources")
        self.configure(bg=BG)
        self.geometry("520x360")
        self.resizable(False, False)
        self._on_change = on_change
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Mod Sources", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Repositories the launcher fetches community mods from. The "
                 "Modding Tavern source is always present. You can add your own "
                 "sources, but they are not checked by Modding Tavern; you are "
                 "responsible for what you install from them.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,6))

        lb_frame = tk.Frame(self, bg=BORDER, highlightbackground=BORDER, highlightthickness=1)
        lb_frame.pack(fill="both", expand=True, padx=20, pady=(0,8))
        self._listbox = tk.Listbox(lb_frame, bg=SURF, fg=PARCH, bd=0,
                                   highlightthickness=0, selectbackground=AMBERDIM,
                                   font=("Consolas",9), activestyle="none")
        self._listbox.pack(fill="both", expand=True, padx=1, pady=1)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=(0,12))
        _btn(bar, "+ Add source", self._on_add, style="primary",
             font=("Segoe UI",9), pady=5, padx=12).pack(side="left")
        _btn(bar, "- Remove selected", self._on_remove, style="danger",
             font=("Segoe UI",9), pady=5, padx=12).pack(side="left", padx=8)

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",8), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(0,8))
        self._reload()

    def _reload(self):
        self._repos = list_repos(_load_cfg())
        self._listbox.delete(0, tk.END)
        for url in self._repos:
            tag = "  (default, always on)" if _is_default(url) else ""
            self._listbox.insert(tk.END, url + tag)

    def _on_add(self):
        url = simpledialog.askstring("Add mod source",
            "Raw-content base URL of the repository\n"
            "(e.g. https://raw.githubusercontent.com/user/repo/main):",
            parent=self)
        if not url:
            return
        if not messagebox.askokcancel("Unverified source",
                "This source has NOT been checked by Modding Tavern.\n\n"
                "Mods you install from it run with full access to your game and "
                "PC. You are responsible for vetting what you install.\n\n"
                "Add this source anyway?", parent=self):
            return
        self._status.set("Checking source...")
        def worker():
            try:
                cfg = _load_cfg()
                add_repo(cfg, url)
                self.after(0, lambda: self._added_ok())
            except Exception as e:
                self.after(0, lambda e=e: self._status.set(f"Couldn't add source: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _added_ok(self):
        self._status.set("Source added.")
        self._reload()
        if self._on_change:
            self._on_change()

    def _on_remove(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        url = self._repos[sel[0]]
        if _is_default(url):
            messagebox.showinfo("Can't remove",
                "The Modding Tavern source can't be removed.", parent=self)
            return
        if not messagebox.askyesno("Remove source",
                f"Remove this source?\n\n{url}", parent=self):
            return
        try:
            remove_repo(_load_cfg(), url)
        except Exception as e:
            self._status.set(f"Couldn't remove: {e}")
            return
        self._status.set("Source removed.")
        self._reload()
        if self._on_change:
            self._on_change()


class PinnedModsWindow(tk.Toplevel):
    """Client-only: mods pinned always-on, unioned into whichever server's
    active set is rendered at join regardless of what that server
    actually requires. Same listbox/add/remove shape as ModSourcesWindow,
    calling straight into list_pinned/add_pin/remove_pin."""
    def __init__(self, parent, on_change=None):
        super().__init__(parent)
        self.title("Pinned Mods")
        self.configure(bg=BG)
        self.geometry("480x360")
        self.resizable(False, False)
        self._on_change = on_change
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Pinned Mods", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Mods that stay active no matter which server you join, on top "
                 "of whatever that server itself requires. Pin just the mod id to "
                 "always track its latest version, or id@version to pin an exact "
                 "one (e.g. Acme.AdminTools@1.4.0). If the server you're joining "
                 "requires a different exact version, you'll be asked which to use.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=430, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,6))

        lb_frame = tk.Frame(self, bg=BORDER, highlightbackground=BORDER, highlightthickness=1)
        lb_frame.pack(fill="both", expand=True, padx=20, pady=(0,8))
        self._listbox = tk.Listbox(lb_frame, bg=SURF, fg=PARCH, bd=0,
                                   highlightthickness=0, selectbackground=AMBERDIM,
                                   font=("Consolas",9), activestyle="none")
        self._listbox.pack(fill="both", expand=True, padx=1, pady=1)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=(0,12))
        _btn(bar, "+ Add pin", self._on_add, style="primary",
             font=("Segoe UI",9), pady=5, padx=12).pack(side="left")
        _btn(bar, "- Remove selected", self._on_remove, style="danger",
             font=("Segoe UI",9), pady=5, padx=12).pack(side="left", padx=8)

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",8), wraplength=430, justify="left"
        ).pack(anchor="w", padx=20, pady=(0,8))
        self._reload()

    def _reload(self):
        self._pins = list_pinned(_load_cfg())
        self._listbox.delete(0, tk.END)
        for entry in self._pins:
            self._listbox.insert(tk.END, entry)

    def _on_add(self):
        entry = simpledialog.askstring("Add pin",
            "Mod id to pin (e.g. Acme.QuestGiver or Acme.AdminTools@1.4.0):",
            parent=self)
        if not entry:
            return
        try:
            add_pin(_load_cfg(), entry.strip())
        except Exception as e:
            self._status.set(f"Couldn't add pin: {e}")
            return
        self._status.set("Pin added.")
        self._reload()
        if self._on_change:
            self._on_change()

    def _on_remove(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        entry = self._pins[sel[0]]
        try:
            remove_pin(_load_cfg(), entry)
        except Exception as e:
            self._status.set(f"Couldn't remove: {e}")
            return
        self._status.set("Pin removed.")
        self._reload()
        if self._on_change:
            self._on_change()


_tooltip_state = {"after_id": None, "win": None, "owner": None}


def _hide_tooltip():
    if _tooltip_state["after_id"] is not None and _tooltip_state["owner"] is not None:
        _tooltip_state["owner"].after_cancel(_tooltip_state["after_id"])
    _tooltip_state["after_id"] = None
    if _tooltip_state["win"] is not None:
        _tooltip_state["win"].destroy()
    _tooltip_state["win"] = None
    _tooltip_state["owner"] = None


def _attach_tooltip(widget, text):
    """A small delayed popup on hover - tk has no built-in tooltip, so this is
    the minimal version: appears after a beat over the widget, gone the
    instant the pointer leaves. The pending timer and the popup itself live
    in shared module state rather than a per-widget closure, so entering any
    tooltip-bound widget always clears whatever another one left behind
    first. That matters because several widgets can cover the exact same
    row (a fixed-width cell and the label filling it, say) - each tracking
    its own popup independently let a fast Enter/Leave between them show two
    at once or leave one stuck open."""
    def _show():
        _tooltip_state["after_id"] = None
        win = tk.Toplevel(widget)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win.geometry(f"+{x}+{y}")
        tk.Label(win, text=text, bg=SURF, fg=PARCH, font=("Segoe UI", 8),
                 wraplength=260, justify="left", padx=8, pady=5,
                 highlightbackground=BORDER, highlightthickness=1
        ).pack()
        _tooltip_state["win"] = win

    def _enter(_event):
        _hide_tooltip()
        _tooltip_state["owner"] = widget
        _tooltip_state["after_id"] = widget.after(400, _show)

    def _leave(_event):
        if _tooltip_state["owner"] is widget:
            _hide_tooltip()

    widget.bind("<Enter>", _enter)
    widget.bind("<Leave>", _leave)


class ModDiffWindow(tk.Toplevel):
    """Side-by-side comparison of what a server runs against what's installed
    here, shown before anything is applied. Replaces a flat "will install / will
    deactivate" list with the actual two sides and a per-mod verdict, so it's
    clear WHY each change is proposed and what the server is actually asking for.

    Also where applying happens. Given an apply_action, pressing Apply turns the
    same table into a progress view - each row saying what's happening to it,
    a bar underneath - and the window stays up until the work is done. The
    outcome is reported in the window rather than a popup: with
    close_on_success it disappears and the caller carries on (the join case),
    otherwise it stays with the result on screen (the sync case).

    Without an apply_action it's a plain confirm dialog: the caller waits, then
    reads `.result` (True to apply).

    The plan is edited to match the screen before anything is rendered, so what
    gets applied is what was shown: a deactivation the user keeps is dropped from
    plan.to_deactivate, and a recommended mod the user skips is dropped from
    plan.entries. `declined` afterwards is the full set of recommended mods
    turned off for this server, for the caller to remember.
    on_cancel(window) fires exactly once if the window closes without a
    successful apply, so a caller waiting on it isn't left hanging. Check
    `.apply_attempted` there to tell a plain decline from a close after a
    failure that has already been reported."""

    _WIDTHS = (280, 90, 90, 190)   # mod, server, you, action

    # Row colour per action, keyed by an abstract tag name resolved to an
    # actual colour in _build (once the host's palette is wired in via
    # set_helpers). Reads as a diff at a glance: additions green, removals
    # red, version changes amber, problems red, quiet rows muted.
    _TAGS = {
        ACTION_INSTALL:     "add",   ACTION_ACTIVATE: "add",  ACTION_ENABLE: "add",
        ACTION_UPDATE:      "chg",   ACTION_DOWNGRADE: "chg",
        ACTION_DEACTIVATE:  "del",   ACTION_MATCH:    "same",
        ACTION_SERVER_ONLY: "same",  ACTION_SKIPPED:  "same",
        ACTION_MISSING:     "bad",   ACTION_NEEDS_REPO: "bad",
    }
    _VERB = {
        ACTION_INSTALL:  "Download",   ACTION_ACTIVATE:  "Install",
        ACTION_ENABLE:   "Enable",     ACTION_UPDATE:    "Update",
        ACTION_DOWNGRADE:"Downgrade",  ACTION_DEACTIVATE:"Deactivate",
        ACTION_MATCH:    "Up to date", ACTION_SERVER_ONLY:"Not needed",
        ACTION_SKIPPED:  "Skipped",
        ACTION_MISSING:  "Unavailable",ACTION_NEEDS_REPO:"Source missing",
    }
    # What a row says while it's being worked on, so the table reads as live
    # progress instead of a static proposal.
    _VERB_ING = {
        ACTION_INSTALL:  "Downloading", ACTION_ACTIVATE:  "Installing",
        ACTION_ENABLE:   "Enabling",    ACTION_UPDATE:    "Updating",
        ACTION_DOWNGRADE:"Downgrading", ACTION_DEACTIVATE:"Deactivating",
    }

    def __init__(self, parent, rows, plan, server_label="this server",
                 apply_action=None, close_on_success=True, on_cancel=None):
        super().__init__(parent)
        self.title("Mod Comparison")
        self.configure(bg=BG)
        self.geometry("760x560")
        self.minsize(660, 440)
        self._rows   = {r.mod_id: r for r in rows}
        self._order  = [r.mod_id for r in rows]
        self._plan   = plan
        self._label  = server_label
        # Both directions a row can be overruled. _keep: deactivations the user
        # wants to hold on to. _skip: recommended mods the user doesn't want.
        # A row starts in _skip if it came in already declined for this server.
        self._keep   = set()
        self._skip   = {r.mod_id for r in rows if r.action == ACTION_SKIPPED}
        self.result  = False

        self._apply_action     = apply_action
        self._close_on_success = close_on_success
        self._on_cancel_cb     = on_cancel
        self._applying   = False
        self._closed     = False
        self.apply_attempted = False   # Apply was pressed; a close isn't a decline
        self.declined = list(self._skip)   # kept current on every apply
        self._progress   = {}     # mod id -> "start" | "done", during an apply
        self._steps_done = 0
        self._steps_total = 0
        # mod id -> (action_cell, action_label, toggle_frame_or_None); the
        # toggle is None for a non-optional row, which never had a choice.
        self._action_widgets = {}
        self._name_labels    = {}     # mod id -> its coloured name label
        self._segment_labels = {}     # mod id -> {option text: its segment label}

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self.grab_set()

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Mod Comparison", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Label(h, text=self._label, bg=SURF, fg=MUTED,
                 font=("Segoe UI",9)).pack(side="left", pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Server / You / Action: what this server runs, what's installed here, and "
                 "what applying would do. A toggle means it's your call; no toggle means the "
                 "server decides. Hover a mod name or its Action for more detail.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=720, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        # Colour per abstract tag, resolved now that the host's palette is
        # wired in (set_helpers ran before this window could ever open).
        self._tag_colors = {
            "add": GREEN, "chg": AMBER, "del": RED, "same": MUTED, "bad": RED,
            "kept": CYAN, "working": AMBER, "done": GREEN,
        }

        table_wrap = tk.Frame(self, bg=BG)
        table_wrap.pack(fill="both", expand=True, padx=16)

        header = tk.Frame(table_wrap, bg=SURF)
        header.pack(fill="x")
        for text, width in zip(("Mod", "Server", "You", "Action"), self._WIDTHS):
            cell = tk.Frame(header, bg=SURF, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            tk.Label(cell, text=text, bg=SURF, fg=AMBER, font=("Segoe UI",9,"bold"),
                     anchor="w").pack(fill="both", padx=6)

        # A Treeview can't host a live control per row, so the table itself is
        # a scrollable stack of row frames instead - each optional row gets
        # its own two-way toggle rather than a shared button whose meaning
        # changes depending what's selected.
        canvas_frame = tk.Frame(table_wrap, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vsb = _mk_scrollbar(canvas_frame, canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.config(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas
        self._rows_frame = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        self._rows_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
        def _mousewheel(event):
            # Without this guard the canvas will still happily scroll a
            # screenful of nothing - scrollregion can end up briefly stale
            # (e.g. mid-rebuild), and yview_scroll doesn't check content
            # height on its own before moving the view.
            if self._rows_frame.winfo_height() <= canvas.winfo_height():
                return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _mousewheel)
        self._rows_frame.bind("<MouseWheel>", _mousewheel)

        # Progress line and bar. Built now so they keep their place in the
        # layout, packed only once an apply starts (see _begin_apply).
        self._status = tk.StringVar(value="")
        self._status_lbl = tk.Label(self, textvariable=self._status, bg=BG, fg=MUTED,
                                    font=("Segoe UI",9), wraplength=720,
                                    justify="left", anchor="w")
        self._bar = tk.Canvas(self, bg=SURF, height=6, highlightthickness=0, bd=0)
        self._bar_fill = self._bar.create_rectangle(0, 0, 0, 6, fill=GREEN, width=0)
        self._bar.bind("<Configure>", lambda e: self._draw_bar())

        self._btn_bar = tk.Frame(self, bg=BG)
        self._btn_bar.pack(fill="x", padx=16, pady=(8,12))
        self._apply_btn = _btn(self._btn_bar, "Apply changes", self._on_apply,
                               style="primary", font=("Segoe UI",9), pady=5, padx=14)
        self._apply_btn.pack(side="right")
        self._cancel_btn = _btn(self._btn_bar, "Cancel", self._on_cancel, style="dim",
                                font=("Segoe UI",9), pady=5, padx=14)
        self._cancel_btn.pack(side="right", padx=8)

        self._populate()

    def _draw_bar(self):
        width = max(self._bar.winfo_width(), 1)
        frac = (self._steps_done / self._steps_total) if self._steps_total else 0.0
        self._bar.coords(self._bar_fill, 0, 0, int(width * min(1.0, frac)), 6)

    def _action_text(self, mod_id):
        r = self._rows[mod_id]
        state = self._progress.get(mod_id)
        if state == "done":
            return "Done"
        if state == "start":
            return self._VERB_ING.get(r.action, "Working") + "…"
        if mod_id in self._keep:
            return "Keep active"
        if r.recommended and mod_id in self._skip:
            return "Skip"
        if r.action == ACTION_SKIPPED:
            return "Install"          # was skipped, user has just turned it back on
        verb = self._VERB.get(r.action, r.action)
        # Say which rows cost a download and which are just a file move out
        # of the cache: it's the difference between a moment and a wait.
        if r.action in (ACTION_INSTALL, ACTION_ACTIVATE, ACTION_UPDATE, ACTION_DOWNGRADE):
            verb += "" if r.downloads else " (cached)"
        return verb

    def _row_tag(self, mod_id):
        state = self._progress.get(mod_id)
        if state:
            return "done" if state == "done" else "working"
        r = self._rows[mod_id]
        if mod_id in self._keep:
            return "kept"
        if r.recommended:
            # Overruled either way reads as a deliberate choice, not a change.
            if mod_id in self._skip:
                return "same" if r.action == ACTION_SKIPPED else "kept"
            return "add" if r.action == ACTION_SKIPPED else self._TAGS.get(r.action, "same")
        return self._TAGS.get(r.action, "same")

    def _row_color(self, mod_id):
        return self._tag_colors.get(self._row_tag(mod_id), MUTED)

    def _choice_options(self, mod_id):
        """(values, current) for an optional row's two-way toggle. 'included'
        is whichever option the plan proposes by default - install for a
        recommendation, deactivate for an extra the server doesn't use;
        'current' is the separate, actual pending choice."""
        r = self._rows[mod_id]
        if r.recommended:
            if r.action == ACTION_SKIPPED:
                included = "Install"
            else:
                included = self._VERB.get(r.action, r.action)
                if r.action in (ACTION_INSTALL, ACTION_ACTIVATE,
                                ACTION_UPDATE, ACTION_DOWNGRADE):
                    included += "" if r.downloads else " (cached)"
            values = (included, "Skip")
            current = "Skip" if mod_id in self._skip else included
        else:
            values = ("Deactivate", "Keep")
            current = "Keep" if mod_id in self._keep else "Deactivate"
        return values, current

    def _populate(self):
        for child in self._rows_frame.winfo_children():
            child.destroy()
        self._action_widgets = {}
        self._name_labels = {}
        self._segment_labels = {}
        for mod_id in self._order:
            self._mk_row(mod_id)
        # The <Configure> binding usually keeps the scrollregion current, but
        # it fires off Tk's own idle loop - forcing it here too means a
        # rebuild never leaves a stale, too-tall region behind that would let
        # the wheel/scrollbar move the view into empty space.
        self._rows_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        # A required mod nobody can supply can't be resolved by pressing
        # Apply, so the button says so rather than failing after the fact.
        blocked = any(r.blocking for r in self._rows.values())
        self._apply_btn.config(state="disabled" if blocked else "normal",
                               text="Can't apply" if blocked else "Apply changes")

    def _mk_row(self, mod_id):
        r = self._rows[mod_id]
        row = tk.Frame(self._rows_frame, bg=BG)
        row.pack(fill="x")

        name_cell = tk.Frame(row, bg=BG, width=self._WIDTHS[0], height=26)
        name_cell.pack(side="left", fill="y"); name_cell.pack_propagate(False)
        name_lbl = tk.Label(name_cell, text=r.name or r.mod_id, bg=BG,
                            fg=self._row_color(mod_id), font=("Segoe UI",9), anchor="w")
        name_lbl.pack(fill="both", padx=6)
        self._name_labels[mod_id] = name_lbl
        # The display name is friendlier; the id is what you'd actually need
        # to go looking for it (a manifest file, a repo listing), so it's a
        # hover away rather than gone.
        _attach_tooltip(name_cell, r.mod_id)
        _attach_tooltip(name_lbl, r.mod_id)

        for text, width in ((r.server_version or "-", self._WIDTHS[1]),
                            (r.client_version or "-", self._WIDTHS[2])):
            cell = tk.Frame(row, bg=BG, width=width, height=26)
            cell.pack(side="left", fill="y"); cell.pack_propagate(False)
            tk.Label(cell, text=text, bg=BG, fg=MUTED, font=("Segoe UI",9),
                     anchor="w").pack(fill="both", padx=6)

        action_cell = tk.Frame(row, bg=BG, width=self._WIDTHS[3], height=26)
        action_cell.pack(side="left", fill="y"); action_cell.pack_propagate(False)
        action_lbl = tk.Label(action_cell, bg=BG, font=("Segoe UI",9), anchor="w")
        toggle = None
        hover = [action_cell, action_lbl]
        if r.optional:
            toggle = self._mk_toggle(action_cell, mod_id)
            toggle.pack(fill="both")
            hover.append(toggle)
            hover.extend(self._segment_labels[mod_id].values())
        else:
            action_lbl.config(text=self._action_text(mod_id), fg=self._row_color(mod_id))
            action_lbl.pack(fill="both", padx=6)
        self._action_widgets[mod_id] = (action_cell, action_lbl, toggle)
        # r.note is the longer explanation (why it's listed, a pin conflict,
        # etc.) that used to sit under the row as its own line - now a hover
        # away over Action instead, on whichever widget is actually visible
        # there (a toggle's segments included, since they cover the cell).
        if r.note:
            for w in hover:
                _attach_tooltip(w, r.note)

    def _mk_toggle(self, parent, mod_id):
        """A two-way toggle for an optional row, in place of a dropdown: two
        small labels side by side, the live choice lit and the other dim -
        click either to switch. Nothing pops open, so there's no separate
        listbox popup fighting the dark theme, and the current choice reads
        without needing to click anything first."""
        values, current = self._choice_options(mod_id)
        wrap = tk.Frame(parent, bg=BG)
        segs = {}
        for text in values:
            lbl = tk.Label(wrap, text=text, font=("Segoe UI",8),
                           padx=6, pady=3, cursor="hand2")
            lbl.pack(side="left", padx=(0,4))
            lbl.bind("<Button-1>",
                    lambda e, mid=mod_id, t=text: self._on_choice_changed(mid, t))
            segs[text] = lbl
        self._segment_labels[mod_id] = segs
        self._paint_toggle(mod_id, current)
        return wrap

    def _paint_toggle(self, mod_id, current):
        for text, lbl in self._segment_labels[mod_id].items():
            active = text == current
            lbl.config(bg=(AMBERDIM if active else SURF),
                      fg=(AMBER if active else MUTED))

    def _on_choice_changed(self, mod_id, choice):
        r = self._rows[mod_id]
        # The override literal is always the second _choice_options value
        # (Skip for a recommendation, Keep for an extra); picking it means
        # "override the plan's default", picking the other means "don't".
        target_set = self._skip if r.recommended else self._keep
        override_literal = "Skip" if r.recommended else "Keep"
        if choice == override_literal:
            target_set.add(mod_id)
        else:
            target_set.discard(mod_id)
        self._name_labels[mod_id].config(fg=self._row_color(mod_id))
        self._paint_toggle(mod_id, choice)

    def _on_apply(self):
        if self._applying or any(r.blocking for r in self._rows.values()):
            return
        # Bring the plan in line with the screen, so what gets rendered is what
        # was shown. Kept mods come out of to_deactivate and stay where they are;
        # skipped recommendations come out of entries and are never fetched.
        #
        # A skipped mod's own dependencies stay planned in, since nothing here
        # knows which root pulled which dependency. They're harmless (installed
        # and enabled, just unused) and the next join drops them on its own,
        # having resolved the active set without that root in the first place.
        self._plan.to_deactivate = [m for m in self._plan.to_deactivate
                                    if m not in self._keep]
        self._plan.entries = [e for e in self._plan.entries
                              if e.mod_id not in self._skip]
        # What the caller should remember for this server, including choices
        # taken back: a row toggled off _skip drops out of here too.
        self.declined = sorted(self._skip)
        if self._apply_action is None:
            self.result = True
            self.destroy()
            return
        self._begin_apply()
        self._apply_action(self)

    def _begin_apply(self):
        """Turn the proposal into a progress view. Closing is off for the
        duration: render_active_set has no abort, and a Mods/ folder caught
        half-rendered is worse than waiting. Every row's toggle (if it had
        one) is swapped for the same static, colour-coded label a required
        row always had - set_step keeps that label live as things happen."""
        self._applying = True
        self.apply_attempted = True
        self._steps_done = 0
        self._steps_total = render_plan_steps(self._plan)
        for mod_id, (cell, label, toggle) in self._action_widgets.items():
            if toggle is not None:
                toggle.pack_forget()
            label.config(text=self._action_text(mod_id), fg=self._row_color(mod_id))
            label.pack(fill="both", padx=6)
        self._apply_btn.config(state="disabled", text="Applying…")
        self._cancel_btn.config(state="disabled")
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self._status_lbl.config(fg=MUTED)
        self._status.set("Starting…")
        self._status_lbl.pack(anchor="w", fill="x", padx=16, pady=(6,2),
                              before=self._btn_bar)
        self._bar.pack(fill="x", padx=16, pady=(0,4), before=self._btn_bar)
        self._draw_bar()

    # Driven by whoever runs the render, from the UI thread.

    def set_status(self, message):
        """The line of detail under the table: which file, how far into it."""
        if self._closed:
            return
        self._status.set(message)

    def set_step(self, item_id, state):
        """One item starting or finishing, as render_active_set reports them.
        Ids with no row of their own (libraries) still move the bar; they just
        have nothing to mark."""
        if self._closed:
            return
        if state == "done":
            self._steps_done += 1
            self._draw_bar()
        if item_id in self._rows:
            self._progress[item_id] = state
            if item_id in self._action_widgets:
                _, label, _ = self._action_widgets[item_id]
                label.config(text=self._action_text(item_id), fg=self._row_color(item_id))

    def apply_finished(self, ok, message=""):
        """The render is over. On success with close_on_success the window gets
        out of the way so the caller can carry on; otherwise the outcome stays
        on screen, which is the whole point of not using a popup."""
        if self._closed:
            return
        self._applying = False
        self.result = bool(ok)
        if ok:
            self._steps_done = self._steps_total
            self._draw_bar()
        if ok and self._close_on_success:
            self.destroy()
            return
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._status_lbl.config(fg=GREEN if ok else RED)
        self._status.set(message or ("Done." if ok else "Couldn't apply the changes."))
        self._apply_btn.config(state="disabled", text="Applied" if ok else "Not applied")
        self._cancel_btn.config(state="normal", text="Close")

    def _on_cancel(self):
        if self._applying:
            return
        self.result = False
        self.destroy()

    def _on_close(self):
        self.destroy()

    def destroy(self):
        # on_cancel means "the caller isn't getting an applied plan", so it
        # fires for a cancel, the X, and closing after a failed apply, but never
        # after a successful one. Once only, whichever of those happens.
        first = not self._closed
        self._closed = True
        super().destroy()
        if first and not self.result and self._on_cancel_cb:
            self._on_cancel_cb(self)


class CommunityModsWindow(tk.Toplevel):
    """Table-based browser for community mods pulled from the configured sources.
    Lists every available mod, plus any installed mod that has dropped out of the
    index, with its status, and offers install / update / reinstall / uninstall on
    the selected row. All network and disk work runs off the UI thread; the parent
    Mods window is refreshed through on_change after anything changes."""

    _COLS   = ("status", "author", "mod", "installed", "latest", "source")
    _WIDTHS = (100,      120,      170,   84,          80,       120)
    _STATE_WORD = {
        "missing":  "Available",
        "current":  "Installed",
        "outdated": "Update ready",
        "unknown":  "Installed",
    }
    _PRIMARY_LABEL = {
        "missing":  "Install",
        "outdated": "Update",
        "current":  "Reinstall",
        "unknown":  "Reinstall",
    }

    def __init__(self, parent, game_dir, side="client", on_change=None):
        super().__init__(parent)
        self.title("Community Mods")
        self.configure(bg=BG)
        self.geometry("720x460")
        self.minsize(620, 360)
        self._game_dir  = game_dir
        self._side      = side
        self._on_change = on_change
        self._index     = []      # list[ModSummary], the merged raw index
        self._rows      = {}      # id -> row dict (see _rebuild_rows)
        self._busy      = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _enable_dark_titlebar(self)
        self.transient(parent)
        self._load(force=False)

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Community Mods", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        _btn(h, "Manage Sources", self._open_sources, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=12)
        if self._side == "client":
            # Pinning is a join-time concept - only the client renders a
            # per-server active set at all; a launcher-run server just has one
            # fixed Mods/, with nothing to pin against.
            _btn(h, "Pinned Mods", self._open_pinned, style="dim",
                 font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=(12,0))
        # Modlist import/export applies to both sides - a client's
        # Mods/ and a launcher-run server's Mods/ are both real installed sets
        # that can be exported, and either can import one (the same file a
        # headless server's /modlist can point at directly, with zero
        # conversion - see export_modlist/import_modlist).
        _btn(h, "Import Modlist", self._on_import_modlist, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=(12,0))
        _btn(h, "Export Modlist", self._on_export_modlist, style="dim",
             font=("Segoe UI",9), pady=4, padx=10).pack(side="right", padx=(12,0))
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Mods available from your configured sources. Pick one, then use "
                 "the buttons below to install, update, or remove it. Sources other "
                 "than Modding Tavern are not vetted; you install from them at your "
                 "own risk.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=16, pady=(10,6))

        table_wrap = tk.Frame(self, bg=BG)
        table_wrap.pack(fill="both", expand=True, padx=16)
        self._tree = _mk_tree(table_wrap, self._COLS, self._WIDTHS, height=10)
        # Colour each row by its state so status reads at a glance without icons.
        self._tree.tag_configure("missing",  foreground=PARCH)
        self._tree.tag_configure("current",  foreground=GREEN)
        self._tree.tag_configure("outdated", foreground=AMBER)
        self._tree.tag_configure("unknown",  foreground=MUTED)
        self._tree.tag_configure("disabled", foreground=MUTED)
        self._tree.bind("<<TreeviewSelect>>", lambda e: self._sync_buttons())
        self._tree.bind("<Double-1>", lambda e: self._on_primary())

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(8,4))
        self._primary_btn = _btn(bar, "Install", self._on_primary, style="primary",
                                 font=("Segoe UI",9), pady=5, padx=14)
        self._primary_btn.pack(side="left")
        self._uninstall_btn = _btn(bar, "Uninstall", self._on_uninstall, style="danger",
                                   font=("Segoe UI",9), pady=5, padx=14)
        self._uninstall_btn.pack(side="left", padx=8)
        self._toggle_btn = _btn(bar, "Disable", self._on_toggle, style="normal",
                                font=("Segoe UI",9), pady=5, padx=14)
        self._toggle_btn.pack(side="left")
        self._refresh_btn = _btn(bar, "Refresh", lambda: self._load(force=True),
                                 style="dim", font=("Segoe UI",9), pady=5, padx=12)
        self._refresh_btn.pack(side="right")
        if self._side == "client":
            self._clear_cache_btn = _btn(bar, "Clear Mod Cache", self._on_clear_cache,
                                         style="dim", font=("Segoe UI",9), pady=5, padx=12)
            self._clear_cache_btn.pack(side="right", padx=(0,8))

        self._status = tk.StringVar(value="Loading community mods...")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=16, pady=(0,10))
        self._sync_buttons()

    # -- data loading --

    def _load(self, force=False):
        self._set_busy(True, "Loading community mods...")
        def worker():
            try:
                repos = list_repos(_load_cfg())
                index = fetch_indexes(repos, force=force)
                available = list_mods(index, self._side)
                installed = list_installed_mods(self._game_dir)
                self.after(0, lambda: self._render(index, available, installed))
            except Exception as e:
                self.after(0, lambda e=e: self._load_failed(e))
        threading.Thread(target=worker, daemon=True).start()

    def _load_failed(self, err):
        self._set_busy(False, f"Couldn't load community mods: {err}")

    def _render(self, index, available, installed):
        self._index = index
        self._rebuild_rows(available, installed)
        self._populate_tree()
        self._set_busy(False, "")
        self._refresh_states()

    def _rebuild_rows(self, available, installed):
        rows = {}
        for s in available:
            rows[s.id] = {
                "name": s.name, "author": s.author or "",
                "latest": s.highest(), "source": self._source_label(s.source_repo),
                "summary": s, "installed": None, "state": "missing", "enabled": None,
            }
        # Installed mods that have dropped out of every index still need to be
        # manageable (uninstall/toggle), so fold them in even without a summary.
        for meta in installed:
            mod_id = meta.get("id")
            if not mod_id or not self._meta_matches_side(meta):
                continue                    # library sidecar (no id) or other side
            row = rows.get(mod_id)
            if row is None:
                rows[mod_id] = {
                    "name": mod_id, "author": "", "latest": "-", "source": "",
                    "summary": None, "installed": meta.get("version", "?"),
                    "state": "unknown", "enabled": meta.get("enabled", True),
                }
            else:
                row["installed"] = meta.get("version", "?")
                row["enabled"] = meta.get("enabled", True)
        self._rows = rows

    def _meta_matches_side(self, meta):
        key = "client_side" if self._side == "client" else "server_side"
        return meta.get(key, True)

    def _source_label(self, source_repo):
        if _is_default(source_repo):
            return "Modding Tavern"
        return urlparse(source_repo).netloc or source_repo

    # -- table rendering --

    def _status_word(self, r):
        # A disabled mod reads "Disabled" regardless of its version state; the
        # version comparison is preserved on the row and shown again once enabled.
        if r.get("enabled") is False:
            return "Disabled"
        return self._STATE_WORD.get(r["state"], "")

    def _row_tag(self, r):
        return "disabled" if r.get("enabled") is False else r["state"]

    def _row_values(self, mod_id):
        r = self._rows[mod_id]
        return (self._status_word(r), r["author"], r["name"],
                r["installed"] or "-", r["latest"], r["source"])

    def _populate_tree(self):
        keep = self._selected_id()
        self._tree.delete(*self._tree.get_children())
        for mod_id in sorted(self._rows, key=lambda i: self._rows[i]["name"].lower()):
            self._tree.insert("", "end", iid=mod_id,
                              values=self._row_values(mod_id),
                              tags=(self._row_tag(self._rows[mod_id]),))
        if keep and self._tree.exists(keep):
            self._tree.selection_set(keep)
        self._sync_buttons()

    def _refresh_states(self):
        if not self._rows:
            return
        ids, index, game_dir = list(self._rows), self._index, self._game_dir
        def worker():
            states = {}
            for mod_id in ids:
                try:
                    states[mod_id] = mod_status(game_dir, mod_id, index)
                except Exception:
                    states[mod_id] = "unknown"
            self.after(0, lambda: self._apply_states(states))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_states(self, states):
        for mod_id, st in states.items():
            if mod_id in self._rows:
                self._rows[mod_id]["state"] = st
                if self._tree.exists(mod_id):
                    self._tree.item(mod_id, values=self._row_values(mod_id),
                                    tags=(self._row_tag(self._rows[mod_id]),))
        self._sync_buttons()

    # -- selection / buttons --

    def _selected_id(self):
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _sync_buttons(self):
        sel = self._selected_id()
        r = self._rows.get(sel) if sel else None
        if self._busy or r is None:
            self._primary_btn.config(state="disabled")
            self._uninstall_btn.config(state="disabled")
            self._toggle_btn.config(state="disabled")
            return
        state = r["state"]
        self._primary_btn.config(
            text=self._PRIMARY_LABEL.get(state, "Install"),
            state="normal" if r["summary"] is not None else "disabled")
        installed = r["installed"] not in (None, "-")
        self._uninstall_btn.config(state="normal" if installed else "disabled")
        # Toggle: label reflects what the click will do; only an installed row
        # (enabled True/False) can be toggled; a not-installed row (enabled None)
        # leaves the button disabled.
        enabled = r.get("enabled")
        self._toggle_btn.config(
            text="Enable" if enabled is False else "Disable",
            state="normal" if enabled in (True, False) else "disabled")

    # -- actions --

    def _on_primary(self):
        if self._busy:
            return
        r = self._rows.get(self._selected_id())
        if not r or r["summary"] is None:
            return
        mod = r["summary"]
        if not _melonloader_installed(self._game_dir):
            messagebox.showwarning("Install MelonLoader first",
                f"{mod.name} is a MelonLoader mod. Install MelonLoader from the "
                "Mods window first.", parent=self)
            return
        if not _tavernlib_installed(self._game_dir):
            messagebox.showwarning("Install TavernLib first",
                f"{mod.name} needs TavernLib. Install it from the Mods window "
                "first.", parent=self)
            return
        self._set_busy(True, f"Installing {mod.name}...")
        index = self._index
        def worker():
            try:
                repos = list_repos(_load_cfg())
                install_mod_closure(
                    self._game_dir, mod, index, repos,
                    lambda m: self.after(0, lambda: self._status.set(m)))
                self.after(0, lambda: self._finish(f"{mod.name} installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Install failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_uninstall(self):
        if self._busy:
            return
        sel = self._selected_id()
        r = self._rows.get(sel) if sel else None
        if not r or r["installed"] in (None, "-"):
            return
        if not messagebox.askyesno("Remove mod",
                f"Remove {r['name']} from your game?\n\nThis deletes its folder from "
                "Mods/. Libraries shared with other installed mods are left in place.",
                parent=self):
            return
        self._set_busy(True, f"Removing {r['name']}...")
        game_dir, name = self._game_dir, r["name"]
        def worker():
            try:
                removed = uninstall_mod(game_dir, sel)
                self.after(0, lambda: self._finish(
                    f"{name} removed." if removed else f"{name} was not installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Remove failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_toggle(self):
        if self._busy:
            return
        sel = self._selected_id()
        r = self._rows.get(sel) if sel else None
        if not r or r.get("enabled") not in (True, False):
            return
        disabling = r["enabled"] is True
        self._set_busy(True, f"{'Disabling' if disabling else 'Enabling'} {r['name']}...")
        game_dir, name = self._game_dir, r["name"]
        def worker():
            try:
                if disabling:
                    ok = disable_mod(game_dir, sel)
                    msg = f"{name} disabled." if ok else f"Couldn't disable {name}."
                else:
                    ok = enable_mod(game_dir, sel)
                    msg = f"{name} enabled." if ok else f"Couldn't enable {name}."
                self.after(0, lambda: self._finish(msg))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Toggle failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_clear_cache(self):
        if self._busy:
            return
        if not messagebox.askyesno("Clear mod cache",
                "Delete every cached mod and library version on this machine?\n\n"
                "Nothing currently installed in Mods/ is touched - this only clears "
                "the local copies kept so switching servers doesn't re-download. "
                "The next install or server join will fetch fresh from source.",
                parent=self):
            return
        self._set_busy(True, "Clearing mod cache...")
        def worker():
            try:
                clear_mod_cache()
                self.after(0, lambda: self._finish("Mod cache cleared."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Clear cache failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, msg):
        self._set_busy(False, msg)
        # The available set didn't change, only what's on disk, so re-scan
        # installed state locally rather than re-fetching the index.
        self._reconcile_installed()
        self._refresh_states()
        if self._on_change:
            self._on_change()

    def _reconcile_installed(self):
        try:
            available = list_mods(self._index, self._side)
            installed = list_installed_mods(self._game_dir)
        except Exception:
            return
        self._rebuild_rows(available, installed)
        self._populate_tree()

    def _open_sources(self):
        if self._busy:
            return
        ModSourcesWindow(self, on_change=lambda: self._load(force=True))

    def _open_pinned(self):
        if self._busy:
            return
        PinnedModsWindow(self)

    def _on_export_modlist(self):
        if self._busy:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export Modlist", defaultextension=".json",
            filetypes=[("Modlist JSON", "*.json")])
        if not path:
            return
        pin = messagebox.askyesno("Pin exact versions?",
            "Pin every mod to its exact currently-installed version?\n\n"
            "Yes: a reproducible snapshot of this exact setup.\n"
            "No: each mod tracks whatever's latest when this file is later "
            "imported (a living modpack reference).", parent=self)
        try:
            modlist = export_modlist(self._game_dir, _load_cfg(), pin_versions=pin)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(modlist, f, indent=2)
            self._status.set(f"Exported {len(modlist['mods'])} mod(s) to {os.path.basename(path)}.")
        except Exception as e:
            messagebox.showerror("Export failed", str(e), parent=self)

    def _on_import_modlist(self):
        if self._busy:
            return
        path = filedialog.askopenfilename(parent=self, title="Import Modlist",
            filetypes=[("Modlist JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                modlist = json.load(f)
        except Exception as e:
            messagebox.showerror("Import failed", f"Couldn't read that file: {e}", parent=self)
            return

        self._set_busy(True, "Resolving modlist...")
        index = self._index
        def worker():
            try:
                repos = list_repos(_load_cfg())
                plan = import_modlist(modlist, index, repos)
                self.after(0, lambda: self._confirm_import(plan))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Import failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _confirm_import(self, plan):
        self._set_busy(False)
        all_mods = plan.roots + plan.dependencies
        lines = []
        if all_mods:
            lines.append("Will install:")
            lines += [f"  • {m.id} {m.version}" for m in all_mods]
        if plan.unresolved:
            lines.append("Could not resolve (add the right source, then re-import):")
            lines += [f"  • {mid}" + (f" {v}" if v else "") for mid, v in plan.unresolved]
            if plan.hinted_repos:
                lines.append("This modlist expects sources: " + ", ".join(plan.hinted_repos))
        if plan.blocking:
            messagebox.showerror("Modlist has unresolved mods", "\n".join(lines), parent=self)
            return
        if not all_mods:
            messagebox.showinfo("Nothing to import",
                "This modlist has nothing new to install.", parent=self)
            return
        if not messagebox.askyesno("Import modlist",
                "\n".join(lines) + "\n\nInstall these now?", parent=self):
            return

        self._set_busy(True, "Installing...")
        game_dir = self._game_dir
        def worker():
            try:
                apply_import(game_dir, plan,
                             lambda m: self.after(0, lambda: self._status.set(m)))
                self.after(0, lambda: self._finish(f"Imported {len(plan.roots)} mod(s)."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish(f"Import failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        self._refresh_btn.config(state="disabled" if busy else "normal")
        if msg or not busy:
            self._status.set(msg)
        self._sync_buttons()
