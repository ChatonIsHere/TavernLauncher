"""The wire shapes: a repository.json summary row (ModSummary), a full
per-version manifest (ModManifest), its per-target builds (Artifact), and a
library dependency. CommunityMods' docs/REPO_STRUCTURE.md is the contract."""
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.layout import _safe_basename
from tavern_shared.mods.version import _major, _parse_version


# A manifest of any other schema major is skipped rather than mis-parsed.
SUPPORTED_MANIFEST_MAJOR = 2


# Every target a manifest may name. Any other name is ignored, so a target
# added later doesn't break this build; "quest" is parsed but never installed.
TARGETS = ("pc", "server", "quest")


# The targets an installer here installs: the client launcher "pc", the
# server launcher "server".
INSTALL_TARGETS = ("pc", "server")


# An artifact with any other package is ignored, like an unknown target.
PACKAGES = ("dll", "zip")


# Compiled-manifest key order, which the install record keeps.
_ARTIFACT_KEYS = ("targets", "download_url", "sha256", "package", "library_dependencies")
_LIBRARY_KEYS = ("name", "download_url", "sha256", "filename")


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
class Artifact:
    """One build of a mod: the file at download_url, installed into
    Mods/<id>/.<target>/ for each target it lists."""
    targets: list                           # the known targets it lists, in manifest order
    download_url: str
    sha256: str                             # lowercase hex
    package: str                            # "dll" (single file) or "zip" (extracted bundle)
    library_dependencies: list              # pinned UserLibs/ files (list[LibraryDependency])

    def install_name(self, mod_id):
        """The basename a package="dll" build gets in its target folder: the
        name it was published under (from download_url), unless that fails
        _safe_basename or doesn't end in .dll, in which case f"{mod_id}.dll"."""
        base = os.path.basename(urlparse(self.download_url).path)
        try:
            safe = _safe_basename(base)
        except ModManagerError:
            safe = ""
        return safe if safe.lower().endswith(".dll") else f"{mod_id}.dll"


@dataclass
class ModManifest:
    manifest_version: int                   # schema version; unknown-major is skipped at parse
    id: str                                 # "<github-user>.<github-repo>", path-safe (single segment)
    name: str
    version: str                            # exactly "MAJOR.MINOR.PATCH"
    author: str
    description: str
    dependencies: dict                      # other MODS: id -> min version, SemVer major-locked
    artifacts: list                         # the usable builds (list[Artifact]); ignored ones dropped
    source_repo: str                        # injected by fetch_indexes, NOT read from the file
    parity_required: bool = True            # see parity_required() above
    scenes: list = None                     # see mod_scenes(); None = every scene
    record_artifacts: list = None           # every artifact, as the install record keeps it

    def artifact_for(self, target):
        """The build for `target`, or None when the mod doesn't support it."""
        return next((a for a in self.artifacts if target in a.targets), None)

    @property
    def targets(self):
        return sorted({t for a in self.artifacts for t in a.targets})


@dataclass
class ModSummary:
    """One (id, major) row of repository.json: enough to browse and pick a
    version. Install-critical facts (download_url, sha256, dependencies) live
    only in the per-version manifest, so the two can't drift."""
    id: str
    name: str
    author: str
    description: str
    targets: list                           # known targets the release has a build for, sorted
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


_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def _sha256_field(d, where):
    """d["sha256"] lowercased, refusing anything but 64 hex characters.
    Same rule and message as TavernLib's ModDataModel.Sha256Field."""
    value = d.get("sha256")
    if not (isinstance(value, str) and _SHA256.fullmatch(value)):
        raise ModManagerError(f"{where} has an invalid sha256 (expected 64 hex characters).")
    return value.lower()


def _library_from_dict(d):
    if not isinstance(d, dict):
        raise ModManagerError(f"library_dependencies entry {d!r} isn't an object.")
    for k in _LIBRARY_KEYS:
        if k not in d:
            raise ModManagerError(f"library_dependencies entry is missing '{k}'.")
    return LibraryDependency(
        name=str(d["name"]),
        download_url=str(d["download_url"]),
        sha256=_sha256_field(d, "library_dependencies entry"),
        filename=str(d["filename"]),
    )


def _in_key_order(d, keys):
    """d with `keys` first, in that order (those present), then any others as
    they came: the record copies artifacts verbatim, in compiled order."""
    out = {k: d[k] for k in keys if k in d}
    out.update((k, v) for k, v in d.items() if k not in out)
    return out


def _record_artifact(a):
    rec = _in_key_order(a, _ARTIFACT_KEYS)
    if isinstance(rec.get("library_dependencies"), list):
        rec["library_dependencies"] = [_in_key_order(l, _LIBRARY_KEYS)
                                       for l in rec["library_dependencies"]]
    return rec


def _artifacts_from_list(raw, mod_id, warn=True):
    """(usable Artifacts, every artifact as the record keeps it). Raises when
    the list or an artifact is malformed, or a known target appears twice.
    An artifact with no known target or an unknown package is ignored (the
    latter with a warning, when `warn`). TavernLib's manifest parser applies
    the same rules."""
    where = f"manifest for {mod_id}"
    if not isinstance(raw, list) or not raw:
        raise ModManagerError(f"{where} has no artifacts; it needs at least one.")
    usable, kept, seen = [], [], set()
    for a in raw:
        if not isinstance(a, dict):
            raise ModManagerError(f"{where} has an artifact that isn't an object.")
        targets = a.get("targets")
        if (not isinstance(targets, list) or not targets
                or not all(isinstance(t, str) for t in targets)):
            raise ModManagerError(f"{where} has an artifact without a list of targets.")
        known = [t for t in targets if t in TARGETS]
        for t in known:
            # Within one artifact too: either way it's two answers for one target.
            if t in seen:
                raise ModManagerError(f"{where} lists the {t} target twice.")
            seen.add(t)
        if not isinstance(a.get("download_url"), str):
            raise ModManagerError(f"{where} has an artifact without a download_url.")
        sha256 = _sha256_field(a, f"{where}'s artifact")
        libs_raw = a.get("library_dependencies", [])
        if not isinstance(libs_raw, list):
            raise ModManagerError(f"{where} has an artifact whose library_dependencies isn't a list.")
        libs = [_library_from_dict(x) for x in libs_raw]
        kept.append(_record_artifact(a))
        # Exactly "dll" or "zip", as TavernLib reads it: the tooling writes it.
        package = a.get("package")
        if not known:
            continue
        if package not in PACKAGES:
            if warn:
                _warn(f"{where}: the {'/'.join(known)} artifact has unknown package "
                      f"type {a.get('package')!r} (expected 'dll' or 'zip'), ignored.")
            continue
        usable.append(Artifact(targets=known, download_url=a["download_url"],
                               sha256=sha256, package=package,
                               library_dependencies=libs))
    return usable, kept


def _manifest_from_dict(d, source_repo):
    """A ModManifest from a parsed manifest, or None (with a warning) for an
    unsupported schema major. Raises for a supported manifest that is
    structurally broken."""
    mv = d.get("manifest_version")
    if not isinstance(mv, int):
        _warn(f"manifest for {d.get('id', '?')} has no integer manifest_version, skipped.")
        return None
    if mv != SUPPORTED_MANIFEST_MAJOR:
        _warn(f"manifest for {d.get('id', '?')} is schema v{mv}, this build "
              f"supports v{SUPPORTED_MANIFEST_MAJOR}, skipped.")
        return None

    try:
        mod_id = str(d["id"])
        artifacts, kept = _artifacts_from_list(d.get("artifacts"), mod_id)
        return ModManifest(
            manifest_version=mv,
            id=mod_id,
            name=str(d.get("name", mod_id)),
            version=str(d["version"]),
            author=str(d.get("author", mod_id.split(".", 1)[0])),
            description=str(d.get("description", "")),
            dependencies=dict(d.get("dependencies", {}) or {}),
            artifacts=artifacts,
            source_repo=source_repo,
            parity_required=parity_required(d),
            scenes=mod_scenes(d),
            record_artifacts=kept,
        )
    except KeyError as e:
        raise ModManagerError(f"manifest for {d.get('id', '?')} is missing field {e}.")


def _known_targets(value):
    """The known target names in a list, sorted; [] for anything else."""
    if not isinstance(value, list):
        return []
    return sorted({t for t in value if isinstance(t, str) and t in TARGETS})


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
        targets=_known_targets(d.get("targets")),
        versions=versions,
        source_repo=source_repo,
        parity_required=required,
        scenes=scenes,
    )
