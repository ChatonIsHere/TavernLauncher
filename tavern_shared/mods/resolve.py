"""Dependency closure and manifest fetching."""
from tavern_shared.mods import repos
from tavern_shared.mods.base import ModManagerError, _parse_version, _warn
from tavern_shared.mods.manifest import _manifest_from_dict
from tavern_shared.mods.repos import _norm


MAX_DEPENDENCY_DEPTH = 5


# (repo base, id, version) -> ModManifest. Per-version manifests are immutable,
# so successes are kept for the process; latest*.json pointers never are.
_manifest_memo = {}


def resolve_dependencies(roots, summary_index, repo_bases, target):
    """The closure of `roots` for `target`, roots excluded: per dependency, the
    highest indexed version in the required major at or above the running
    minimum. A dependency with no `target` build is pruned, with everything
    only it needed, before its constraints count. Raises on a cycle, a chain
    deeper than MAX_DEPENDENCY_DEPTH, a cross-major conflict or a missing
    version."""
    summ = {(s.id, s.major): s for s in summary_index}
    root_ids = {r.id for r in roots}
    chosen = {r.id: r for r in roots}   # id -> ModManifest selected
    req_major = {}       # id -> locked major
    req_min = {}         # id -> highest minimum required so far
    req_by = {}          # id -> (first requirer, its constraint), for messages
    pruned = set()

    def add_requirement(dep_id, min_v, requirer, chain):
        if dep_id in pruned:
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

        new_min = max(req_min.get(dep_id, mv), mv)
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
        fresh = prev is None or prev.version != pick_version
        if fresh:
            manifest = _fetch_manifest(repo_bases, dep_id, pick_version, summary.source_repo)
            if manifest.artifact_for(target) is None:
                pruned.add(dep_id)
                _warn(f"{requirer} depends on '{dep_id}' {pick_version}, which has no "
                      f"{target} build; skipped, along with anything only it needed.")
                return
        req_major[dep_id] = major
        req_min[dep_id] = new_min
        req_by.setdefault(dep_id, (requirer, min_v))
        if fresh:
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


def _index_source(index, mod_id, repo_bases, version=None):
    """The trusted repo the index found mod_id in (preferring one listing
    `version`), to try first; never a repo outside repo_bases."""
    trusted = {_norm(b) for b in repo_bases}
    hits = [s for s in index if s.id == mod_id and _norm(s.source_repo) in trusted]
    for s in hits:
        if version is None or version in s.versions:
            return s.source_repo
    return hits[0].source_repo if hits else None


def _fetch_manifest_leaf(repo_bases, mod_id, leaf, prefer_repo=None, what=None,
                         expect_version=None):
    """The first repo's manifests/<author>/<repo>/<leaf> that parses, trying
    prefer_repo first. With expect_version, a manifest naming another id or
    version fails that repo like a 404 (as in TavernLib), and successes are
    memoized."""
    if "." not in mod_id:
        raise ModManagerError(
            f"Mod id {mod_id!r} isn't '<github-user>.<github-repo>'.")
    author, repo = mod_id.split(".", 1)
    order = list(dict.fromkeys(_norm(b) for b in ([prefer_repo] if prefer_repo else []) + repo_bases))
    last_err = None
    for base in order:
        key = (base, mod_id, expect_version)
        if expect_version is not None and key in _manifest_memo:
            return _manifest_memo[key]
        url = f"{base}/manifests/{author}/{repo}/{leaf}"
        try:
            data = repos._get_json(url)
        except ModManagerError as e:
            last_err = e
            continue
        m = _manifest_from_dict(data, base)
        if m is None:
            continue
        if expect_version is not None:
            if (m.id, m.version) != (mod_id, expect_version):
                last_err = ModManagerError(
                    f"{url} serves '{m.id}' {m.version}, not the requested "
                    f"'{mod_id}' {expect_version}.")
                continue
            _manifest_memo[key] = m
        return m
    raise ModManagerError(
        f"Couldn't find {what or mod_id} in any configured repository."
        + (f" Last error: {last_err}" if last_err else ""))


def _fetch_manifest(repo_bases, mod_id, version, prefer_repo=None):
    return _fetch_manifest_leaf(
        repo_bases, mod_id, f"{version}.json", prefer_repo,
        what=f"mod '{mod_id}' version {version}", expect_version=version)


def resolve_mod_by_id(repo_bases, mod_id, prefer_repo=None):
    """The latest manifest of a mod, via latest.json."""
    return _fetch_manifest_leaf(repo_bases, mod_id, "latest.json", prefer_repo,
                                what=f"mod '{mod_id}'")
