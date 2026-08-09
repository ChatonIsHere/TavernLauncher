"""Dependency closure and by-id resolution (latest.json / latest.<major>.json)."""
from tavern_shared.mods import repos
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.manifest import _manifest_from_dict
from tavern_shared.mods.repos import _norm
from tavern_shared.mods.version import _parse_version

# _get_json is reached through the module (repos._get_json) rather than
# imported by name on purpose: it is the network seam the tests stub, and a
# by-name import would bind a second copy here that stubbing repos wouldn't
# reach. _norm is a pure function nothing ever replaces, so it imports plainly.


# A backstop distinct from cycle detection: a real cycle (A -> B -> A) is caught
# regardless of this number, but a long strictly-acyclic chain would still
# recurse once per link, and a broken/adversarial chain doesn't have to be a
# cycle to be a problem. Kept deliberately low: real chains are 1 to 2 hops,
# occasionally 3; anything hitting this cap is almost certainly a mistake. Turns
# a pathological chain into one clear "too deep" error instead of a raw
# RecursionError.
MAX_DEPENDENCY_DEPTH = 5


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
            data = repos._get_json(url)
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
