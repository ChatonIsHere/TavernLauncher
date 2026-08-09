"""The wire shapes: a repository.json summary row (ModSummary), a full
per-version manifest (ModManifest), and a library dependency."""
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.layout import _safe_basename
from tavern_shared.mods.version import _major, _parse_version


# The manifest schema major this build understands. A per-version manifest whose
# manifest_version has a *different* major is skipped rather than mis-parsed. This
# is the forward-compat hook that lets the manifest schema grow later (multi-file
# mods, new fields) without an old launcher guessing at a shape it doesn't get.
# Bump this only alongside a real manifest schema change.
SUPPORTED_MANIFEST_MAJOR = 1


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
