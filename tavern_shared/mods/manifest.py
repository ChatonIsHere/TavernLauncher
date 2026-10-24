"""The wire shapes: a repository.json summary row (ModSummary), a full
per-version manifest (ModManifest), and a library dependency."""
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.layout import _safe_basename
from tavern_shared.mods.version import _major, _parse_version


# A manifest of any other schema major is skipped rather than mis-parsed.
SUPPORTED_MANIFEST_MAJOR = 1


@dataclass
class LibraryDependency:
    """A pinned, exact file a mod links against, installed flat into UserLibs/
    (native DLLs must be libraries: it's MelonLoader's only native search
    path). No id and no version range; distinct from `dependencies`, which
    name other mods."""
    name: str            # display label only, not a resolvable id
    download_url: str
    sha256: str
    filename: str        # required; no id to default a name from


def parity_required(d):
    """Whether a client joining a server that runs this mod must match its
    exact version (true), or may decline it (false). Read off a manifest,
    record, index entry or handshake entry.

    Mandatory and a real boolean: absent or malformed (including 0/1 or
    "false") raises, never defaults, because guessing is what would let a
    required mod pass as optional. TavernLib's ModParityField.Read throws on
    the same bytes; one answer on both sides matters more than which answer."""
    if "parity_required" not in d:
        raise ModManagerError(
            f"'{d.get('id', '?')}' has no parity_required field. It's required: "
            f"true if a client joining a server running this mod must have this "
            f"exact version, false if the server shouldn't block the join over "
            f"it. A mod installed before this field existed needs reinstalling.")
    value = d["parity_required"]
    if not isinstance(value, bool):
        raise ModManagerError(
            f"'{d.get('id', '?')}' has a parity_required of {value!r}, which "
            f"isn't true or false. It must be a real boolean: true if a client "
            f"joining a server running this mod must have this exact version, "
            f"false if the server shouldn't block the join over it.")
    return value


# The scenes a server can run, by the names a manifest's `scenes` uses.
SCENES = ("normal", "quest", "tutorial")


def mod_scenes(d):
    """Which scenes a mod works in. None (absent or null) means every scene. A
    present value must be a non-empty list of non-empty strings or this
    raises; unknown names are kept, so a later scene doesn't make an older
    launcher refuse the manifest. TavernLib's ModScenesField.Read is the same
    rule."""
    value = d.get("scenes")
    if value is None:
        return None
    if (not isinstance(value, list) or not value
            or not all(isinstance(s, str) and s for s in value)):
        raise ModManagerError(
            f"'{d.get('id', '?')}' has a scenes value of {value!r}. Leave it out "
            f"for a mod that works in every scene, or list the ones it works in, "
            f"out of {', '.join(SCENES)}.")
    return list(dict.fromkeys(value))


def runs_in_scene(scenes, scene):
    """Whether a mod with these scenes works in `scene`. An unknown scene (None)
    never rules a mod out."""
    return scenes is None or scene is None or scene in scenes


def scene_from_arg(scene_arg):
    """The scene for a server's launch argument, as TavernLib's
    SceneIndexPatches maps it."""
    return {"/tutorialScene": "tutorial", "/questScene": "quest"}.get(scene_arg or "", "normal")


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
    scenes: list = None                     # see mod_scenes(); None = every scene

    def install_name(self):
        """The basename a package="dll" mod's file gets in Mods/<id>/: the name it
        was published under (from download_url), unless that fails
        _safe_basename or doesn't end in .dll, in which case f"{id}.dll"."""
        base = os.path.basename(urlparse(self.download_url).path)
        try:
            safe = _safe_basename(base)
        except ModManagerError:
            safe = ""
        return safe if safe.lower().endswith(".dll") else f"{self.id}.dll"


@dataclass
class ModSummary:
    """One (id, major) row of repository.json: enough to browse and pick a
    version. Install-critical facts (download_url, sha256, dependencies) live
    only in the per-version manifest, so the two can't drift."""
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
    scenes: list = None                     # the highest release's; None = every scene

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
    """A ModManifest from a parsed manifest, or None (with a warning) for an
    unsupported schema major or package type. Raises for a supported manifest
    that is structurally broken."""
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
            scenes=mod_scenes(d),
        )
    except KeyError as e:
        raise ModManagerError(f"manifest for {d.get('id', '?')} is missing field {e}.")


def _summary_from_dict(d, mod_id, source_repo):
    """A ModSummary from one repository.json (id, major) entry, or None (with a
    warning) when it has no usable versions or parity_required. The index is
    discovery data, so a bad entry is skipped, not fatal."""
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
    except ModManagerError as e:
        _warn(f"index entry for {mod_id} has no usable parity_required "
              f"({e}), skipped.")
        return None
    try:
        scenes = mod_scenes(d)
    except ModManagerError as e:
        # Display-only here (installs read the manifest's), so a bad value
        # costs the tag, not the mod.
        _warn(f"index entry for {mod_id}: {e} Shown as working in every scene.")
        scenes = None
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
        scenes=scenes,
    )
