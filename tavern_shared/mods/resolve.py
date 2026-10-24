"""Dependency closure and by-id resolution (latest.json / latest.<major>.json)."""
from tavern_shared.mods import repos
from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.manifest import INSTALL_TARGETS, _manifest_from_dict
from tavern_shared.mods.repos import _norm
from tavern_shared.mods.version import _parse_version

# repos._get_json is reached through the module: it's the network seam tests
# stub, and a by-name import wouldn't see the stub.


# A backstop for long acyclic chains (cycles are caught regardless). Real
# chains are one to three hops.
MAX_DEPENDENCY_DEPTH = 5


# Per-version manifests are immutable by contract, so a fetched one is kept
# for the life of the process: (normalized repo base, id, version) -> manifest.
# Successes only; the latest*.json pointers move and are never memoized.
_manifest_memo = {}


def _clear_manifest_memo():
    _manifest_memo.clear()


def resolve_dependencies(roots, summary_index, fetch_manifest, target):
    """The dependency closure of one or more roots as a flat, deduplicated
    list of ModManifests, roots excluded. Several roots at once is what lets
    two independent required mods be caught pulling incompatible versions of
    a shared dependency.

    Minimum-version-with-major-lock: for each dependency, the highest version
    summary_index lists in the required major that is >= the running minimum.
    fetch_manifest(id, version, source_repo) loads only what the closure uses.

    `target` ("pc"/"server", no default) is what's being installed. A
    dependency with no build for it is pruned, with everything only it
    needed, before its constraints are recorded, and reported via _warn.
    Roots aren't filtered: they're what the caller asked for.

    Raises, naming the mods involved, on a cycle, a chain deeper than
    MAX_DEPENDENCY_DEPTH, a cross-major (diamond) conflict, or a missing
    version."""
    if target not in INSTALL_TARGETS:
        raise ModManagerError(
            f"resolve_dependencies needs target="
            f"{' or '.join(map(repr, INSTALL_TARGETS))}, got {target!r}.")

    summ = {}            # (id, major) -> ModSummary
    for s in summary_index:
        summ[(s.id, s.major)] = s

    root_ids = {r.id for r in roots}
    # Seeded with the roots, so a root another root depends on at the same
    # version isn't fetched and walked a second time.
    chosen = {r.id: r for r in roots}   # id -> ModManifest currently selected
    req_major = {}       # id -> locked major
    req_min = {}         # id -> highest min-version tuple required so far
    req_by = {}          # id -> (mod name, constraint str) of first requirer, for messages
    pruned = {}          # id -> the ModManifest with no `target` build, fetched at most once

    def add_requirement(dep_id, min_v, requirer, chain):
        if dep_id in pruned:
            # Reported for every requirer, fetched and walked once.
            _warn(f"{requirer} depends on '{dep_id}', which has no {target} "
                  f"build; skipped.")
            return

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

        def record():
            req_major[dep_id] = major
            req_min[dep_id] = new_min
            req_by.setdefault(dep_id, (requirer, min_v))

        prev = chosen.get(dep_id)
        if prev is not None and prev.version == pick_version:
            record()         # already selected at this exact version
            return

        manifest = fetch_manifest(dep_id, pick_version, summary.source_repo)
        if manifest.artifact_for(target) is None:
            # Before record(): a mod we aren't installing can't raise a diamond
            # conflict, and nothing only it needs is walked.
            pruned[dep_id] = manifest
            _warn(f"{requirer} depends on '{dep_id}' {pick_version}, which has no "
                  f"{target} build; skipped, along with anything only it needed.")
            return

        record()
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

    return [m for mid, m in chosen.items() if mid not in root_ids]


def _repo_order(repo_bases, prefer_repo):
    """prefer_repo first, then the rest of repo_bases, de-duplicated."""
    order = []
    if prefer_repo:
        order.append(prefer_repo)
    for b in repo_bases:
        if _norm(b) not in [_norm(x) for x in order]:
            order.append(b)
    return order


def _index_source(index, mod_id, repo_bases, version=None):
    """The repo the merged index found mod_id in (preferring the entry that
    lists `version`), so a fetch can try it first instead of 404ing through
    the default repo. Only ever one of repo_bases: a hint can't add a source."""
    trusted = {_norm(b) for b in repo_bases}
    hits = [s for s in index if s.id == mod_id and _norm(s.source_repo) in trusted]
    for s in hits:
        if version is None or version in s.versions:
            return s.source_repo
    return hits[0].source_repo if hits else None


def _fetch_manifest_leaf(repo_bases, mod_id, leaf, prefer_repo=None, what=None,
                         expect_version=None):
    """Fetches manifests/<author>/<repo>/<leaf> from each repo in preference
    order and returns the first that parses; <author>/<repo> is mod_id split
    on its first '.'. With expect_version, a manifest naming another id or
    version fails that repo like a 404 does, and successes are memoized.
    Raises if no repo has it."""
    if "." not in mod_id:
        raise ModManagerError(
            f"Mod id {mod_id!r} isn't '<github-user>.<github-repo>'.")
    author, repo = mod_id.split(".", 1)
    last_err = None
    for base in _repo_order(repo_bases, prefer_repo):
        key = (_norm(base), mod_id, expect_version)
        if expect_version is not None and key in _manifest_memo:
            return _manifest_memo[key]
        url = f"{_norm(base)}/manifests/{author}/{repo}/{leaf}"
        try:
            data = repos._get_json(url)
        except ModManagerError as e:
            last_err = e
            continue
        m = _manifest_from_dict(data, _norm(base))
        if m is None:
            continue
        if expect_version is not None and (m.id, m.version) != (mod_id, expect_version):
            last_err = ModManagerError(
                f"{url} serves '{m.id}' {m.version}, not the requested "
                f"'{mod_id}' {expect_version}.")
            continue
        if expect_version is not None:
            _manifest_memo[key] = m
        return m
    raise ModManagerError(
        f"Couldn't find {what or mod_id} in any configured repository."
        + (f" Last error: {last_err}" if last_err else ""))


def _fetch_manifest(repo_bases, mod_id, version, prefer_repo=None):
    """The manifest for an exact version (memoized; see _manifest_memo)."""
    return _fetch_manifest_leaf(
        repo_bases, mod_id, f"{version}.json", prefer_repo,
        what=f"mod '{mod_id}' version {version}", expect_version=version)


def resolve_mod_by_id(repo_bases, mod_id, prefer_repo=None, major=None):
    """Fetches a single mod's manifest by id via the latest*.json pointer files,
    without downloading the full repository.json. If major is given, fetches
    latest.<major>.json; otherwise latest.json."""
    leaf = f"latest.{major}.json" if major is not None else "latest.json"
    what = f"mod '{mod_id}'" + (f" (major {major})" if major is not None else "")
    return _fetch_manifest_leaf(repo_bases, mod_id, leaf, prefer_repo, what=what)
