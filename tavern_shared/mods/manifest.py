"""repository.json rows (ModSummary), per-version manifests (ModManifest) and
their artifacts, parsed per REPO_STRUCTURE.md's field rules; twin of
TavernLib's manifest parser."""
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from tavern_shared.mods.base import ModManagerError, _major, _parse_version, _warn
from tavern_shared.mods.layout import _safe_basename


SUPPORTED_MANIFEST_MAJOR = 2
TARGETS = ("pc", "server", "quest")
PACKAGES = ("dll", "zip")
SCENES = ("normal", "quest", "tutorial")
# Compiled-manifest key order, which the install record keeps.
_ARTIFACT_KEYS = ("targets", "download_url", "sha256", "package", "library_dependencies")
_LIBRARY_KEYS = ("name", "download_url", "sha256", "filename")


@dataclass
class LibraryDependency:
    name: str
    download_url: str
    sha256: str
    filename: str


def parity_required(d):
    """Read off a manifest, record, index or handshake entry. Absent or not a
    real boolean raises rather than defaults, as TavernLib's
    ModParityField.Read does."""
    meaning = ("true if a client joining a server running this mod must have this "
               "exact version, false if the server shouldn't block the join over it")
    if "parity_required" not in d:
        raise ModManagerError(
            f"'{d.get('id', '?')}' has no parity_required field. It's required: "
            f"{meaning}. A mod installed before this field existed needs reinstalling.")
    value = d["parity_required"]
    if not isinstance(value, bool):
        raise ModManagerError(
            f"'{d.get('id', '?')}' has a parity_required of {value!r}, which "
            f"isn't true or false. It must be a real boolean: {meaning}.")
    return value


def mod_scenes(d):
    """None (every scene), or the listed scenes, unknown names kept. Twin of
    TavernLib's ModScenesField.Read."""
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
    return scenes is None or scene is None or scene in scenes


def scene_from_arg(scene_arg):
    """As TavernLib's SceneIndexPatches maps a server's launch argument."""
    return {"/tutorialScene": "tutorial", "/questScene": "quest"}.get(scene_arg or "", "normal")


@dataclass
class Artifact:
    targets: list                   # the known targets it lists
    download_url: str
    sha256: str                     # lowercase hex
    package: str
    library_dependencies: list      # list[LibraryDependency]

    def install_name(self, mod_id):
        """A dll build's name in its target folder: the published basename if
        safe and a .dll, else f"{mod_id}.dll"."""
        base = os.path.basename(urlparse(self.download_url).path)
        try:
            safe = _safe_basename(base)
        except ModManagerError:
            safe = ""
        return safe if safe.lower().endswith(".dll") else f"{mod_id}.dll"


@dataclass
class ModManifest:
    id: str
    name: str
    version: str
    author: str
    description: str
    dependencies: dict              # id -> minimum version
    artifacts: list                 # the usable list[Artifact]
    source_repo: str                # where it was fetched from, not read from the file
    parity_required: bool
    scenes: list                    # None = every scene
    record_artifacts: list          # every artifact, as the install record keeps it

    def artifact_for(self, target):
        return next((a for a in self.artifacts if target in a.targets), None)


@dataclass
class ModSummary:
    """One (id, major) row of repository.json."""
    id: str
    name: str
    author: str
    description: str
    targets: list
    versions: list                  # every version in this major
    source_repo: str
    parity_required: bool = True
    scenes: list = None

    def highest(self):
        return max(self.versions, key=_parse_version)

    @property
    def major(self):
        return _major(self.highest())


_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def _sha256_field(d, where):
    """Twin of TavernLib's ModDataModel.Sha256Field, message included."""
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
    """(usable Artifacts, every artifact as the record keeps it). A known
    target listed twice raises, even beside an unknown package; an artifact
    with no known target or an unknown package is ignored."""
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
        package = a.get("package")
        if not known:
            continue
        if package not in PACKAGES:
            if warn:
                _warn(f"{where}: the {'/'.join(known)} artifact has unknown package "
                      f"type {package!r} (expected 'dll' or 'zip'), ignored.")
            continue
        usable.append(Artifact(targets=known, download_url=a["download_url"],
                               sha256=sha256, package=package,
                               library_dependencies=libs))
    return usable, kept


def _manifest_from_dict(d, source_repo):
    """None (with a warning) for an unsupported schema major."""
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


def _summary_from_dict(d, mod_id, source_repo):
    """None (with a warning) for an entry with no usable versions or
    parity_required: the index is discovery data, so a bad entry is skipped."""
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
        # Display-only here; installs read the manifest's.
        _warn(f"index entry for {mod_id}: {e} Shown as working in every scene.")
        scenes = None
    targets = d.get("targets")
    return ModSummary(
        id=mod_id,
        name=str(d.get("name", mod_id)),
        author=str(d.get("author", mod_id.split(".", 1)[0])),
        description=str(d.get("description", "")),
        targets=(sorted({t for t in targets if isinstance(t, str) and t in TARGETS})
                 if isinstance(targets, list) else []),
        versions=versions,
        source_repo=source_repo,
        parity_required=required,
        scenes=scenes,
    )
