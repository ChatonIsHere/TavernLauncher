"""Tests for the community mod manager (tavern_shared/mods/). The network
(repos._get_json) and the download/hash helpers are stubbed per test."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared.mods import (
    base, cache, install, layout, manifest, modlist, pins, plan, repos,
    resolve,
)
from conftest import install_mod_closure, stub, temp_dir
from tavern_shared import paths


class _Facade:
    """Reads any mod-manager name off whichever submodule defines it, so the
    tests can say mm.<name>. Writes raise: a stub has to patch the owning
    module (see conftest.stub), or it silently stubs nothing."""

    _MODULES = (base, layout, manifest, repos, resolve,
                install, cache, pins, plan, modlist)

    def __getattr__(self, name):
        for m in self._MODULES:
            try:
                return getattr(m, name)
            except AttributeError:
                pass
        raise AttributeError(f"no tavern_shared.mods submodule defines {name!r}")

    def __setattr__(self, name, _value):
        raise AttributeError(
            f"patch the owning module, not the facade: {name} "
            f"(e.g. repos._get_json = ...)")


mm = _Facade()


DEFAULT = mm.DEFAULT_REPO
OTHER = "https://raw.githubusercontent.com/someone/theirmods/main"


# builders

def default_url(mod_id, version):
    author, repo = (mod_id.split(".", 1) + ["repo"])[:2]
    return f"https://github.com/{author}/{repo}/releases/download/v{version}/{mod_id}.dll"


# Overrides manifest_dict puts on its artifact rather than the manifest.
_ARTIFACT_FIELDS = ("download_url", "sha256", "package", "library_dependencies")


def manifest_dict(mod_id, version, targets=("client_windows", "server"), **over):
    """A v2 manifest with one dll artifact for `targets`; download_url,
    sha256, package and library_dependencies overrides land on it."""
    author, repo = (mod_id.split(".", 1) + ["repo"])[:2]
    artifact = {
        "targets": list(targets),
        "download_url": default_url(mod_id, version),
        "sha256": "0" * 64,
        "package": "dll",
        "library_dependencies": [],
    }
    for k in _ARTIFACT_FIELDS:
        if k in over:
            artifact[k] = over.pop(k)
    d = {
        "manifest_version": 2,
        "id": mod_id,
        "name": repo,
        "version": version,
        "author": author,
        "description": "",
        "parity_required": True,
        "dependencies": {},
        "artifacts": [artifact],
    }
    d.update(over)
    return d


def make_manifest(mod_id, version, base=DEFAULT, **over):
    return mm._manifest_from_dict(manifest_dict(mod_id, version, **over), base)


def summary(mod_id, versions, targets=("client_windows", "server"), source=DEFAULT, name=None,
            description=""):
    return mm.ModSummary(
        id=mod_id, name=name or mod_id.split(".", 1)[-1],
        author=mod_id.split(".", 1)[0], description=description,
        targets=sorted(targets), versions=list(versions), source_repo=source)


def slim_entry(versions, name="M", author="a", description="", targets=("client_windows", "server"),
               parity_required=True):
    return {"name": name, "author": author, "description": description,
            "parity_required": parity_required, "targets": sorted(targets),
            "versions": list(versions)}


def slim_index(mods):
    return {"index_version": 2, "mods": mods}


def _srv(mod_id, version, targets=("client_windows", "server"), required=None, source_repo=None):
    """One handshake mods-list entry, as plan_join receives it. parity_required
    is always stated; its absence is its own contract (a raise)."""
    entry = {"id": mod_id, "version": version,
             "parity_required": True if required is None else required,
             "targets": sorted(targets), "target_sha256": {}}
    if source_repo is not None:
        entry["source_repo"] = source_repo
    return entry


def lib_dep(name):
    """A library_dependencies entry for UserLibs/<name>.dll; _publish fills in
    the sha256 the fake download will produce."""
    return {"name": name, "download_url": f"https://x/{name}.dll",
            "sha256": "", "filename": f"{name}.dll"}


def store(*manifests, fetched=None):
    """A fetch_manifest over these manifests, recording fetched ids if asked."""
    by_key = {(m.id, m.version): m for m in manifests}

    def fetch(mid, ver, _src):
        if fetched is not None:
            fetched.append(mid)
        return by_key[(mid, ver)]
    return fetch


def noop(*_a):
    pass


def resolve_deps(test, roots, index, fetch, target):
    """resolve_dependencies with fetch(id, version, source_repo) as the network."""
    stub(test, resolve, "_fetch_manifest",
         lambda _bases, mid, ver, prefer_repo=None: fetch(mid, ver, prefer_repo))
    return mm.resolve_dependencies(roots, index, [], target)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class VersionAndPath(unittest.TestCase):
    def test_numeric_ordering_not_lexical(self):
        self.assertLess(mm._parse_version("1.9.0"), mm._parse_version("1.10.0"))
        self.assertGreater(mm._parse_version("2.0.0"), mm._parse_version("1.99.99"))

    def test_safe_basename_rejects_traversal(self):
        bad = ["..", ".", "", "a/b.dll", "../x.dll", "sub/x.dll",
               "a" + chr(92) + "b.dll"]   # backslash
        for name in bad:
            with self.assertRaises(mm.ModManagerError):
                mm._safe_basename(name)
        self.assertEqual(mm._safe_basename("Fine.dll"), "Fine.dll")

    def test_safe_basename_rejects_colons(self):
        """A drive-relative name, or an NTFS alternate data stream: neither is a
        directory part to os.path.basename."""
        for name in ("C:evil.dll", "mod.dll:payload", "x:", ":stream"):
            with self.assertRaises(mm.ModManagerError):
                mm._safe_basename(name)


class ManifestParsing(unittest.TestCase):
    def test_unknown_major_is_skipped_not_misparsed(self):
        for major in (1, 3):
            self.assertIsNone(mm._manifest_from_dict(
                manifest_dict("a.b", "1.0.0", manifest_version=major), DEFAULT))

    def test_sha256_must_be_64_hex_for_mods_and_libraries(self):
        """Same rule as TavernLib's ModDataModel.Sha256Field."""
        lib = {"name": "L", "download_url": "https://x/l.dll", "filename": "l.dll"}
        for bad in (None, "", "abc", "g" * 64, "a" * 63, 5):
            with self.subTest(sha256=bad):
                with self.assertRaises(mm.ModManagerError):
                    make_manifest("a.b", "1.0.0", sha256=bad)
                with self.assertRaises(mm.ModManagerError):
                    make_manifest("a.b", "1.0.0",
                                  library_dependencies=[dict(lib, sha256=bad)])
        self.assertEqual(make_manifest("a.b", "1.0.0", sha256="AB" * 32).artifacts[0].sha256,
                         "ab" * 32)

    def test_install_name_keeps_published_name_and_guards_traversal(self):
        u = "https://github.com/a/b/releases/download/v1"
        # keeps the published asset name
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url=f"{u}/CoolMod-v1.0.0.dll").artifacts[0].install_name("a.b"),
            "CoolMod-v1.0.0.dll")
        # non-.dll basename falls back to <id>.dll
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url=f"{u}/download").artifacts[0].install_name("a.b"), "a.b.dll")
        # traversal segments are stripped to a bare basename
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url="https://evil/../../etc/passwd").artifacts[0].install_name("a.b"),
            "a.b.dll")
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url="https://evil/x/..").artifacts[0].install_name("a.b"), "a.b.dll")
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url=f"{u}/../../../Sneaky.dll").artifacts[0].install_name("a.b"),
            "Sneaky.dll")


class SummaryParsing(unittest.TestCase):
    def test_summary_from_dict_reads_versions(self):
        s = mm._summary_from_dict(slim_entry(["1.0.0", "1.2.0"]), "a.b", DEFAULT)
        self.assertEqual(s.highest(), "1.2.0")
        self.assertEqual(s.major, 1)

    def test_summary_drops_bad_versions_keeps_good(self):
        s = mm._summary_from_dict(slim_entry(["1.0.0", "not-a-version"]), "a.b", DEFAULT)
        self.assertEqual(s.versions, ["1.0.0"])

    def test_summary_none_when_no_usable_versions(self):
        self.assertIsNone(mm._summary_from_dict(slim_entry([]), "a.b", DEFAULT))
        self.assertIsNone(mm._summary_from_dict({"name": "x"}, "a.b", DEFAULT))


class Collisions(unittest.TestCase):
    def setUp(self):
        mm._index_cache.clear()

    def _serve(self, mapping):
        def fake(url, timeout=15):
            for base, idx in mapping.items():
                if url == f"{base}/repository.json":
                    return idx
            raise mm.ModManagerError(f"no fake for {url}")
        stub(self, repos, "_get_json", fake)

    def test_default_repo_wins_collision(self):
        # same major, so a genuine collision; default wins despite the lower version
        self._serve({
            DEFAULT: slim_index({"a.b": {"1": slim_entry(["1.0.0"], name="from-default")}}),
            OTHER: slim_index({"a.b": {"1": slim_entry(["1.9.9"], name="from-other")}}),
        })
        merged = mm.fetch_indexes([DEFAULT, OTHER], force=True)
        picked = [s for s in merged if s.id == "a.b"]
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].name, "from-default")

    def test_highest_wins_among_non_default(self):
        third = "https://raw.githubusercontent.com/third/x/main"
        self._serve({
            OTHER: slim_index({"a.b": {"1": slim_entry(["1.2.0"])}}),
            third: slim_index({"a.b": {"1": slim_entry(["1.5.0"])}}),
        })
        merged = mm.fetch_indexes([OTHER, third], force=True)
        picked = [s for s in merged if s.id == "a.b"]
        self.assertEqual(picked[0].highest(), "1.5.0")

    def test_different_majors_both_kept(self):
        self._serve({DEFAULT: slim_index(
            {"a.b": {"1": slim_entry(["1.4.0"]), "2": slim_entry(["2.1.0"])}})})
        merged = mm.fetch_indexes([DEFAULT], force=True)
        highs = sorted(s.highest() for s in merged if s.id == "a.b")
        self.assertEqual(highs, ["1.4.0", "2.1.0"])

    def test_unsupported_index_major_skipped_whole(self):
        self._serve({DEFAULT: {"index_version": 1, "mods": {
            "a.b": {"1": slim_entry(["1.0.0"])}}}})
        self.assertEqual(mm.fetch_indexes([DEFAULT], force=True), [])


class RepoSourcesConfig(unittest.TestCase):
    """User repos are stored by their "Author/Repo" shorthand, which only
    add_repo's live check ever produces."""

    def setUp(self):
        def fake(url, timeout=15):
            if url == f"{OTHER}/repository.json":
                return slim_index({})
            raise mm.ModManagerError(f"no fake for {url}")
        stub(self, repos, "_get_json", fake)

    def test_shorthand_derived_from_url(self):
        self.assertEqual(mm._repo_shorthand(OTHER), "someone/theirmods")
        self.assertEqual(mm._repo_shorthand(mm.DEFAULT_REPO), "ModdingTavern/TavernMods")

    def test_source_display_github_uses_shorthand(self):
        self.assertEqual(mm._source_display(OTHER), "someone/theirmods")
        self.assertEqual(mm._source_display(mm.DEFAULT_REPO), "ModdingTavern/TavernMods")

    def test_source_display_non_github_uses_full_url(self):
        url = "https://example.com/mods/someone/theirmods"
        self.assertEqual(mm._source_display(url), url)

    def test_own_mod_matches_source_owner(self):
        self.assertTrue(mm.is_own_mod("ModdingTavern.TavernEssentials", mm.DEFAULT_REPO))
        self.assertTrue(mm.is_own_mod("moddingtavern.TavernVoice", mm.DEFAULT_REPO))
        self.assertFalse(mm.is_own_mod("Nyx12012.CookingLib", mm.DEFAULT_REPO))
        self.assertTrue(mm.is_own_mod("someone.Thing", OTHER))
        self.assertFalse(mm.is_own_mod("someone.Thing", "https://example.com/someone/theirmods"))

    def test_add_repo_stores_by_shorthand(self):
        cfg = {}
        mm.add_repo(cfg, OTHER)
        self.assertEqual(cfg[mm.CFG_REPOS_KEY], {"someone/theirmods": OTHER})
        self.assertIn(OTHER, mm.list_repos(cfg))

    def test_add_repo_rejects_duplicate(self):
        cfg = {}
        mm.add_repo(cfg, OTHER)
        with self.assertRaises(mm.ModManagerError):
            mm.add_repo(cfg, OTHER)

    def test_add_repo_rejects_shorthand_collision_with_different_url(self):
        cfg = {mm.CFG_REPOS_KEY: {"someone/theirmods": "https://raw.githubusercontent.com/someone/theirmods/old-branch"}}
        with self.assertRaises(mm.ModManagerError):
            mm.add_repo(cfg, OTHER)

    def test_remove_repo_by_shorthand_or_url(self):
        cfg = {}
        mm.add_repo(cfg, OTHER)
        mm.remove_repo(cfg, "someone/theirmods")
        self.assertNotIn(OTHER, mm.list_repos(cfg))

        mm.add_repo(cfg, OTHER)
        mm.remove_repo(cfg, OTHER)
        self.assertNotIn(OTHER, mm.list_repos(cfg))

    def test_remove_repo_refuses_default_by_either_form(self):
        cfg = {}
        with self.assertRaises(mm.ModManagerError):
            mm.remove_repo(cfg, mm.DEFAULT_REPO)
        with self.assertRaises(mm.ModManagerError):
            mm.remove_repo(cfg, "ModdingTavern/TavernMods")

class PinnedModsConfig(unittest.TestCase):
    """list_pinned/add_pin/remove_pin - the client's always-on pin list."""

    def test_add_pin_stores_bare_and_exact_pins(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver")
        mm.add_pin(cfg, "Acme.AdminTools@1.4.0")
        self.assertEqual(mm.list_pinned(cfg), ["Acme.QuestGiver", "Acme.AdminTools@1.4.0"])

    def test_add_pin_replaces_existing_pin_for_same_id(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver@1.0.0")
        mm.add_pin(cfg, "Acme.QuestGiver@2.0.0")
        self.assertEqual(mm.list_pinned(cfg), ["Acme.QuestGiver@2.0.0"])

    def test_add_pin_rejects_what_could_never_resolve(self):
        """Caught as it is typed: a pin joins every server's plan, so a bad one
        would fail far from anything that explains it."""
        for bad in ("", "   ", "QuestGiver", "@1.0.0", "  @1.0.0", "Acme.QuestGiver@",
                    "Acme.QuestGiver@1.0", "Acme.QuestGiver@latest", "Acme.QuestGiver@v1.0.0"):
            with self.assertRaises(mm.ModManagerError, msg=bad):
                mm.add_pin({}, bad)

    def test_add_pin_leaves_the_list_untouched_when_it_rejects(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver")
        with self.assertRaises(mm.ModManagerError):
            mm.add_pin(cfg, "nope")
        self.assertEqual(mm.list_pinned(cfg), ["Acme.QuestGiver"])

    def test_remove_pin_by_bare_id_or_pinned_form(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver@1.0.0")
        mm.remove_pin(cfg, "Acme.QuestGiver")
        self.assertEqual(mm.list_pinned(cfg), [])

        mm.add_pin(cfg, "Acme.QuestGiver")
        mm.remove_pin(cfg, "Acme.QuestGiver@1.0.0")   # version suffix ignored on remove
        self.assertEqual(mm.list_pinned(cfg), [])

class Resolution(unittest.TestCase):
    

    def test_major_lock_picks_highest_in_major(self):
        index = [summary("dep.k", ["1.1.0", "1.4.0"]), summary("dep.k", ["2.1.0"])]
        fetch = store(make_manifest("dep.k", "1.4.0"))
        root = make_manifest("root.m", "1.0.0", dependencies={"dep.k": "1.2.0"})
        got = resolve_deps(self, [root], index, fetch, target="client_windows")
        self.assertEqual([m.version for m in got], ["1.4.0"])   # not 2.1.0, not 1.1.0

    def test_diamond_conflict_raises(self):
        index = [summary("dep.k", ["1.0.0"]), summary("dep.k", ["2.0.0"])]
        # a resolves dep.k@1, then b's major-2 requirement conflicts with it
        fetch = store(make_manifest("dep.k", "1.0.0"))
        a = make_manifest("a.a", "1.0.0", dependencies={"dep.k": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"dep.k": "2.0.0"})
        with self.assertRaises(mm.ModManagerError):
            resolve_deps(self, [a, b], index, fetch, target="client_windows")

    def test_cycle_raises(self):
        index = [summary("a.a", ["1.0.0"]), summary("b.b", ["1.0.0"])]
        a = make_manifest("a.a", "1.0.0", dependencies={"b.b": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"a.a": "1.0.0"})
        fetch = store(a, b)
        with self.assertRaises(mm.ModManagerError):
            resolve_deps(self, [a], index, fetch, target="client_windows")

    def test_a_root_that_is_also_a_dependency_is_not_refetched(self):
        a = make_manifest("a.a", "1.0.0")
        b = make_manifest("b.b", "1.0.0", dependencies={"a.a": "1.0.0"})
        fetched = []

        def fetch(mid, ver, src):
            fetched.append(mid)
            raise AssertionError("roots are already in hand")
        self.assertEqual(resolve_deps(self,
            [a, b], [summary("a.a", ["1.0.0"])], fetch, target="client_windows"), [])
        self.assertEqual(fetched, [])

    def test_a_chain_deeper_than_the_cap_raises(self):
        """MAX_DEPENDENCY_DEPTH links resolve; one more names the whole chain."""
        def chain(links):
            ids = [f"c{i}.m" for i in range(links + 1)]
            mods = [make_manifest(i, "1.0.0", dependencies={n: "1.0.0"})
                    for i, n in zip(ids, ids[1:])] + [make_manifest(ids[-1], "1.0.0")]
            return mods, [summary(i, ["1.0.0"]) for i in ids]

        mods, index = chain(mm.MAX_DEPENDENCY_DEPTH - 1)
        self.assertEqual(len(resolve_deps(self,
            mods[:1], index, store(*mods), target="client_windows")), len(mods) - 1)
        mods, index = chain(mm.MAX_DEPENDENCY_DEPTH)
        with self.assertRaises(mm.ModManagerError) as caught:
            resolve_deps(self, mods[:1], index, store(*mods), target="client_windows")
        self.assertIn("too deep", str(caught.exception))
        self.assertIn(" -> ".join(m.id for m in mods), str(caught.exception))

    def test_missing_dependency_raises(self):
        root = make_manifest("root.m", "1.0.0", dependencies={"nope.x": "1.0.0"})
        with self.assertRaises(mm.ModManagerError):
            resolve_deps(self, [root], [], store(), target="client_windows")


class TargetFilteredResolution(unittest.TestCase):
    """A dependency with no build for the target being installed is dropped,
    along with anything only it needed."""

    # A pc+server root whose dependency has only a pc build.
    def _dual_root_with_client_only_dep(self):
        index = [summary("ui.kit", ["1.0.0"], targets=("client_windows",))]
        dep = make_manifest("ui.kit", "1.0.0", targets=("client_windows",))
        root = make_manifest("dual.m", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        return index, dep, root

    def test_client_only_dependency_pruned_on_server(self):
        index, dep, root = self._dual_root_with_client_only_dep()
        got = resolve_deps(self, [root], index, store(dep), target="server")
        self.assertEqual(got, [])

    def test_same_dependency_kept_on_client(self):
        # Identical inputs to the test above; only the side differs. The pair is
        # the point: one input, two correct answers.
        index, dep, root = self._dual_root_with_client_only_dep()
        got = resolve_deps(self, [root], index, store(dep), target="client_windows")
        self.assertEqual([m.id for m in got], ["ui.kit"])

    def test_server_only_dependency_pruned_on_client(self):
        index = [summary("db.tools", ["1.0.0"], targets=("server",))]
        dep = make_manifest("db.tools", "1.0.0", targets=("server",))
        root = make_manifest("dual.m", "1.0.0", dependencies={"db.tools": "1.0.0"})
        self.assertEqual(resolve_deps(self, [root], index, store(dep),
                                                  target="client_windows"), [])
        self.assertEqual([m.id for m in resolve_deps(self,
            [root], index, store(dep), target="server")], ["db.tools"])

    def test_dual_side_dependency_kept_on_both(self):
        index = [summary("shared.d", ["1.0.0"])]
        dep = make_manifest("shared.d", "1.0.0")
        root = make_manifest("root.m", "1.0.0", dependencies={"shared.d": "1.0.0"})
        for target in ("client_windows", "server"):
            got = resolve_deps(self, [root], index, store(dep), target=target)
            self.assertEqual([m.id for m in got], ["shared.d"], target)

    def test_subtree_under_pruned_dependency_is_never_walked(self):
        index = [summary("ui.kit", ["1.0.0"], targets=("client_windows",)),
                 summary("deep.d", ["1.0.0"])]
        dep = make_manifest("ui.kit", "1.0.0", targets=("client_windows",),
                            dependencies={"deep.d": "1.0.0"})
        deep = make_manifest("deep.d", "1.0.0")
        root = make_manifest("dual.m", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        fetched = []
        fetch = store(dep, deep, fetched=fetched)
        self.assertEqual(resolve_deps(self, [root], index, fetch, target="server"), [])
        # ui.kit is fetched (its manifest is what decides the side), deep.d never is.
        self.assertEqual(fetched, ["ui.kit"])

    def test_dependency_also_reachable_by_a_valid_path_still_arrives(self):
        """Pruning ui.kit must not take shared.d with it when something else
        needs shared.d in its own right. Pruning is 'drop what only IT needed'."""
        index = [summary("ui.kit", ["1.0.0"], targets=("client_windows",)),
                 summary("shared.d", ["1.0.0"])]
        ui = make_manifest("ui.kit", "1.0.0", targets=("client_windows",),
                           dependencies={"shared.d": "1.0.0"})
        shared = make_manifest("shared.d", "1.0.0")
        # a is visited first, so shared.d is first reached THROUGH the pruned dep.
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"shared.d": "1.0.0"})
        got = resolve_deps(self, [a, b], index, store(ui, shared), target="server")
        self.assertEqual([m.id for m in got], ["shared.d"])

    def test_pruned_dependency_raises_no_diamond_conflict(self):
        """A mod we aren't installing must not be able to fail the whole resolve
        over a version disagreement about itself - the reason its constraints are
        recorded only after the side check passes."""
        index = [summary("ui.kit", ["1.0.0"], targets=("client_windows",)),
                 summary("ui.kit", ["2.0.0"], targets=("client_windows",))]
        ui1 = make_manifest("ui.kit", "1.0.0", targets=("client_windows",))
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"ui.kit": "2.0.0"})
        self.assertEqual(resolve_deps(self, [a, b], index, store(ui1),
                                                  target="server"), [])
        # The same disagreement is still a hard error on the side that installs it.
        with self.assertRaises(mm.ModManagerError):
            resolve_deps(self, [a, b], index, store(ui1), target="client_windows")

    def test_wrong_target_dependency_fetched_only_once(self):
        index = [summary("ui.kit", ["1.0.0"], targets=("client_windows",))]
        ui = make_manifest("ui.kit", "1.0.0", targets=("client_windows",))
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        fetched = []
        fetch = store(ui, fetched=fetched)
        self.assertEqual(resolve_deps(self, [a, b], index, fetch, target="server"), [])
        self.assertEqual(fetched, ["ui.kit"])

    def test_pruning_is_reported_not_silent(self):
        index, dep, root = self._dual_root_with_client_only_dep()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resolve_deps(self, [root], index, store(dep), target="server")
        msg = err.getvalue()
        self.assertIn("ui.kit", msg)
        self.assertIn("server", msg)

    def test_second_requirer_of_a_pruned_dependency_also_reported(self):
        index = [summary("ui.kit", ["1.0.0"], targets=("client_windows",))]
        ui = make_manifest("ui.kit", "1.0.0", targets=("client_windows",))
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resolve_deps(self, [a, b], index, store(ui), target="server")
        # Fetched once (test above), but both requirers still say so.
        self.assertEqual(err.getvalue().count("ui.kit"), 2)

    def test_roots_are_not_target_filtered(self):
        """Only dependencies are filtered: a root is what the caller asked for."""
        index = [summary("shared.d", ["1.0.0"])]
        dep = make_manifest("shared.d", "1.0.0")
        root = make_manifest("client.only", "1.0.0", targets=("client_windows",),
                             dependencies={"shared.d": "1.0.0"})
        got = resolve_deps(self, [root], index, store(dep), target="server")
        self.assertEqual([m.id for m in got], ["shared.d"])


class Libraries(unittest.TestCase):
    def _lib(self, sha):
        return {"name": "ExampleLib", "download_url": "https://x/ExampleLib.dll",
                "sha256": sha, "filename": "ExampleLib.dll"}

    def test_same_library_dedupes(self):
        a = make_manifest("a.a", "1.0.0", library_dependencies=[self._lib("a" * 64)])
        b = make_manifest("b.b", "1.0.0", library_dependencies=[self._lib("a" * 64)])
        self.assertEqual(len(mm.collect_library_dependencies([a, b], "client_windows")), 1)

    def test_conflicting_library_raises(self):
        a = make_manifest("a.a", "1.0.0", library_dependencies=[self._lib("a" * 64)])
        b = make_manifest("b.b", "1.0.0", library_dependencies=[self._lib("b" * 64)])
        with self.assertRaises(mm.ModManagerError):
            mm.collect_library_dependencies([a, b], "client_windows")


class Listing(unittest.TestCase):
    def test_target_filter_and_collapse(self):
        index = [
            summary("a.a", ["1.0.0"], targets=("client_windows",)),
            summary("a.a", ["1.2.0"], targets=("client_windows",)),   # separate major-1 entry, higher
            summary("b.b", ["1.0.0"], targets=("server",)),
        ]
        client = mm.list_mods(index, "client_windows")
        self.assertEqual([(s.id, s.highest()) for s in client], [("a.a", "1.2.0")])
        server = mm.list_mods(index, "server")
        self.assertEqual([s.id for s in server], ["b.b"])


class _FakeInstallFixture:
    """Real installs against fake download/hash helpers, mixed in beside
    unittest.TestCase. Installs are for TARGET unless a test says otherwise."""
    BASE = DEFAULT
    TARGET = "client_windows"

    def setUp(self):
        self.game = temp_dir(self, "tavern_game_")
        self.progress = []
        self.manifests = {}          # url -> manifest dict served by _get_json

        def fake_get_json(url, timeout=15):
            if url in self.manifests:
                return self.manifests[url]
            raise mm.ModManagerError(f"404 {url}")
        stub(self, repos, "_get_json", fake_get_json)

        # download content is derived from the URL; hash it for real so the
        # manifest sha256 has to match, same as production.
        self.dl_dirs = []            # directory each download landed its temp in

        def fake_download(url, dest, on_progress=None):
            self.dl_dirs.append(os.path.dirname(os.path.abspath(dest)))
            with open(dest, "wb") as f:
                f.write(("BYTES:" + url).encode())
            if on_progress:
                on_progress("downloaded")

        def fake_hash(path):
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        stub(self, install, "_download_with_progress", fake_download)
        stub(self, install, "_sha256_file", fake_hash)

    def _sha_for(self, url):
        return hashlib.sha256(("BYTES:" + url).encode()).hexdigest()

    def _publish(self, mod_id, version, **over):
        """Register a per-version manifest at its fetch URL, with a sha256
        matching what fake_download will produce."""
        d = manifest_dict(mod_id, version, **over)
        for art in d["artifacts"]:
            art["sha256"] = self._sha_for(art["download_url"])
            for lib in art["library_dependencies"]:
                lib["sha256"] = self._sha_for(lib["download_url"])
        author, repo = mod_id.split(".", 1)
        url = f"{self.BASE}/manifests/{author}/{repo}/{version}.json"
        self.manifests[url] = d
        return d

    def _lib_sha(self, name):
        return self._sha_for(lib_dep(name)["download_url"])

    def _closure(self, mod_id, version="1.0.0", target=None):
        """Closure-install one already-published mod, as Get Mods does."""
        install_mod_closure(self.game, summary(mod_id, [version]),
                               [summary(mod_id, [version])], [self.BASE],
                               self.progress.append, target or self.TARGET)

    def _install(self, mod_id, version="1.0.0", **over):
        """Publish + closure-install one mod: the installed state most cases
        start from."""
        self._publish(mod_id, version, **over)
        self._closure(mod_id, version)

    def _plan(self, server_mods, index, **kw):
        return mm.plan_join(self.game, server_mods, index, [self.BASE], **kw)

    def _mod_file(self, mod_id, *parts, target=None):
        """A path inside Mods/<id>/.<target>/."""
        return os.path.join(self.game, "Mods", mod_id, "." + (target or self.TARGET), *parts)

    def _record(self, mod_id):
        return read_json(os.path.join(self.game, "Mods", mod_id, "manifest.json"))

    def _cache_entry(self, mod_id, version):
        """The cache entry for a mod published with its default download_url."""
        return mm._cache_mod_dir(mod_id, version, self._sha_for(default_url(mod_id, version)))

    def _restore(self, mod_id, version):
        mm.cache_restore_mod(self.game, mod_id, version,
                             self._sha_for(default_url(mod_id, version)), "client_windows")


class _TempDataDirFixture(_FakeInstallFixture):
    """_FakeInstallFixture with the mod cache in a temp dir: tests never touch
    the real %AppData%."""

    def setUp(self):
        super().setUp()
        self.data_dir = temp_dir(self, "tavern_appdata_")
        stub(self, paths, "_tavern_data_dir", lambda: self.data_dir)


class InstallEndToEnd(_FakeInstallFixture, unittest.TestCase):
    def test_full_closure_install_lands_files_and_sidecars(self):
        lib = lib_dep("ExampleLib")
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0",
                      dependencies={"dep.k": "1.0.0"},
                      library_dependencies=[dict(lib)])

        index = [summary("dep.k", ["1.0.0"]), summary("root.m", ["1.0.0"])]
        root_summary = summary("root.m", ["1.0.0"])
        install_mod_closure(self.game, root_summary, index, [self.BASE], self.progress.append, target="client_windows")

        # Each mod lands in its own folder, Mods/<id>/, with the dll inside.
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "root.m", ".client_windows", "root.m.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dep.k", ".client_windows", "dep.k.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "ExampleLib.dll")))

        # Mods/<id>/manifest.json is also MelonLoader's folder marker.
        mod_meta = read_json(os.path.join(self.game, "Mods", "root.m", "manifest.json"))
        self.assertEqual(mod_meta["version"], "1.0.0")
        self.assertEqual(mod_meta["artifacts"][0]["package"], "dll")
        self.assertNotIn("filename", mod_meta)
        self.assertIn("source_repo", mod_meta)
        self.assertEqual(list(mod_meta["installed"]), ["client_windows"])
        self.assertEqual(mod_meta["installed"]["client_windows"]["libraries"], ["ExampleLib.dll"])
        # Nothing but the record and the target folder in Mods/<id>/.
        self.assertEqual(sorted(os.listdir(os.path.join(self.game, "Mods", "root.m"))),
                         [".client_windows", "manifest.json"])
        # No flat sidecar beside a bare dll.
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "root.m.meta.json")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "root.m.dll")))
        # Library sidecar lives in UserLibs/, not Mods/.
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "ExampleLib.dll.meta.json")))
        lib_meta = read_json(os.path.join(self.game, "UserLibs", "ExampleLib.dll.meta.json"))
        self.assertNotIn("version", lib_meta)
        self.assertNotIn("source_repo", lib_meta)
        self.assertTrue(any(m.startswith("[3/3]") for m in self.progress))

    def test_install_closure_explicit_version_overrides_highest(self):
        self._publish("v.v", "1.0.0")
        self._publish("v.v", "2.0.0")
        index = [summary("v.v", ["1.0.0", "2.0.0"])]
        install_mod_closure(self.game, summary("v.v", ["1.0.0", "2.0.0"]), index,
                               [self.BASE], self.progress.append, version="1.0.0", target="client_windows")
        mod_meta = read_json(os.path.join(self.game, "Mods", "v.v", "manifest.json"))
        self.assertEqual(mod_meta["version"], "1.0.0")

    def test_checksum_mismatch_aborts_leaving_nothing(self):
        d = self._publish("bad.m", "1.0.0")
        d["artifacts"][0]["sha256"] = "f" * 64        # break it after publishing
        with self.assertRaises(mm.ModManagerError):
            self._closure("bad.m", "1.0.0")
        # No mod folder and no staging left behind.
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "bad.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".bad.m.installing")))

    def test_status_current_then_outdated(self):
        self._install("s.s", "1.0.0")
        self.assertEqual(mm.mod_status(self.game, "s.s", [summary("s.s", ["1.0.0"])], "client_windows"), "current")
        self.assertEqual(mm.mod_status(self.game, "s.s", [summary("s.s", ["1.0.0", "1.1.0"])], "client_windows"), "outdated")

    def test_status_agrees_with_the_listings_about_a_legacy_record(self):
        """A record without parity_required isn't ours to the listings, so
        mod_status mustn't call it installed either."""
        self._install("legacy.m", "1.0.0")

        record = os.path.join(self.game, "Mods", "legacy.m", "manifest.json")
        rec = read_json(record)
        del rec["parity_required"]
        with open(record, "w", encoding="utf-8") as f:
            json.dump(rec, f)

        self.assertEqual(mm.mod_status(self.game, "legacy.m", [summary("legacy.m", ["1.0.0"])], "client_windows"),
                         "missing")
        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], [])
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)], ["legacy.m"])

    def test_uninstall_removes_mod_and_its_orphaned_library(self):
        lib = lib_dep("OrphanLib")
        self._install("u.m", "1.0.0", library_dependencies=[dict(lib)])

        self.assertTrue(mm.uninstall_mod(self.game, "u.m"))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "u.m")))
        self.assertEqual(mm.mod_status(self.game, "u.m", [], "client_windows"), "missing")
        # No mod needs the library anymore, so it and its sidecar are gone too.
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "OrphanLib.dll")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "OrphanLib.dll.meta.json")))

    def test_uninstall_keeps_library_another_mod_still_needs(self):
        lib = lib_dep("SharedLib")
        self._publish("a.m", "1.0.0", library_dependencies=[dict(lib)])
        self._publish("b.m", "1.0.0", library_dependencies=[dict(lib)])
        self._closure("a.m", "1.0.0")
        self._closure("b.m", "1.0.0")

        # Removing a leaves the library, since b still needs it.
        mm.uninstall_mod(self.game, "a.m")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "SharedLib.dll")))
        # Removing the last owner drops it.
        mm.uninstall_mod(self.game, "b.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "SharedLib.dll")))

    def test_uninstall_missing_mod_is_noop(self):
        self.assertFalse(mm.uninstall_mod(self.game, "never.installed"))

    def test_disable_enable_round_trips(self):
        self._install("d.m", "1.0.0")
        folder = os.path.join(self.game, "Mods", "d.m")
        record = os.path.join(folder, "manifest.json")
        disabled = os.path.join(folder, "manifest.disabled.json")

        # Disable removes the MelonLoader marker (manifest.json); folder stays.
        self.assertTrue(mm.disable_mod(self.game, "d.m"))
        self.assertFalse(os.path.exists(record))
        self.assertTrue(os.path.isfile(disabled))
        self.assertTrue(os.path.isdir(folder))
        # Still installed and version-comparable while disabled.
        self.assertEqual(mm.mod_status(self.game, "d.m", [summary("d.m", ["1.0.0"])], "client_windows"), "current")
        rec = next(r for r in mm.list_installed_mods(self.game) if r["id"] == "d.m")
        self.assertIs(rec["enabled"], False)

        # Enable restores the marker.
        self.assertTrue(mm.enable_mod(self.game, "d.m"))
        self.assertTrue(os.path.isfile(record))
        self.assertFalse(os.path.exists(disabled))
        rec = next(r for r in mm.list_installed_mods(self.game) if r["id"] == "d.m")
        self.assertIs(rec["enabled"], True)

    def test_disable_enable_noops(self):
        # Nothing installed.
        self.assertFalse(mm.disable_mod(self.game, "nope.x"))
        self.assertFalse(mm.enable_mod(self.game, "nope.x"))
        self._install("n.m", "1.0.0")
        # Enabling an already-enabled mod, or disabling twice, is a no-op.
        self.assertFalse(mm.enable_mod(self.game, "n.m"))
        self.assertTrue(mm.disable_mod(self.game, "n.m"))
        self.assertFalse(mm.disable_mod(self.game, "n.m"))

    def test_uninstall_removes_disabled_mod_folder(self):
        self._install("z.m", "1.0.0")
        self.assertTrue(mm.disable_mod(self.game, "z.m"))
        self.assertTrue(mm.uninstall_mod(self.game, "z.m"))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "z.m")))

    def test_temp_download_lives_on_destination_volume(self):
        # Temp .part files must be created under the game directory, not the
        # system temp dir, or os.replace fails with WinError 17 when the game
        # is on a different drive than TEMP.
        lib = lib_dep("RegLib")
        self._install("t.m", "1.0.0", library_dependencies=[dict(lib)])
        game_root = os.path.normcase(os.path.abspath(self.game)) + os.sep
        self.assertTrue(self.dl_dirs)
        for d in self.dl_dirs:
            self.assertTrue(os.path.normcase(d).startswith(game_root))


class InstallModSet(_FakeInstallFixture, unittest.TestCase):
    """install_mod_set: the one install pass behind install_mod_closure and
    apply_import. What's already there, intact and enabled, isn't fetched
    again."""

    LIB = lib_dep("SetLib")

    def _set(self, *ids):
        return [mm._fetch_manifest([self.BASE], i, "1.0.0") for i in ids]

    def test_an_intact_enabled_mod_and_library_are_not_refetched(self):
        self._install("set.m", library_dependencies=[dict(self.LIB)])
        downloads = len(self.dl_dirs)
        self.assertEqual(mm.install_mod_set(self.game, self._set("set.m"), "client_windows", noop), [])
        self.assertEqual(len(self.dl_dirs), downloads)

    def test_a_damaged_mod_is_reinstalled(self):
        self._install("dmg.m")
        with open(os.path.join(self.game, "Mods", "dmg.m", ".client_windows", "dmg.m.dll"), "wb") as f:
            f.write(b"junk")
        mm._verify_memo.clear()
        self.assertEqual(mm.install_mod_set(self.game, self._set("dmg.m"), "client_windows", noop), ["dmg.m"])
        self.assertIs(mm.verify_mod_files(self.game, "dmg.m", "client_windows"), True)

    def test_a_disabled_mod_is_reinstalled_enabled(self):
        self._install("off.m")
        mm.disable_mod(self.game, "off.m")
        self.assertEqual(mm.install_mod_set(self.game, self._set("off.m"), "client_windows", noop), ["off.m"])
        self.assertTrue(os.path.isfile(mm._mod_record_path(self.game, "off.m")))

    def test_a_tampered_library_is_reinstalled(self):
        self._install("lib.m", library_dependencies=[dict(self.LIB)])
        lib_path = os.path.join(self.game, "UserLibs", "SetLib.dll")
        with open(lib_path, "wb") as f:
            f.write(b"junk")
        downloads = len(self.dl_dirs)
        mm.install_mod_set(self.game, self._set("lib.m"), "client_windows", noop)
        self.assertEqual(len(self.dl_dirs), downloads + 1)
        with open(lib_path, "rb") as f:
            self.assertEqual(f.read(), b"BYTES:https://x/SetLib.dll")

    def test_progress_counts_every_mod_and_library_once(self):
        self._publish("a.m", "1.0.0", library_dependencies=[dict(self.LIB)])
        self._publish("b.m", "1.0.0")
        lines = []
        mm.install_mod_set(self.game, self._set("a.m", "b.m"), "client_windows", lines.append)
        self.assertEqual(sorted({l.split("]")[0] + "]" for l in lines}),
                         ["[1/3]", "[2/3]", "[3/3]"])


class MissingDependencies(_FakeInstallFixture, unittest.TestCase):
    """Get Mods asks before adding dependencies; a mod installed without them
    is marked until Install deps adds them."""

    def _publish_pair(self, dep_versions=("1.0.0",), dep_targets=("client_windows", "server"),
                      dep_min="1.0.0"):
        for v in dep_versions:
            self._publish("dep.k", v, targets=dep_targets)
        self._publish("root.m", "1.0.0", dependencies={"dep.k": dep_min})
        return [summary("dep.k", list(dep_versions), targets=dep_targets),
                summary("root.m", ["1.0.0"])]

    def _root_alone(self):
        mm.install_mod_set(self.game, [mm._fetch_manifest([self.BASE], "root.m", "1.0.0")],
                           self.TARGET, noop)

    def _missing(self, index):
        installed = mm.list_installed_mods(self.game)
        rec = next(r for r in installed if r["id"] == "root.m")
        return mm.missing_dependencies(rec, installed, index, self.TARGET)

    def test_plan_lists_only_dependencies_not_yet_installed(self):
        index = self._publish_pair()
        root, deps, needed = mm.plan_mod_install(self.game, index[1], index, [self.BASE], self.TARGET)
        self.assertEqual((root.id, [d.id for d in deps], [d.id for d in needed]),
                         ("root.m", ["dep.k"], ["dep.k"]))
        self._closure("dep.k")
        self.assertEqual(mm.plan_mod_install(self.game, index[1], index, [self.BASE], self.TARGET)[2], [])

    def test_a_mod_installed_alone_is_marked_until_install_dependencies(self):
        index = self._publish_pair()
        self._root_alone()
        self.assertEqual(self._missing(index), [("dep.k", "1.0.0", "missing")])
        rec = next(r for r in mm.list_installed_mods(self.game) if r["id"] == "root.m")
        self.assertEqual(mm.install_dependencies(self.game, rec, index, [self.BASE], self.TARGET, noop),
                         ["dep.k"])
        self.assertEqual(self._missing(index), [])

    def test_a_disabled_dependency_counts_as_missing(self):
        index = self._publish_pair()
        install_mod_closure(self.game, index[1], index, [self.BASE], noop, self.TARGET)
        mm.disable_mod(self.game, "dep.k")
        self.assertEqual(self._missing(index), [("dep.k", "1.0.0", "disabled")])

    def test_an_old_dependency_counts_as_missing(self):
        index = self._publish_pair(dep_versions=("1.0.0", "1.1.0"), dep_min="1.1.0")
        self._root_alone()
        install_mod_closure(self.game, index[0], index, [self.BASE], noop, self.TARGET,
                            version="1.0.0")
        self.assertEqual(self._missing(index), [("dep.k", "1.1.0", "too old")])

    def test_a_dependency_on_another_major_counts_as_missing(self):
        index = self._publish_pair(dep_versions=("0.9.0", "1.0.0"))
        self._root_alone()
        install_mod_closure(self.game, index[0], index, [self.BASE], noop, self.TARGET,
                            version="0.9.0")
        self.assertEqual(self._missing(index), [("dep.k", "1.0.0", "wrong major")])

    def test_a_dependency_with_no_build_here_is_not_missing(self):
        index = self._publish_pair(dep_targets=("server",))
        install_mod_closure(self.game, index[1], index, [self.BASE], noop, self.TARGET)
        self.assertEqual(self._missing(index), [])


class Replacements(_TempDataDirFixture, unittest.TestCase):
    """A source's `replaced`: old.m is retired in favour of new.m. Only the
    source an installed mod came from can retire it."""

    def _index(self, source=DEFAULT, new_versions=("2.0.0",), extra=()):
        return mm.ModIndex([summary("new.m", list(new_versions)), *extra],
                           [mm.Replacement("old.m", "new.m", "Moved.", source)])

    def _ids(self):
        return sorted(r["id"] for r in mm.list_installed_mods(self.game))

    def test_fetch_indexes_carries_valid_replacements_only(self):
        mm._index_cache.clear()
        self.manifests[f"{self.BASE}/repository.json"] = dict(
            slim_index({"new.m": {"2": slim_entry(["2.0.0"])}}),
            replaced={"old.m": {"by": "new.m", "note": "Moved."}, "bad.m": "new.m",
                      "self.m": {"by": "self.m"}, "nonote.m": {"by": "new.m", "note": 3}})
        index = mm.fetch_indexes([self.BASE], force=True)
        mm._index_cache.clear()
        self.assertEqual([s.id for s in index], ["new.m"])
        self.assertEqual(index.replaced, [
            mm.Replacement("old.m", "new.m", "Moved.", self.BASE),
            mm.Replacement("nonote.m", "new.m", "", self.BASE)])

    def test_without_a_source_the_default_sources_replacement_wins(self):
        index = mm.ModIndex([summary("new.m", ["2.0.0"]), summary("theirs.m", ["1.0.0"])],
                            [mm.Replacement("old.m", "theirs.m", "", OTHER),
                             mm.Replacement("old.m", "new.m", "", DEFAULT)])
        self.assertEqual(mm.replacement_of_unlisted(index, "old.m").by, "new.m")
        self.assertEqual(mm.replacement_for(index, "old.m", OTHER).by, "theirs.m")

    def test_a_listed_id_or_an_unlisted_replacement_is_left_alone(self):
        self.assertIsNone(mm.replacement_of_unlisted(self._index(extra=[summary("old.m", ["1.0.0"])]),
                                                     "old.m"))
        self.assertIsNone(mm.replacement_of_unlisted(
            mm.ModIndex([], [mm.Replacement("old.m", "new.m", "", DEFAULT)]), "old.m"))

    def test_installing_the_replacement_retires_the_old_mod(self):
        self._install("old.m", "1.0.0")
        self._install("new.m", "2.0.0")
        self.assertEqual(mm.retire_replaced(self.game, self._index(), noop), [("old.m", "new.m")])
        self.assertEqual(self._ids(), ["new.m"])

    def test_another_source_cant_retire_a_mod(self):
        self._install("old.m", "1.0.0")
        self._install("new.m", "2.0.0")
        self.assertIsNone(mm.replaced_by(self._index(OTHER), mm.list_installed_mods(self.game)[0]))
        self.assertEqual(mm.retire_replaced(self.game, self._index(OTHER), noop), [])
        self.assertEqual(self._ids(), ["new.m", "old.m"])

    def test_the_old_mod_stays_until_the_replacement_covers_it(self):
        self._install("old.m", "1.0.0")
        self._closure("old.m", "1.0.0", target="server")
        self._install("new.m", "2.0.0")
        self.assertEqual(mm.retire_replaced(self.game, self._index(), noop), [])   # no server build yet
        self._closure("new.m", "2.0.0", target="server")
        mm.disable_mod(self.game, "new.m")
        self.assertEqual(mm.retire_replaced(self.game, self._index(), noop), [])   # turned off
        mm.enable_mod(self.game, "new.m")
        self.assertEqual(mm.retire_replaced(self.game, self._index(), noop), [("old.m", "new.m")])

    def test_a_dependency_on_the_old_mod_resolves_to_the_replacement(self):
        self._publish("new.m", "2.0.0")
        self._publish("root.m", "1.0.0", dependencies={"old.m": "1.0.0"})
        index = self._index(extra=[summary("root.m", ["1.0.0"])])
        _root, deps, _needed = mm.plan_mod_install(self.game, summary("root.m", ["1.0.0"]), index,
                                                   [self.BASE], "client_windows")
        self.assertEqual([(d.id, d.version) for d in deps], [("new.m", "2.0.0")])

    def test_a_pin_on_the_old_mod_pins_the_replacement(self):
        self._publish("new.m", "2.0.0")
        plan = self._plan([], self._index(), pinned=["old.m@1.0.0"])
        self.assertEqual([(e.mod_id, e.version, e.reason) for e in plan.entries],
                         [("new.m", "2.0.0", "pinned")])

    def test_a_server_on_the_old_mod_leaves_out_a_pinned_replacement(self):
        self._publish("old.m", "1.0.0")
        self._publish("new.m", "2.0.0")
        plan = self._plan([_srv("old.m", "1.0.0")], self._index(), pinned=["new.m"])
        self.assertEqual([e.mod_id for e in plan.entries], ["old.m"])

    def test_a_modlist_entry_for_the_old_mod_imports_the_replacement(self):
        d = self._publish("new.m", "2.0.0")
        self.manifests[f"{self.BASE}/manifests/new/m/latest.json"] = d
        plan = mm.import_modlist({"schema": 1, "repos": [], "mods": ["old.m@1.0.0"]},
                                 self._index(), [self.BASE], target="client_windows")
        self.assertEqual([(m.id, m.version) for m in plan.roots], [("new.m", "2.0.0")])
        self.assertEqual(plan.replaced, [("old.m", "new.m")])


class UntrackedMods(_FakeInstallFixture, unittest.TestCase):
    """Mods sitting in Mods/ that MelonLoader loads but this manager didn't
    install. TavernLib's ListUntrackedMods is the same listing for headless
    servers, so anything asserted here is a claim about both."""

    def _mods(self, *parts):
        path = os.path.join(self.game, "Mods", *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def _write(self, relative, content=""):
        path = self._mods(*relative.split("/"))
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _names(self):
        return {u["name"]: u for u in mm.list_untracked_mods(self.game)}

    # -- loose root dlls ------------------------------------------------------

    # (files written under Mods/, expected {name: (kind, enabled)})
    LISTING = [
        (["Manual.dll"], {"Manual.dll": ("file", True)}),
        # Reported under its enabled name, so one string toggles it both ways.
        (["Manual.dll.disabled"], {"Manual.dll": ("file", False)}),
        (["notes.txt", "config.json"], {}),
        (["LegacyHUD/manifest.json"], {"LegacyHUD": ("folder", True)}),
        # MelonLoader's marker is an existence check, so junk JSON still loads.
        (["BrokenHUD/manifest.json:not json {{{"], {"BrokenHUD": ("folder", True)}),
        (["LegacyHUD/manifest.disabled.json"], {"LegacyHUD": ("folder", False)}),
        # Not an object, or an id that isn't a non-empty string: not ours.
        (["ListRec/manifest.json:[1, 2]"], {"ListRec": ("folder", True)}),
        (['NumId/manifest.json:{"id": 5, "parity_required": true}'], {"NumId": ("folder", True)}),
        (["Empty/", "JustFiles/README.txt"], {}),
        ([".p.p.installing/manifest.json", "~backup/manifest.json"], {}),
    ]

    def test_listing(self):
        """TavernLib's ListUntrackedMods must report the same set."""
        for files, expected in self.LISTING:
            with self.subTest(files=files):
                mods = os.path.join(self.game, "Mods")
                shutil.rmtree(mods, ignore_errors=True)
                for spec in files:
                    rel, _, content = spec.partition(":")
                    if rel.endswith("/"):
                        os.makedirs(self._mods(rel.rstrip("/")), exist_ok=True)
                    else:
                        self._write(rel, content or json.dumps({"whatever": True}))
                self.assertEqual({n: (u["kind"], u["enabled"]) for n, u in self._names().items()},
                                 expected)

    def test_toggling_a_loose_dll_round_trips(self):
        self._write("Manual.dll")
        self.assertTrue(mm.disable_untracked_dll(self.game, "Manual.dll"))
        self.assertFalse(self._names()["Manual.dll"]["enabled"])
        self.assertFalse(os.path.isfile(self._mods("Manual.dll")))

        self.assertTrue(mm.enable_untracked_dll(self.game, "Manual.dll"))
        self.assertTrue(self._names()["Manual.dll"]["enabled"])
        self.assertTrue(os.path.isfile(self._mods("Manual.dll")))

    def test_toggling_reports_false_when_there_is_nothing_to_do(self):
        self.assertFalse(mm.disable_untracked_dll(self.game, "Absent.dll"))
        self.assertFalse(mm.enable_untracked_dll(self.game, "Absent.dll"))

    def test_toggling_refuses_to_clobber_the_file_on_the_other_side(self):
        """Both names occupied at once is someone else's doing, and the rename
        that resolves it would destroy a mod file irrecoverably."""
        self._write("Manual.dll", "enabled copy")
        self._write("Manual.dll.disabled", "disabled copy")

        with self.assertRaises(mm.ModManagerError):
            mm.disable_untracked_dll(self.game, "Manual.dll")
        with self.assertRaises(mm.ModManagerError):
            mm.enable_untracked_dll(self.game, "Manual.dll")

        with io.open(self._mods("Manual.dll"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "enabled copy")
        with io.open(self._mods("Manual.dll.disabled"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "disabled copy")

    # -- foreign folders ------------------------------------------------------

    # -- the boundary with managed mods ---------------------------------------

    def test_a_managed_mod_is_never_reported_as_untracked(self):
        self._install("p.p")
        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.p"])
        self.assertEqual(self._names(), {})

    def test_a_disabled_managed_mod_is_still_not_untracked(self):
        """Disabling renames the record to manifest.disabled.json, which is
        exactly the shape a foreign disabled folder has. The two are told apart
        by whether the record reads as ours, not by which file is present."""
        self._install("p.p")
        self.assertTrue(mm.disable_mod(self.game, "p.p"))
        self.assertEqual(self._names(), {})

    def test_a_managed_mod_and_a_manual_one_coexist(self):
        self._install("p.p")
        self._write("Manual.dll")
        self._write("LegacyHUD/manifest.json", json.dumps({"whatever": True}))
        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.p"])
        self.assertEqual(set(self._names()), {"Manual.dll", "LegacyHUD"})


class HandshakeSnapshot(_FakeInstallFixture, unittest.TestCase):
    """handshake_snapshot(game_dir) - the ping/pong mod-sync fingerprint of
    what this folder runs as a server. Reuses the fake download/hash wiring
    to actually install mods (server builds), then checks the snapshot."""
    TARGET = "server"

    def test_empty_mods_dir_gives_empty_snapshot(self):
        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 0)
        self.assertEqual(entries, [])
        self.assertEqual(mods_hash, hashlib.sha256(b"").hexdigest())

    def test_installed_mods_appear_sorted_by_id(self):
        self._publish("z.z", "1.0.0")
        self._publish("a.a", "2.1.0")
        self._closure("z.z", "1.0.0")
        self._closure("a.a", "2.1.0")

        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 2)
        self.assertEqual([e["id"] for e in entries], ["a.a", "z.z"])
        self.assertEqual(entries[0]["version"], "2.1.0")
        self.assertEqual(entries[0]["targets"], ["client_windows", "server"])
        self.assertEqual(set(entries[0]["target_sha256"]), {"client_windows", "server"})

        expected = hashlib.sha256(b"a.a@2.1.0@req\nz.z@1.0.0@req").hexdigest()
        self.assertEqual(mods_hash, expected)

    def test_hash_changes_when_only_parity_changes(self):
        """A server can flip required/recommended without a version bump, and
        clients cache the list against this hash."""
        self._install("p.p", "1.0.0")
        before, _, entries, _ = mm.handshake_snapshot(self.game)
        self.assertIs(entries[0]["parity_required"], True)

        # Same mod, same version, parity relaxed - exactly the case a version
        # comparison can't see.
        record_path = mm._mod_record_path(self.game, "p.p")
        with io.open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        record["parity_required"] = False
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

        after, _, entries, _ = mm.handshake_snapshot(self.game)
        self.assertIs(entries[0]["parity_required"], False)
        self.assertNotEqual(before, after)

    def test_disabled_mod_excluded_from_snapshot(self):
        self._install("d.d", "1.0.0")
        mm.disable_mod(self.game, "d.d")

        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 0)
        self.assertEqual(entries, [])
        self.assertEqual(mods_hash, hashlib.sha256(b"").hexdigest())

    def test_hash_changes_when_a_version_changes(self):
        self._install("v.v", "1.0.0")
        before, _, _, _ = mm.handshake_snapshot(self.game)

        self._install("v.v", "1.1.0")
        after, _, _, _ = mm.handshake_snapshot(self.game)

        self.assertNotEqual(before, after)

    # -- untracked mods in the handshake --------------------------------------

    def _untracked(self, name, content="x"):
        path = os.path.join(self.game, "Mods", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_untracked_mods_are_reported_separately_from_managed_ones(self):
        """Never merged into the mods list: plan_join resolves every entry it's
        handed, and one with no id or version has nothing to resolve."""
        self._install("m.m", "1.0.0")
        self._untracked("Manual.dll")

        _, count, entries, untracked = mm.handshake_snapshot(self.game)
        self.assertEqual([e["id"] for e in entries], ["m.m"])
        self.assertEqual(untracked, [{"name": "Manual.dll", "kind": "file"}])
        # Count tracks the managed list a client sanity-checks against, so an
        # untracked mod must not inflate it.
        self.assertEqual(count, 1)

    def test_a_disabled_untracked_mod_is_not_reported(self):
        """Same rule as a disabled managed mod: it isn't loaded, so there's
        nothing to tell a joining client about."""
        self._untracked("Manual.dll")
        mm.disable_untracked_dll(self.game, "Manual.dll")
        _, _, _, untracked = mm.handshake_snapshot(self.game)
        self.assertEqual(untracked, [])

    def test_untracked_mods_move_the_hash(self):
        """Adding, toggling or rewriting (mtime) an untracked mod each moves the
        hash, or clients keep a stale cached list; nothing changing keeps it."""
        path = os.path.join(self.game, "Mods", "Manual.dll")
        steps = [lambda: self._untracked("Manual.dll"),
                 lambda: os.utime(path, (1_700_000_000, 1_700_000_000)),
                 lambda: mm.disable_untracked_dll(self.game, "Manual.dll")]
        before = mm.handshake_snapshot(self.game)[0]
        for step in steps:
            step()
            after = mm.handshake_snapshot(self.game)[0]
            self.assertNotEqual(after, before)
            self.assertEqual(mm.handshake_snapshot(self.game)[0], after)
            before = after

    def test_untracked_fingerprint_lines_are_byte_exact(self):
        """Pins the "untracked:name@mtime" line: TavernLib's ModHandshake must
        produce the same bytes."""
        self._untracked("Manual.dll")
        path = os.path.join(self.game, "Mods", "Manual.dll")
        os.utime(path, (1_700_000_000, 1_700_000_000))

        mods_hash, _, _, _ = mm.handshake_snapshot(self.game)
        expected = hashlib.sha256(b"untracked:Manual.dll@1700000000").hexdigest()
        self.assertEqual(mods_hash, expected)


class DisplacedFolders(_TempDataDirFixture, unittest.TestCase):
    """Installing over a Mods/<id>/ folder we didn't create moves it aside
    instead of deleting it. TavernLib's ClearInstallPath does the same thing for
    headless servers, where there's nobody to prompt."""

    def _foreign(self, name, marker="theirs"):
        """A folder at Mods/<name>/ that isn't ours: a manifest with no id, so it
        reads as untracked exactly like a hand-installed mod would."""
        folder = os.path.join(self.game, "Mods", name)
        os.makedirs(folder, exist_ok=True)
        with io.open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"theirs": True}))
        with io.open(os.path.join(folder, "payload.txt"), "w", encoding="utf-8") as f:
            f.write(marker)
        return folder

    def _displaced(self, *parts):
        return os.path.join(self.game, "Mods", ".displaced", *parts)

    def test_installing_over_a_foreign_folder_moves_it_aside(self):
        self._foreign("p.p", marker="irreplaceable")
        self._install("p.p")

        # The install landed...
        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.p"])
        # ...and their files survived intact rather than being deleted.
        with io.open(self._displaced("p.p", "payload.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "irreplaceable")

    def test_reinstalling_over_our_own_folder_just_replaces_it(self):
        """Nothing is displaced when the folder is ours - it's being replaced,
        and it's cached already if anything wanted it kept. Otherwise every
        routine update would litter .displaced/."""
        self._install("p.p", "1.0.0")
        self._install("p.p", "1.1.0")
        self.assertFalse(os.path.exists(self._displaced()))

    def test_displacing_the_same_name_twice_keeps_both(self):
        self._foreign("p.p", marker="first")
        self._install("p.p")
        self._foreign("p.p", marker="second")
        self._install("p.p")

        with io.open(self._displaced("p.p", "payload.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "first")
        with io.open(self._displaced("p.p.2", "payload.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "second")

    def test_a_displaced_folder_is_invisible_to_both_listers(self):
        """Dot-prefixed, so it's skipped as an install staging dir would be. If
        it showed up as untracked, the operator would be told a mod is loading
        when nothing loads from .displaced/ at all."""
        self._foreign("p.p")
        self._install("p.p")

        self.assertEqual(mm.list_untracked_mods(self.game), [])
        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.p"])

    def test_restoring_from_cache_displaces_too(self):
        """The likelier of the two paths to meet a foreign folder: it runs on
        every join that switches a mod's version, not just on a fresh install."""
        self._install("p.p")
        mm.cache_store_mod(self.game, "p.p", "client_windows")
        shutil.rmtree(os.path.join(self.game, "Mods", "p.p"))
        self._foreign("p.p", marker="theirs, mid-join")

        self._restore("p.p", "1.0.0")

        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.p"])
        with io.open(self._displaced("p.p", "payload.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "theirs, mid-join")


class ClientModCache(_TempDataDirFixture, unittest.TestCase):
    """The client mod cache: mod_cache/<id>/<version>/<sha256>/ and the
    library equivalent, keyed by filename+sha256."""

    def test_store_then_restore_round_trip(self):
        self._install("c.m", "1.0.0")
        self.assertTrue(mm.cache_store_mod(self.game, "c.m", "client_windows"))
        self.assertTrue(os.path.isdir(self._cache_entry("c.m", "1.0.0")))

        mm.uninstall_mod(self.game, "c.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "c.m")))

        self._restore("c.m", "1.0.0")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "c.m", ".client_windows", "c.m.dll")))
        restored = read_json(os.path.join(self.game, "Mods", "c.m", "manifest.json"))
        self.assertEqual(restored["version"], "1.0.0")

    def test_restoring_uncached_version_raises(self):
        with self.assertRaises(mm.ModManagerError):
            self._restore("never.installed", "9.9.9")

    def test_store_is_idempotent(self):
        self._install("i.m", "1.0.0")
        self.assertTrue(mm.cache_store_mod(self.game, "i.m", "client_windows"))
        # A second call with the cached copy already present is a no-op, not
        # an error (shutil.copytree onto an existing dir would raise).
        self.assertFalse(mm.cache_store_mod(self.game, "i.m", "client_windows"))

    def test_disabled_mod_still_caches_its_build(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        self.assertTrue(mm.cache_store_mod(self.game, "dis.m", "client_windows"))
        cached = self._cache_entry("dis.m", "1.0.0")
        self.assertEqual(sorted(os.listdir(cached)), ["record.json", "tree"])
        self.assertEqual(os.listdir(os.path.join(cached, "tree")), ["dis.m.dll"])

    def test_multiple_versions_coexist_in_cache(self):
        self._install("mv.m", "1.0.0")
        mm.cache_store_mod(self.game, "mv.m", "client_windows")
        self._install("mv.m", "2.0.0")
        mm.cache_store_mod(self.game, "mv.m", "client_windows")
        self.assertTrue(os.path.isdir(self._cache_entry("mv.m", "1.0.0")))
        self.assertTrue(os.path.isdir(self._cache_entry("mv.m", "2.0.0")))

    def test_library_store_and_restore_round_trip(self):
        lib = lib_dep("Shared")
        self._install("lib.m", "1.0.0", library_dependencies=[dict(lib)])
        sha = self._lib_sha("Shared")

        mm.cache_store_library(self.game, "Shared.dll")
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Shared.dll", sha)))

        os.remove(os.path.join(self.game, "UserLibs", "Shared.dll"))
        os.remove(os.path.join(self.game, "UserLibs", "Shared.dll.meta.json"))

        mm.cache_restore_library(self.game, "Shared.dll", sha)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "Shared.dll")))
        meta = read_json(os.path.join(self.game, "UserLibs", "Shared.dll.meta.json"))
        self.assertEqual(meta["sha256"], sha)

    def test_restoring_library_already_present_with_matching_hash_is_noop(self):
        lib = lib_dep("AlreadyThere")
        self._install("lib2.m", "1.0.0", library_dependencies=[dict(lib)])
        sha = self._lib_sha("AlreadyThere")
        mm.cache_store_library(self.game, "AlreadyThere.dll")
        # Already present with the right hash - must not raise even though
        # nothing was ever removed from UserLibs/.
        mm.cache_restore_library(self.game, "AlreadyThere.dll", sha)

    def test_adopt_caches_mod_and_its_library_returns_newly_adopted_only(self):
        lib = lib_dep("Adopted")
        self._install("ad.m", "1.0.0", library_dependencies=[dict(lib)])
        self._install("ad2.m", "1.0.0")

        adopted = mm.adopt_installed_mods(self.game, "client_windows")
        self.assertEqual(sorted(adopted), ["ad.m", "ad2.m"])
        self.assertTrue(os.path.isdir(self._cache_entry("ad.m", "1.0.0")))
        self.assertTrue(os.path.isdir(self._cache_entry("ad2.m", "1.0.0")))
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Adopted.dll", self._lib_sha("Adopted"))))

        # Re-running adopts nothing new - both mods are already cached.
        self.assertEqual(mm.adopt_installed_mods(self.game, "client_windows"), [])


class PlanJoin(_TempDataDirFixture, unittest.TestCase):
    """plan_join: the pure resolution core. The temp _tavern_data_dir is
    needed for the cached/active-entry checks."""

    OTHER = OTHER

    def _entry(self, plan, mod_id):
        return next(e for e in plan.entries if e.mod_id == mod_id)

    def test_required_mod_becomes_active_entry(self):
        self._publish("req.m", "1.0.0")
        server_mods = [_srv("req.m", "1.0.0")]
        index = [summary("req.m", ["1.0.0"])]

        plan = self._plan(server_mods, index)
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.needs_repo, [])
        entry = self._entry(plan, "req.m")
        self.assertEqual(entry.version, "1.0.0")
        self.assertEqual(entry.reason, "required")
        self.assertFalse(entry.cached)
        self.assertFalse(entry.active)

    def test_a_server_only_mod_is_not_required(self):
        server_mods = [_srv("srv.m", "1.0.0", targets=("server",))]
        plan = self._plan(server_mods, [])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.missing, [])

    def test_dependency_pulled_in_with_reason_dependency(self):
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        server_mods = [_srv("root.m", "1.0.0")]
        index = [summary("dep.k", ["1.0.0"]), summary("root.m", ["1.0.0"])]

        plan = self._plan(server_mods, index)
        self.assertEqual({e.mod_id: e.reason for e in plan.entries},
                         {"root.m": "required", "dep.k": "dependency"})

    def test_missing_required_mod_blocks_with_no_hint(self):
        server_mods = [_srv("ghost.m", "1.0.0")]
        plan = self._plan(server_mods, [])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.needs_repo, [])

    def test_needs_repo_when_hint_names_unadded_repo_blocks_with_the_hint(self):
        """Blocks like a missing mod (an apply could only fail the parity check
        afterwards), but names the source to add."""
        server_mods = [_srv("elsewhere.m", "1.0.0", source_repo=self.OTHER)]
        plan = self._plan(server_mods, [])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.needs_repo, [("elsewhere.m", "1.0.0", self.OTHER)])

    def test_pinned_mod_with_no_version_resolves_to_index_highest(self):
        self._publish("pin.m", "2.5.0")
        index = [summary("pin.m", ["1.0.0", "2.5.0"])]
        plan = self._plan([], index, pinned=["pin.m"])
        entry = self._entry(plan, "pin.m")
        self.assertEqual(entry.version, "2.5.0")
        self.assertEqual(entry.reason, "pinned")

    def test_pinned_mod_with_exact_version(self):
        self._publish("pin.m", "1.0.0")
        self._publish("pin.m", "2.5.0")
        index = [summary("pin.m", ["1.0.0", "2.5.0"])]
        plan = self._plan([], index, pinned=["pin.m@1.0.0"])
        entry = self._entry(plan, "pin.m")
        self.assertEqual(entry.version, "1.0.0")

    def test_an_unresolvable_pin_does_not_block_the_join(self):
        """A broken pin is the user's own setting, not a demand from this server."""
        plan = self._plan([], [], pinned=["ghost.pin"])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.unresolved_pins, [("ghost.pin", "latest")])

    def test_an_unresolvable_exact_pin_does_not_block_either(self):
        """The version-pinned path resolves through a different branch than the
        track-latest one above, so it gets its own case."""
        plan = self._plan([], [summary("gone.pin", ["9.9.9"])], pinned=["gone.pin@9.9.9"])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.unresolved_pins, [("gone.pin", "9.9.9")])

    def test_a_broken_pin_still_lets_a_real_server_mod_resolve(self):
        """The whole point: the join goes ahead, carrying everything the server
        actually asked for."""
        self._publish("req.m", "1.0.0")
        server_mods = [_srv("req.m", "1.0.0")]
        plan = self._plan(server_mods, [summary("req.m", ["1.0.0"])], pinned=["ghost.pin"])
        self.assertFalse(plan.blocking)
        self.assertEqual([e.mod_id for e in plan.entries], ["req.m"])
        self.assertEqual(plan.unresolved_pins, [("ghost.pin", "latest")])

    def test_a_recommended_mod_from_an_unadded_repo_is_not_needs_repo(self):
        """needs_repo means "required"; a recommendation never is."""
        server_mods = [_srv("opt.elsewhere", "1.0.0", required=False,
                            source_repo=self.OTHER)]
        plan = self._plan(server_mods, [])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.needs_repo, [])
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.entries, [])

    def test_pin_conflict_is_reported_for_a_recommended_mod_too(self):
        """The plan installs the server's version either way, so a pin that
        disagrees is overruled. Checking only `required` meant that happened
        with nothing on screen to say so."""
        self._publish("rec.conf", "1.0.0", parity_required=False)
        self._publish("rec.conf", "2.0.0", parity_required=False)
        server_mods = [_srv("rec.conf", "1.0.0", required=False)]
        index = [summary("rec.conf", ["1.0.0"]), summary("rec.conf", ["2.0.0"])]
        plan = self._plan(server_mods, index,
                            pinned=["rec.conf@2.0.0"])
        self.assertEqual(plan.pin_conflicts, [("rec.conf", "2.0.0", "1.0.0")])

    def test_a_declined_recommendation_is_not_a_pin_conflict(self):
        """Nothing is being installed over the pin, so nothing is overruling
        it. Reporting one anyway would open the comparison window on every join
        over a change that isn't happening."""
        self._publish("rec.conf", "1.0.0", parity_required=False)
        self._publish("rec.conf", "2.0.0", parity_required=False)
        server_mods = [_srv("rec.conf", "1.0.0", required=False)]
        index = [summary("rec.conf", ["1.0.0"]), summary("rec.conf", ["2.0.0"])]
        plan = self._plan(server_mods, index,
                            pinned=["rec.conf@2.0.0"], declined=["rec.conf"])
        self.assertEqual(plan.pin_conflicts, [])

    def test_an_unresolvable_required_mod_is_not_also_a_pin_conflict(self):
        """It's already reported as blocking; a second complaint about the
        version it would have been is noise."""
        server_mods = [_srv("ghost.m", "1.0.0")]
        plan = self._plan(server_mods, [],
                            pinned=["ghost.m@2.0.0"])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.pin_conflicts, [])

    def test_pin_conflict_when_server_requires_a_different_exact_version(self):
        self._publish("conf.m", "2.0.0")
        server_mods = [_srv("conf.m", "2.0.0")]
        index = [summary("conf.m", ["1.0.0", "2.0.0"])]

        plan = self._plan(server_mods, index, pinned=["conf.m@1.0.0"])
        self.assertEqual(plan.pin_conflicts, [("conf.m", "1.0.0", "2.0.0")])
        # The server's exact required version wins the single active entry -
        # the pin conflict is surfaced, not silently overridden or duplicated.
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(self._entry(plan, "conf.m").version, "2.0.0")

    def test_active_true_when_already_installed_at_matching_version(self):
        self._install("act.m", "1.0.0")
        server_mods = [_srv("act.m", "1.0.0")]
        index = [summary("act.m", ["1.0.0"])]

        plan = self._plan(server_mods, index)
        entry = self._entry(plan, "act.m")
        self.assertTrue(entry.active)

    def test_cached_true_when_version_cached_but_not_currently_active(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m", "client_windows")
        mm.uninstall_mod(self.game, "cch.m")
        server_mods = [_srv("cch.m", "1.0.0")]
        index = [summary("cch.m", ["1.0.0"])]

        plan = self._plan(server_mods, index)
        entry = self._entry(plan, "cch.m")
        self.assertTrue(entry.cached)
        self.assertFalse(entry.active)

    def test_to_deactivate_lists_enabled_mod_no_longer_required(self):
        self._install("old.m", "1.0.0")
        plan = self._plan([], [])
        self.assertEqual(plan.to_deactivate, ["old.m"])

    def test_already_disabled_mod_not_listed_to_deactivate(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        plan = self._plan([], [])
        self.assertEqual(plan.to_deactivate, [])

    def test_libraries_collected_for_active_set(self):
        lib = lib_dep("PlanLib")
        self._publish("root.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("root.m", "1.0.0")]
        index = [summary("root.m", ["1.0.0"])]

        plan = self._plan(server_mods, index)
        self.assertEqual([l.filename for l in plan.libraries], ["PlanLib.dll"])


class ModDiff(_TempDataDirFixture, unittest.TestCase):
    """build_mod_diff: the client-vs-server comparison the diff window renders.
    Pure, so none of this needs a display."""

    def _diff(self, server_mods, index):
        plan = self._plan(server_mods, index)
        return {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}, plan

    def test_not_installed_is_an_install(self):
        self._publish("d.new", "1.0.0")
        rows, _ = self._diff([_srv("d.new", "1.0.0")], [summary("d.new", ["1.0.0"])])
        self.assertEqual(rows["d.new"].action, mm.ACTION_INSTALL)
        self.assertEqual(rows["d.new"].server_version, "1.0.0")
        self.assertEqual(rows["d.new"].client_version, "")

    def test_matching_version_is_a_match(self):
        self._install("d.same", "1.0.0")
        rows, _ = self._diff([_srv("d.same", "1.0.0")], [summary("d.same", ["1.0.0"])])
        self.assertEqual(rows["d.same"].action, mm.ACTION_MATCH)
        self.assertEqual(rows["d.same"].client_version, "1.0.0")

    def test_older_installed_is_an_update_newer_is_a_downgrade(self):
        index = [summary("d.up", ["1.0.0"]), summary("d.up", ["2.0.0"])]
        self._install("d.up", "1.0.0")
        self._publish("d.up", "2.0.0")
        rows, _ = self._diff([_srv("d.up", "2.0.0")], index)
        self.assertEqual(rows["d.up"].action, mm.ACTION_UPDATE)

        # The same pair the other way round: the client is ahead and the server
        # pins the older build, which parity makes mandatory rather than optional.
        self._install("d.up", "2.0.0")
        rows, _ = self._diff([_srv("d.up", "1.0.0")], index)
        self.assertEqual(rows["d.up"].action, mm.ACTION_DOWNGRADE)
        self.assertEqual(rows["d.up"].client_version, "2.0.0")
        self.assertEqual(rows["d.up"].server_version, "1.0.0")
        self.assertFalse(rows["d.up"].optional)

    def test_installed_but_disabled_is_an_enable(self):
        self._install("d.off", "1.0.0")
        mm.disable_mod(self.game, "d.off")
        rows, _ = self._diff([_srv("d.off", "1.0.0")], [summary("d.off", ["1.0.0"])])
        self.assertEqual(rows["d.off"].action, mm.ACTION_ENABLE)
        self.assertFalse(rows["d.off"].client_enabled)

    def test_cached_version_activates_rather_than_downloads(self):
        self._install("d.cch", "1.0.0")
        mm.cache_store_mod(self.game, "d.cch", "client_windows")
        mm.uninstall_mod(self.game, "d.cch")
        rows, _ = self._diff([_srv("d.cch", "1.0.0")], [summary("d.cch", ["1.0.0"])])
        row = rows["d.cch"]
        self.assertEqual(row.action, mm.ACTION_ACTIVATE)
        self.assertTrue(row.cached)
        self.assertFalse(row.downloads)     # a file move, no bytes fetched

    def test_downloads_flag_marks_only_rows_that_fetch_bytes(self):
        self._publish("d.dl", "1.0.0")
        rows, _ = self._diff([_srv("d.dl", "1.0.0")], [summary("d.dl", ["1.0.0"])])
        self.assertTrue(rows["d.dl"].downloads)      # not here and not cached

        # An uncached update fetches; a match or a deactivate never does.
        self._install("d.upd", "1.0.0")
        self._publish("d.upd", "2.0.0")
        rows, _ = self._diff([_srv("d.upd", "2.0.0")],
                             [summary("d.upd", ["1.0.0"]), summary("d.upd", ["2.0.0"])])
        self.assertTrue(rows["d.upd"].downloads)

        self._install("d.ok", "1.0.0")
        rows, _ = self._diff([_srv("d.ok", "1.0.0")], [summary("d.ok", ["1.0.0"])])
        self.assertFalse(rows["d.ok"].downloads)

    def test_extra_client_mod_is_an_optional_deactivate(self):
        self._install("d.mine", "1.0.0")
        self._publish("d.theirs", "1.0.0")
        rows, _ = self._diff([_srv("d.theirs", "1.0.0")],
                             [summary("d.theirs", ["1.0.0"])])
        mine = rows["d.mine"]
        self.assertEqual(mine.action, mm.ACTION_DEACTIVATE)
        # The server never checks a mod it doesn't run, so this one is the
        # player's call rather than something applied silently.
        self.assertTrue(mine.optional)
        self.assertEqual(mine.server_version, "")
        self.assertFalse(rows["d.theirs"].optional)

    def test_server_only_mod_is_listed_but_not_required(self):
        rows, _ = self._diff([_srv("d.srv", "1.0.0", targets=("server",))], [])
        self.assertEqual(rows["d.srv"].action, mm.ACTION_SERVER_ONLY)
        self.assertEqual(rows["d.srv"].client_version, "")
        self.assertFalse(rows["d.srv"].blocking)

    def test_unresolvable_required_mod_blocks(self):
        rows, plan = self._diff([_srv("d.gone", "1.0.0")], [])
        self.assertEqual(rows["d.gone"].action, mm.ACTION_MISSING)
        self.assertTrue(rows["d.gone"].blocking)
        self.assertTrue(plan.blocking)

    def test_needs_repo_row_names_the_source_to_add(self):
        server_mods = [_srv("d.other", "1.0.0", source_repo=OTHER)]
        rows, plan = self._diff(server_mods, [])
        row = rows["d.other"]
        self.assertEqual(row.action, mm.ACTION_NEEDS_REPO)
        # Blocking, same as a missing required mod: an apply without it could
        # only fail the parity check afterwards. What makes it the fixable
        # variant is the note naming the exact source to add.
        self.assertTrue(row.blocking)
        self.assertIn(mm._repo_shorthand(OTHER), row.note)

    def test_problems_sort_above_changes_and_matches(self):
        self._install("d.keep", "1.0.0")     # becomes an optional deactivate
        self._publish("d.add", "1.0.0")
        server_mods = [_srv("d.add", "1.0.0"), _srv("d.gone", "1.0.0")]
        plan = self._plan(server_mods, [summary("d.add", ["1.0.0"])])
        order = [r.action for r in mm.build_mod_diff(self.game, server_mods, plan)]
        self.assertEqual(order[0], mm.ACTION_MISSING)
        self.assertLess(order.index(mm.ACTION_INSTALL), order.index(mm.ACTION_DEACTIVATE))

    def test_an_unresolvable_pin_gets_a_visible_but_non_blocking_row(self):
        """Shown, because a pin that silently does nothing is worse than one
        that says why - but not blocking, since no server asked for it. The
        empty server column is what tells the two apart on screen."""
        server_mods = [_srv("d.fine", "1.0.0")]
        self._publish("d.fine", "1.0.0")
        plan = self._plan(server_mods, [summary("d.fine", ["1.0.0"])], pinned=["ghost.pin"])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        row = rows["ghost.pin"]
        self.assertEqual(row.action, mm.ACTION_MISSING)
        self.assertEqual(row.reason, "pinned")
        self.assertEqual(row.server_version, "")
        self.assertFalse(row.blocking)
        # No toggle: it isn't a choice, there is simply nothing to install.
        self.assertFalse(row.optional)
        self.assertIn("pinned", row.note)

    def test_pin_conflict_annotates_the_row_without_duplicating_it(self):
        self._publish("d.pin", "1.0.0")
        self._publish("d.pin", "2.0.0")
        index = [summary("d.pin", ["1.0.0"]), summary("d.pin", ["2.0.0"])]
        server_mods = [_srv("d.pin", "1.0.0")]
        plan = self._plan(server_mods, index, pinned=["d.pin@2.0.0"])
        rows = mm.build_mod_diff(self.game, server_mods, plan)
        self.assertEqual(len([r for r in rows if r.mod_id == "d.pin"]), 1)
        self.assertIn("2.0.0", rows[0].note)
        self.assertIn("1.0.0", rows[0].note)


class RenderActiveSet(_TempDataDirFixture, unittest.TestCase):
    """render_active_set: applying a JoinPlan to disk."""

    def test_downloads_missing_required_mod_and_caches_it(self):
        self._publish("dl.m", "1.0.0")
        server_mods = [_srv("dl.m", "1.0.0")]
        index = [summary("dl.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)

        mm.render_active_set(self.game, plan, noop, noop)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dl.m", ".client_windows", "dl.m.dll")))
        self.assertTrue(os.path.isdir(self._cache_entry("dl.m", "1.0.0")))

    def test_restores_from_cache_without_redownload(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m", "client_windows")
        mm.uninstall_mod(self.game, "cch.m")

        server_mods = [_srv("cch.m", "1.0.0")]
        index = [summary("cch.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan, noop, noop)
        self.assertEqual(len(self.dl_dirs), downloads_before)   # no new download
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "cch.m", ".client_windows", "cch.m.dll")))

    def test_adopting_first_preserves_the_version_a_render_replaces(self):
        """Why the client adopts installed mods before planning: rendering another
        version over an uncached install would destroy the only copy."""
        self._install("adopt.m", "1.0.0")
        self._publish("adopt.m", "2.0.0")
        self.assertFalse(os.path.isdir(self._cache_entry("adopt.m", "1.0.0")))

        mm.adopt_installed_mods(self.game, "client_windows")
        self.assertTrue(os.path.isdir(self._cache_entry("adopt.m", "1.0.0")))

        server_mods = [_srv("adopt.m", "2.0.0")]
        index = [summary("adopt.m", ["1.0.0"]), summary("adopt.m", ["2.0.0"])]
        mm.render_active_set(self.game, self._plan(server_mods, index), noop, noop)

        # 2.0.0 is now what's live, and 1.0.0 survived the swap in the cache.
        self.assertEqual(mm._read_mod_record(self.game, "adopt.m")["version"], "2.0.0")
        self.assertTrue(os.path.isdir(self._cache_entry("adopt.m", "1.0.0")))

        # Dropping back to 1.0.0 is therefore a file move, not a download.
        back = [_srv("adopt.m", "1.0.0")]
        plan_back = self._plan(back, index)
        self.assertTrue(plan_back.entries[0].cached)
        downloads_before = len(self.dl_dirs)
        mm.render_active_set(self.game, plan_back, noop, noop)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertEqual(mm._read_mod_record(self.game, "adopt.m")["version"], "1.0.0")

    def _damage(self, mod_id):
        with open(self._mod_file(mod_id, f"{mod_id}.dll"), "wb") as f:
            f.write(b"eaten by antivirus")
        mm._verify_memo.clear()

    def test_a_damaged_required_mod_is_repaired_not_matched(self):
        """Right version, enabled, files wrong: a version-only match would
        launch the game broken."""
        self._install("dmg.m", "1.0.0")
        self._damage("dmg.m")
        server_mods = [_srv("dmg.m", "1.0.0")]
        index = [summary("dmg.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)
        self.assertFalse(plan.entries[0].active)
        row = mm.build_mod_diff(self.game, server_mods, plan)[0]
        self.assertEqual(row.action, mm.ACTION_INSTALL)
        self.assertIn("damaged", row.note)

        mm.render_active_set(self.game, plan, noop, noop)
        self.assertIs(mm.verify_mod_files(self.game, "dmg.m", "client_windows"), True)
        self.assertTrue(self._plan(server_mods, index).entries[0].active)

    def test_a_damaged_disabled_mod_is_reinstalled_not_just_enabled(self):
        self._install("dmgoff.m", "1.0.0")
        mm.disable_mod(self.game, "dmgoff.m")
        self._damage("dmgoff.m")
        server_mods = [_srv("dmgoff.m", "1.0.0")]
        plan = self._plan(server_mods, [summary("dmgoff.m", ["1.0.0"])])
        mm.render_active_set(self.game, plan, noop, noop)
        self.assertIs(mm.verify_mod_files(self.game, "dmgoff.m", "client_windows"), True)
        self.assertTrue(os.path.isfile(mm._mod_record_path(self.game, "dmgoff.m")))

    def test_a_damaged_mod_is_never_cached_as_the_good_copy(self):
        self._install("dmgc.m", "1.0.0")
        self._damage("dmgc.m")
        self.assertFalse(mm.cache_store_mod(self.game, "dmgc.m", "client_windows"))
        self.assertFalse(os.path.isdir(self._cache_entry("dmgc.m", "1.0.0")))

    def test_already_active_entry_is_left_alone(self):
        self._install("act.m", "1.0.0")
        server_mods = [_srv("act.m", "1.0.0")]
        index = [summary("act.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan, noop, noop)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "act.m", ".client_windows", "act.m.dll")))

    def test_deactivated_mod_is_disabled_not_uninstalled(self):
        """Deactivating is a rename, not a delete. The mod stays installed and
        listed, its files stay on disk, and only the record moves so
        MelonLoader stops scanning the folder."""
        self._install("old.m", "1.0.0")
        plan = self._plan([], [])   # nothing required -> deactivate everything

        mm.render_active_set(self.game, plan, noop, noop)
        mod_dir = os.path.join(self.game, "Mods", "old.m")
        self.assertTrue(os.path.isfile(self._mod_file("old.m", "old.m.dll")))
        self.assertFalse(os.path.isfile(os.path.join(mod_dir, mm.RECORD_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(mod_dir, mm.DISABLED_RECORD_NAME)))

        # Still installed, just not enabled - so it keeps its place in the list.
        listed = {r["id"]: r for r in mm.list_installed_mods(self.game)}
        self.assertIn("old.m", listed)
        self.assertFalse(listed["old.m"]["enabled"])
        # And not reported to a server, which is what makes the parity check pass.
        self.assertEqual(mm.handshake_snapshot(self.game, "client_windows")[2], [])
        # Cached all the same, so a version switch later is still a file move.
        self.assertTrue(os.path.isdir(self._cache_entry("old.m", "1.0.0")))
        # And a second sync finds nothing left to do, since to_deactivate only
        # counts enabled mods. That's what stops it disabling the same mod forever.
        self.assertEqual(self._plan([], []).to_deactivate, [])

    def test_reactivating_a_disabled_mod_needs_no_download_or_cache_copy(self):
        """The counterpart: a mod disabled at the version now wanted comes back
        with a rename. Nothing is fetched, and it works with the cache emptied."""
        self._install("back.m", "1.0.0")
        mm.render_active_set(self.game, self._plan([], []), noop, noop)

        # Wipe the cache to prove the files on disk are what's being reused.
        shutil.rmtree(self._cache_entry("back.m", "1.0.0"))
        downloads_before = len(self.dl_dirs)

        server_mods = [_srv("back.m", "1.0.0")]
        index = [summary("back.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)
        self.assertEqual([e.cached for e in plan.entries], [False])

        mm.render_active_set(self.game, plan, noop, noop)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        mod_dir = os.path.join(self.game, "Mods", "back.m")
        self.assertTrue(os.path.isfile(os.path.join(mod_dir, mm.RECORD_NAME)))
        self.assertFalse(os.path.isfile(os.path.join(mod_dir, mm.DISABLED_RECORD_NAME)))

    def test_a_disabled_mod_at_the_wrong_version_still_switches_properly(self):
        """Enabling in place is only right when the version already matches;
        otherwise the wanted version has to come from the cache or the network."""
        self._install("ver.m", "1.0.0")
        self._publish("ver.m", "2.0.0")
        mm.render_active_set(self.game, self._plan([], []), noop, noop)

        server_mods = [_srv("ver.m", "2.0.0")]
        index = [summary("ver.m", ["1.0.0"]), summary("ver.m", ["2.0.0"])]
        mm.render_active_set(self.game, self._plan(server_mods, index), noop, noop)

        listed = {r["id"]: r for r in mm.list_installed_mods(self.game)}
        self.assertTrue(listed["ver.m"]["enabled"])
        self.assertEqual(listed["ver.m"]["version"], "2.0.0")
        # The version it was disabled at is still cached, so switching back is a move.
        self.assertTrue(os.path.isdir(self._cache_entry("ver.m", "1.0.0")))

    def test_library_downloaded_then_orphan_pruned_after_deactivation(self):
        lib = lib_dep("Prune")
        self._publish("lib.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("lib.m", "1.0.0")]
        index = [summary("lib.m", ["1.0.0"])]

        plan = self._plan(server_mods, index)
        mm.render_active_set(self.game, plan, noop, noop)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "Prune.dll")))
        sha = self._lib_sha("Prune")
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Prune.dll", sha)))

        # Now nothing requires lib.m any more - deactivating it should prune
        # the now-orphaned library from UserLibs/ (it stays cached, though).
        plan2 = self._plan([], [])
        mm.render_active_set(self.game, plan2, noop, noop)
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "Prune.dll")))
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Prune.dll", sha)))

    def test_library_restored_from_cache_without_redownload(self):
        lib = lib_dep("Reuse")
        self._publish("lib2.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("lib2.m", "1.0.0")]
        index = [summary("lib2.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)
        mm.render_active_set(self.game, plan, noop, noop)

        # Deactivate, then reactivate - the library should come back from
        # cache, not a fresh download.
        plan_off = self._plan([], [])
        mm.render_active_set(self.game, plan_off, noop, noop)
        downloads_before = len(self.dl_dirs)

        plan_on = self._plan(server_mods, index)
        mm.render_active_set(self.game, plan_on, noop, noop)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "Reuse.dll")))

    def test_blocking_plan_raises_and_changes_nothing(self):
        server_mods = [_srv("ghost.m", "1.0.0")]
        plan = self._plan(server_mods, [])
        self.assertTrue(plan.blocking)
        with self.assertRaises(mm.ModManagerError):
            mm.render_active_set(self.game, plan, noop, noop)
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods")))

    # on_step / render_plan_steps: what ModDiffWindow shows progress from.

    def _stepped(self, plan):
        """Renders and returns the on_step calls in order."""
        steps = []
        mm.render_active_set(self.game, plan, noop, lambda i, s: steps.append((i, s)))
        return steps

    def test_every_item_reports_a_start_and_a_done(self):
        self._publish("step.a", "1.0.0")
        self._publish("step.b", "1.0.0")
        server_mods = [_srv(i, "1.0.0")
                       for i in ("step.a", "step.b")]
        index = [summary("step.a", ["1.0.0"]), summary("step.b", ["1.0.0"])]
        plan = self._plan(server_mods, index)

        steps = self._stepped(plan)
        self.assertEqual(steps, [("step.a", "start"), ("step.a", "done"),
                                 ("step.b", "start"), ("step.b", "done")])

    def test_deactivation_reports_a_step_too(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        server_mods = [_srv("keep.m", "1.0.0")]
        index = [summary("keep.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)

        self.assertEqual(plan.to_deactivate, ["drop.m"])
        self.assertEqual(self._stepped(plan),
                         [("drop.m", "start"), ("drop.m", "done")])

    def test_step_count_matches_render_plan_steps_including_libraries(self):
        """The bar is sized from render_plan_steps before anything runs, so the
        two must agree exactly or it stalls short of the end or overshoots."""
        lib = lib_dep("Shared")
        self._publish("libbed.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("libbed.m", "1.0.0")]
        index = [summary("libbed.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)

        self.assertEqual(mm.render_plan_steps(plan), 2)      # the mod + its library
        steps = self._stepped(plan)
        self.assertEqual([s for _, s in steps].count("done"), mm.render_plan_steps(plan))
        self.assertIn(("Shared.dll", "done"), steps)

    def test_library_already_active_still_reports_its_step(self):
        """It's skipped for work, not for counting: a bar sized against
        render_plan_steps would never fill if the skip stayed silent."""
        lib = lib_dep("Shared")
        self._install("libbed.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("libbed.m", "1.0.0")]
        index = [summary("libbed.m", ["1.0.0"])]
        plan = self._plan(server_mods, index)

        # Nothing to do for the mod itself, so the library is the whole render.
        self.assertEqual([e.active for e in plan.entries], [True])
        self.assertEqual(mm.render_plan_steps(plan), 1)
        self.assertEqual(self._stepped(plan),
                         [("Shared.dll", "start"), ("Shared.dll", "done")])

class Parity(_TempDataDirFixture, unittest.TestCase):
    """parity_required: whether a server running a mod obliges the client to
    match it. Never defaulted: unstated is rejected or ignored."""

    # -- reading the value ---------------------------------------------------

    def test_absent_parity_is_an_error_not_a_default(self):
        """No default anywhere. Guessing is the one mistake that matters here,
        since this decides whether a joining client is obliged to match."""
        with self.assertRaises(mm.ModManagerError) as caught:
            mm.parity_required({"id": "a.b"})
        self.assertIn("parity_required", str(caught.exception))

    def test_malformed_parity_is_an_error_not_a_coercion(self):
        """Rejected, as TavernLib's ModParityField.Read rejects the same bytes. 0
        and 1 included: isinstance(1, bool) is False."""
        for bad in ("false", "true", "", None, 0, 1, [], {}, "optional"):
            with self.assertRaises(mm.ModManagerError, msg=repr(bad)):
                mm.parity_required({"id": "a.b", "parity_required": bad})

    def test_only_a_real_bool_is_accepted(self):
        self.assertFalse(mm.parity_required({"parity_required": False}))
        self.assertTrue(mm.parity_required({"parity_required": True}))

    def test_a_manifest_without_the_field_is_rejected(self):
        """Published manifests must state it outright."""
        d = manifest_dict("no.parity", "1.0.0")
        d.pop("parity_required")
        with self.assertRaises(mm.ModManagerError) as caught:
            mm._manifest_from_dict(d, self.BASE)
        self.assertIn("parity_required", str(caught.exception))

    def _break_record(self, mod_id, mutate):
        record_path = mm._mod_record_path(self.game, mod_id)
        with io.open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        mutate(record)
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

    def test_a_record_without_the_field_is_ignored_not_assumed(self):
        """A record predating the field can't be read, so the mod counts as not
        installed rather than being assumed required. Recoverable: the next plan
        reinstalls it and the new record carries the field."""
        self._install("p.old", "1.0.0")
        self._break_record("p.old", lambda r: r.pop("parity_required"))

        self.assertEqual(mm.list_installed_mods(self.game), [])
        self.assertEqual(mm.handshake_snapshot(self.game, "client_windows")[2], [])
        # The files are still there, so reinstalling is what fixes it.
        self.assertTrue(os.path.isdir(mm._mod_dir_path(self.game, "p.old")))
        # Not ours by the shared predicate, so the folder demotes to untracked
        # rather than vanishing - it IS still loading. TavernLib reads the same
        # record as null (parity is Required.Always) and reports the same set.
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)],
                         ["p.old"])

    def test_a_record_with_a_malformed_field_is_ignored_too(self):
        """Treated like an absent field, which also keeps handshake_snapshot from
        raising mid-fingerprint."""
        self._install("p.junk", "1.0.0")
        self._break_record("p.junk", lambda r: r.update(parity_required="yes"))

        self.assertEqual(mm.list_installed_mods(self.game), [])
        self.assertEqual(mm.handshake_snapshot(self.game, "client_windows")[2], [])
        # Same demotion as an absent field: unusable record, untracked folder.
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)],
                         ["p.junk"])

    def test_an_index_entry_without_a_usable_field_is_skipped(self):
        """One bad entry is dropped; the rest of the index still loads."""
        d = {"name": "M", "author": "a", "description": "",
             "targets": ["client_windows", "server"], "versions": ["1.0.0"]}
        self.assertIsNone(mm._summary_from_dict(d, "no.parity", self.BASE))
        d["parity_required"] = "yes"
        self.assertIsNone(mm._summary_from_dict(d, "no.parity", self.BASE))
        d["parity_required"] = True
        self.assertIsNotNone(mm._summary_from_dict(d, "no.parity", self.BASE))

    def test_a_server_entry_without_the_field_fails_the_plan(self):
        """A server on an older TavernLib can't be planned against, rather than
        having its requirements guessed at."""
        with self.assertRaises(mm.ModManagerError):
            self._plan([{"id": "p.x", "version": "1.0.0",
                           "targets": ["client_windows", "server"]}],
                         [])

    # -- carrying it through -------------------------------------------------

    def test_manifest_parity_reaches_the_install_record_and_the_handshake(self):
        self._install("p.opt", "1.0.0", parity_required=False)
        self._install("p.req", "1.0.0")

        rec = mm._read_mod_record(self.game, "p.opt")
        self.assertIs(rec["parity_required"], False)

        _, _, mods, _ = mm.handshake_snapshot(self.game, "client_windows")
        by_id = {m["id"]: m for m in mods}
        self.assertIs(by_id["p.opt"]["parity_required"], False)
        self.assertIs(by_id["p.req"]["parity_required"], True)

    # -- planning ------------------------------------------------------------

    def test_optional_mod_is_planned_in_by_default(self):
        """Recommended still means installed unless the user says otherwise."""
        self._publish("p.rec", "1.0.0", parity_required=False)
        plan = self._plan([_srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])])
        self.assertEqual([(e.mod_id, e.reason) for e in plan.entries],
                         [("p.rec", "recommended")])
        self.assertEqual(plan.declined, [])

    def test_declining_leaves_an_optional_mod_out_of_the_plan(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        plan = self._plan([_srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])],
                            declined=["p.rec"])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.declined, [("p.rec", "1.0.0")])

    def test_declining_a_required_mod_is_ignored(self):
        """No client-side choice can make that join work, so pretending it can
        would just move the rejection to the server."""
        self._publish("p.req", "1.0.0")
        plan = self._plan([_srv("p.req", "1.0.0", required=True)],
                            [summary("p.req", ["1.0.0"])],
                            declined=["p.req"])
        self.assertEqual([e.mod_id for e in plan.entries], ["p.req"])
        self.assertEqual(plan.declined, [])

    def test_declining_never_deactivates_something_already_installed(self):
        """Declining is 'don't add it', not 'take it away'."""
        self._install("p.rec", "1.0.0", parity_required=False)
        plan = self._plan([_srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])],
                            declined=["p.rec"])
        self.assertEqual(plan.to_deactivate, [])
        self.assertEqual(plan.entries, [])

    def test_an_unresolvable_optional_mod_does_not_block(self):
        """Required and unresolvable blocks the join; recommended just doesn't
        happen, since the server allows a client not to have it."""
        plan = self._plan([_srv("p.gone", "1.0.0", required=False)],
                            [])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.entries, [])

    # -- the comparison ------------------------------------------------------

    def test_recommended_rows_are_the_users_call_required_ones_are_not(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        self._publish("p.req", "1.0.0")
        server_mods = [_srv("p.rec", "1.0.0", required=False),
                       _srv("p.req", "1.0.0", required=True)]
        index = [summary("p.rec", ["1.0.0"]), summary("p.req", ["1.0.0"])]
        plan = self._plan(server_mods, index)
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        self.assertTrue(rows["p.rec"].optional)
        self.assertTrue(rows["p.rec"].recommended)
        self.assertIn("doesn't require it", rows["p.rec"].note)
        self.assertFalse(rows["p.req"].optional)
        self.assertFalse(rows["p.req"].recommended)

    def _recommended_dependency(self):
        """p.req requires p.mid, which needs the recommended p.rec: the server
        lists p.rec itself, so it's a root rather than a dependency."""
        self._publish("p.rec", "1.0.0", parity_required=False)
        self._publish("p.mid", "1.0.0", dependencies={"p.rec": "1.0.0"})
        self._publish("p.req", "1.0.0", dependencies={"p.mid": "1.0.0"})
        server_mods = [_srv("p.rec", "1.0.0", required=False),
                       _srv("p.req", "1.0.0", required=True)]
        index = [summary(m, ["1.0.0"]) for m in ("p.rec", "p.mid", "p.req")]
        return server_mods, index

    def test_a_recommended_mod_a_required_one_needs_cannot_be_skipped(self):
        """Skipping it would leave p.req loading without it."""
        server_mods, index = self._recommended_dependency()
        plan = self._plan(server_mods, index)
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        self.assertEqual({e.mod_id: e.reason for e in plan.entries}["p.rec"], "dependency")
        self.assertFalse(rows["p.rec"].optional)
        self.assertFalse(rows["p.rec"].recommended)
        self.assertIn("needs it", rows["p.rec"].note)
        self.assertEqual(rows["p.rec"].server_version, "1.0.0")

    def test_a_declined_recommendation_a_required_mod_needs_is_still_planned(self):
        server_mods, index = self._recommended_dependency()
        plan = self._plan(server_mods, index, declined=["p.rec"])
        self.assertIn("p.rec", [e.mod_id for e in plan.entries])

    def test_rows_carry_their_dependencies_for_the_comparison(self):
        """The window uses them to skip or take recommended mods together."""
        self._publish("p.lib", "1.0.0", parity_required=False)
        self._publish("p.rec", "1.0.0", parity_required=False,
                      dependencies={"p.lib": "1.0.0"})
        server_mods = [_srv("p.lib", "1.0.0", required=False),
                       _srv("p.rec", "1.0.0", required=False)]
        plan = self._plan(server_mods, [summary(m, ["1.0.0"]) for m in ("p.lib", "p.rec")])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        self.assertEqual(rows["p.rec"].requires, ("p.lib",))
        self.assertTrue(rows["p.lib"].optional)

    def test_a_declined_mod_gets_a_row_so_the_choice_can_be_taken_back(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        server_mods = [_srv("p.rec", "1.0.0", required=False)]
        plan = self._plan(server_mods, [summary("p.rec", ["1.0.0"])], declined=["p.rec"])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        self.assertEqual(rows["p.rec"].action, mm.ACTION_SKIPPED)
        self.assertTrue(rows["p.rec"].optional)
        self.assertTrue(rows["p.rec"].recommended)
        self.assertEqual(rows["p.rec"].server_version, "1.0.0")

    def test_an_already_matching_recommended_mod_is_not_offered_as_a_choice(self):
        """Nothing to decide: it's installed, current, and the server is happy."""
        self._install("p.rec", "1.0.0", parity_required=False)
        server_mods = [_srv("p.rec", "1.0.0", required=False)]
        plan = self._plan(server_mods, [summary("p.rec", ["1.0.0"])])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}
        self.assertEqual(rows["p.rec"].action, mm.ACTION_MATCH)
        self.assertFalse(rows["p.rec"].optional)

    # -- remembering the choice ----------------------------------------------

    def _cfg(self):
        """set_declined saves through the host's save_cfg, so wire a recorder in
        its place and hand back the dict it writes to."""
        saved = []
        prev_load, prev_save = base._load, base._save
        base.set_config_accessors(prev_load, saved.append)
        self.addCleanup(lambda: base.set_config_accessors(prev_load, prev_save))
        return {}, saved

    def test_declines_are_stored_per_server(self):
        cfg, _ = self._cfg()
        mm.set_declined(cfg, "one.example", ["a.b"])
        mm.set_declined(cfg, "two.example", ["c.d"])
        self.assertEqual(mm.list_declined(cfg, "one.example"), ["a.b"])
        self.assertEqual(mm.list_declined(cfg, "two.example"), ["c.d"])
        self.assertEqual(mm.list_declined(cfg, "never.seen"), [])

    def test_setting_declines_replaces_rather_than_merges(self):
        """The window hands back the whole set each time, so taking a choice
        back has to actually remove it."""
        cfg, _ = self._cfg()
        mm.set_declined(cfg, "one.example", ["a.b", "c.d"])
        mm.set_declined(cfg, "one.example", ["a.b"])
        self.assertEqual(mm.list_declined(cfg, "one.example"), ["a.b"])

    def test_a_server_with_nothing_declined_leaves_no_entry_behind(self):
        cfg, _ = self._cfg()
        mm.set_declined(cfg, "one.example", ["a.b"])
        mm.set_declined(cfg, "one.example", [])
        self.assertEqual(cfg[mm.CFG_DECLINED_KEY], {})

    def test_the_choice_is_actually_persisted_not_just_held_in_memory(self):
        cfg, saved = self._cfg()
        mm.set_declined(cfg, "one.example", ["a.b"])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][mm.CFG_DECLINED_KEY], {"one.example": ["a.b"]})


class ManifestIdentity(_FakeInstallFixture, unittest.TestCase):
    """A per-version manifest must be the one asked for. A repo serving some
    other id or version at that path fails for that repo, the same as a 404,
    so the next repo is tried and nothing is installed under the wrong name."""

    def _serve_at(self, base, mod_id, at_version, **over):
        author, repo = mod_id.split(".", 1)
        url = f"{base}/manifests/{author}/{repo}/{at_version}.json"
        d = manifest_dict(mod_id, at_version)
        d.update(over)
        self.manifests[url] = d

    def test_a_mismatched_manifest_falls_through_to_the_next_repo(self):
        self._serve_at(self.BASE, "want.m", "1.0.0", id="other.m")
        self._serve_at(OTHER, "want.m", "1.0.0")
        m = mm._fetch_manifest([self.BASE, OTHER], "want.m", "1.0.0")
        self.assertEqual((m.id, m.version, m.source_repo), ("want.m", "1.0.0", OTHER))

    def test_a_version_manifest_is_fetched_once_per_repo(self):
        """Immutable by contract, so plans, re-plans and installs reuse it."""
        self._serve_at(self.BASE, "once.m", "1.0.0")
        fetched = self._counting()
        first = mm._fetch_manifest([self.BASE], "once.m", "1.0.0")
        self.assertIs(mm._fetch_manifest([self.BASE], "once.m", "1.0.0"), first)
        self.assertEqual(len(fetched), 1)

    def test_latest_pointers_are_never_memoized(self):
        author_repo = f"{self.BASE}/manifests/lat/m/latest.json"
        self.manifests[author_repo] = manifest_dict("lat.m", "1.0.0")
        self.assertEqual(mm.resolve_mod_by_id([self.BASE], "lat.m").version, "1.0.0")
        self.manifests[author_repo] = manifest_dict("lat.m", "1.1.0")
        self.assertEqual(mm.resolve_mod_by_id([self.BASE], "lat.m").version, "1.1.0")

    def test_a_failed_fetch_is_not_memoized(self):
        with self.assertRaises(mm.ModManagerError):
            mm._fetch_manifest([self.BASE], "late.m", "1.0.0")
        self._serve_at(self.BASE, "late.m", "1.0.0")
        self.assertEqual(mm._fetch_manifest([self.BASE], "late.m", "1.0.0").id, "late.m")

    def _counting(self):
        fetched = []
        real = repos._get_json

        def counting(url, timeout=15):
            fetched.append(url)
            return real(url, timeout)
        stub(self, repos, "_get_json", counting)
        return fetched

    def test_roots_are_fetched_from_the_repo_the_index_found_them_in(self):
        """No 404 against the default repo first for a third-party mod."""
        self._serve_at(OTHER, "third.m", "1.0.0")
        fetched = self._counting()
        index = [summary("third.m", ["1.0.0"], source=OTHER)]
        plan = mm.plan_join(self.game, [_srv("third.m", "1.0.0")], index, [self.BASE, OTHER])
        self.assertEqual([e.source_repo for e in plan.entries], [OTHER])
        self.assertEqual(fetched, [f"{OTHER}/manifests/third/m/1.0.0.json"])

    def test_an_index_source_outside_the_clients_repos_is_not_preferred(self):
        self._serve_at(OTHER, "third.m", "1.0.0")
        fetched = self._counting()
        index = [summary("third.m", ["1.0.0"], source=OTHER)]
        self._plan([_srv("third.m", "1.0.0")], index)
        self.assertEqual(fetched, [f"{self.BASE}/manifests/third/m/1.0.0.json"])

    def test_a_mismatch_everywhere_raises_naming_both(self):
        self._serve_at(self.BASE, "want.m", "1.0.0", version="2.0.0")
        with self.assertRaises(mm.ModManagerError) as caught:
            mm._fetch_manifest([self.BASE], "want.m", "1.0.0")
        msg = str(caught.exception)
        self.assertIn("'want.m' 2.0.0", msg)
        self.assertIn("'want.m' 1.0.0", msg)


class ResolveMissingMods(_FakeInstallFixture, unittest.TestCase):
    """resolve_missing_mods: turns a rejection payload's `missing` entries
    into installable manifests, via the client's own configured repos
    only; source_repo is never trusted directly."""

    def test_resolves_missing_entries_to_manifests(self):
        self._publish("miss.m", "1.4.0")
        missing = [{"id": "miss.m", "version": "1.4.0", "source_repo": self.BASE}]
        roots, deps = mm.resolve_missing_mods(missing, [], [self.BASE])
        self.assertEqual([(m.id, m.version) for m in roots], [("miss.m", "1.4.0")])
        self.assertEqual(deps, [])

    def test_pulls_in_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        missing = [{"id": "root.m", "version": "1.0.0", "source_repo": self.BASE}]
        index = [summary("dep.k", ["1.0.0"])]
        roots, deps = mm.resolve_missing_mods(missing, index, [self.BASE])
        self.assertEqual([m.id for m in roots], ["root.m"])
        self.assertEqual([m.id for m in deps], ["dep.k"])

    def test_unresolvable_entry_names_unadded_hint_repo(self):
        missing = [{"id": "elsewhere.m", "version": "1.0.0", "source_repo": OTHER}]
        with self.assertRaises(mm.ModManagerError) as ctx:
            mm.resolve_missing_mods(missing, [], [self.BASE])
        self.assertIn("elsewhere.m", str(ctx.exception))
        self.assertIn(OTHER, str(ctx.exception))

    def test_unresolvable_entry_with_no_hint_reports_not_found(self):
        missing = [{"id": "ghost.m", "version": "1.0.0"}]
        with self.assertRaises(mm.ModManagerError) as ctx:
            mm.resolve_missing_mods(missing, [], [self.BASE])
        self.assertIn("ghost.m", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    def test_multiple_missing_entries_all_resolved(self):
        self._publish("a.a", "1.0.0")
        self._publish("b.b", "2.0.0")
        missing = [{"id": "a.a", "version": "1.0.0"}, {"id": "b.b", "version": "2.0.0"}]
        roots, deps = mm.resolve_missing_mods(missing, [], [self.BASE])
        self.assertEqual(sorted(m.id for m in roots), ["a.a", "b.b"])


class ModlistImportExport(_FakeInstallFixture, unittest.TestCase):
    """export_modlist/import_modlist/apply_import: the shared
    {schema, repos, mods} shape, reused as-is by a headless server's modlist."""

    def _publish_latest(self, mod_id, version, **over):
        """Like _publish, but also serves it at latest.json, so bare-id
        (non-pinned) modlist entries can resolve via resolve_mod_by_id."""
        d = self._publish(mod_id, version, **over)
        author, repo = mod_id.split(".", 1)
        self.manifests[f"{self.BASE}/manifests/{author}/{repo}/latest.json"] = d
        return d

    def test_export_lists_only_enabled_mods_as_bare_ids(self):
        self._install("en.m", "1.0.0")
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")

        out = mm.export_modlist(self.game, {}, "client_windows")
        self.assertEqual(out["schema"], mm.MODLIST_SCHEMA)
        self.assertEqual(out["mods"], ["en.m"])

    def test_export_pin_versions_writes_exact_versions(self):
        self._install("pin.m", "1.4.0")
        out = mm.export_modlist(self.game, {}, "client_windows", pin_versions=True)
        self.assertEqual(out["mods"], ["pin.m@1.4.0"])

    def test_export_repos_are_informational_shorthands(self):
        cfg = {}
        self.manifests[f"{OTHER}/repository.json"] = slim_index({})
        mm.add_repo(cfg, OTHER)
        out = mm.export_modlist(self.game, cfg, "client_windows")
        self.assertIn("someone/theirmods", out["repos"])
        self.assertIn("ModdingTavern/TavernMods", out["repos"])

    def test_import_resolves_bare_id_via_latest(self):
        self._publish_latest("lat.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["lat.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")
        self.assertFalse(plan.blocking)
        self.assertEqual([(m.id, m.version) for m in plan.roots], [("lat.m", "2.0.0")])
        self.assertEqual(plan.untracked, [])    # a modlist from before the key

    # -- untracked mods in a modlist -----------------------------------------

    def _untracked(self, name, content=""):
        path = os.path.join(self.game, "Mods", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_export_records_untracked_mods_separately_from_mods(self):
        """Never inside `mods`: TavernLib parses every entry there as an id and
        would resolve "Manual.dll" against the trusted repos on each boot."""
        self._install("en.m", "1.0.0")
        self._untracked("Manual.dll")

        out = mm.export_modlist(self.game, {}, "client_windows")
        self.assertEqual(out["mods"], ["en.m"])
        self.assertEqual(out["untracked"], [{"name": "Manual.dll", "kind": "file"}])

    def test_export_skips_disabled_untracked_mods(self):
        """Same enabled-only rule as `mods` - the file records the active set,
        not everything sitting on disk."""
        self._untracked("On.dll")
        self._untracked("Off.dll")
        mm.disable_untracked_dll(self.game, "Off.dll")

        out = mm.export_modlist(self.game, {}, "client_windows")
        self.assertEqual([u["name"] for u in out["untracked"]], ["On.dll"])

    def test_export_has_the_key_even_with_nothing_untracked(self):
        """Always present, so a reader never has to tell "none" from "an
        exporter too old to say"."""
        self._install("en.m", "1.0.0")
        self.assertEqual(mm.export_modlist(self.game, {}, "client_windows")["untracked"], [])

    def test_import_carries_untracked_through_without_resolving_it(self):
        self._publish_latest("lat.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["lat.m"],
                   "untracked": [{"name": "Manual.dll", "kind": "file"}]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")

        self.assertFalse(plan.blocking)
        self.assertEqual(plan.untracked, [{"name": "Manual.dll", "kind": "file"}])
        # Display only: it never becomes something to install, and never counts
        # as unresolved either - there was nothing to resolve in the first place.
        self.assertEqual([m.id for m in plan.roots], ["lat.m"])
        self.assertEqual(plan.unresolved, [])

    def test_an_untracked_block_never_blocks_an_import(self):
        modlist = {"schema": 1, "repos": [], "mods": [],
                   "untracked": [{"name": "Manual.dll", "kind": "file"}]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")
        self.assertFalse(plan.blocking)

    def test_importing_never_disables_the_importers_own_untracked_mods(self):
        """The invariant: nothing automatic touches an untracked mod. A modlist
        that doesn't mention your hand-installed mod is not a request to remove
        it - only `mods` describes an active set anyone can act on."""
        self._publish_latest("lat.m", "2.0.0")
        self._install("mine.m", "1.0.0")
        self._untracked("Manual.dll")

        plan = mm.import_modlist({"schema": 1, "repos": [], "mods": ["lat.m"]},
                                 [], [self.BASE], target="client_windows")
        # The managed mod is on the disable list; the untracked one isn't there
        # to be listed at all.
        self.assertEqual(mm.modlist_import_disables(self.game, plan), ["mine.m"])

        mm.apply_import(self.game, plan, self.progress.append)
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)],
                         ["Manual.dll"])
        self.assertTrue(mm.list_untracked_mods(self.game)[0]["enabled"])

    def test_export_import_round_trips_the_untracked_block(self):
        self._publish_latest("lat.m", "2.0.0")
        self._install("lat.m", "2.0.0")
        self._untracked("Manual.dll")

        exported = mm.export_modlist(self.game, {}, "client_windows")
        plan = mm.import_modlist(exported, [], [self.BASE], target="client_windows")
        self.assertEqual([u["name"] for u in plan.untracked], ["Manual.dll"])

    def test_import_resolves_pinned_exact_version(self):
        self._publish("pin.m", "1.0.0")
        self._publish("pin.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["pin.m@1.0.0"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")
        self.assertEqual([(m.id, m.version) for m in plan.roots], [("pin.m", "1.0.0")])

    def test_import_pulls_in_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE], target="client_windows")
        self.assertEqual([m.id for m in plan.roots], ["root.m"])
        self.assertEqual([m.id for m in plan.dependencies], ["dep.k"])

    def test_import_unresolved_entry_blocks(self):
        modlist = {"schema": 1, "repos": ["Some/Pack"], "mods": ["ghost.m@1.0.0"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.unresolved, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.hinted_repos, ["Some/Pack"])

    def test_import_rejects_unsupported_schema(self):
        modlist = {"schema": 2, "repos": [], "mods": []}
        with self.assertRaises(mm.ModManagerError):
            mm.import_modlist(modlist, [], [self.BASE], target="client_windows")

    def _publish_conflicting_pair(self):
        """Two mods pinning the same library filename at different content -
        the library equivalent of a diamond conflict, which
        collect_library_dependencies refuses."""
        def lib(url, sha):
            return {"name": "Shared", "download_url": url,
                    "sha256": sha, "filename": "Shared.dll"}
        self._publish_latest("one.m", "1.0.0",
                             library_dependencies=[lib("https://x/a.dll", "a" * 64)])
        self._publish_latest("two.m", "1.0.0",
                             library_dependencies=[lib("https://x/b.dll", "b" * 64)])

    def test_import_surfaces_a_library_conflict_at_plan_time(self):
        """Raised while resolving, so the confirmation the user is shown is one
        that can actually be carried out."""
        self._publish_conflicting_pair()
        modlist = {"schema": 1, "repos": [], "mods": ["one.m", "two.m"]}
        with self.assertRaises(mm.ModManagerError) as caught:
            mm.import_modlist(modlist, [], [self.BASE], target="client_windows")
        self.assertIn("Shared.dll", str(caught.exception))

    def test_apply_import_installs_nothing_on_a_library_conflict(self):
        """apply_import is safe on its own: a conflict raises before any install,
        never after the mods landed but before the disable pass."""
        self._publish_conflicting_pair()
        plan = mm.ImportPlan(
            roots=[mm._fetch_manifest([self.BASE], "one.m", "1.0.0"),
                   mm._fetch_manifest([self.BASE], "two.m", "1.0.0")],
            dependencies=[], unresolved=[], hinted_repos=[], untracked=[],
            target="client_windows", no_build=[])

        with self.assertRaises(mm.ModManagerError):
            mm.apply_import(self.game, plan, self.progress.append)

        self.assertEqual(mm.list_installed_mods(self.game), [])
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "one.m")))

    def test_apply_import_installs_roots_and_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE], target="client_windows")

        mm.apply_import(self.game, plan, self.progress.append)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "root.m", ".client_windows", "root.m.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dep.k", ".client_windows", "dep.k.dll")))

    def test_apply_import_disables_installed_mods_not_in_list(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        self._publish_latest("keep.m", "1.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["keep.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["keep.m"])
        self.assertFalse(installed["drop.m"])

    def test_apply_import_does_not_disable_pulled_in_dependencies(self):
        self._install("dep.k", "1.0.0")
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE], target="client_windows")

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["dep.k"])

    def test_apply_import_leaves_already_disabled_mods_alone(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        modlist = {"schema": 1, "repos": [], "mods": []}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertFalse(installed["dis.m"])

    def test_modlist_import_disables_previews_without_side_effects(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        self._publish_latest("keep.m", "1.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["keep.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")

        self.assertEqual(mm.modlist_import_disables(self.game, plan), ["drop.m"])
        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["drop.m"])

    def test_apply_import_blocking_plan_raises_without_installing(self):
        modlist = {"schema": 1, "repos": [], "mods": ["ghost.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], target="client_windows")
        with self.assertRaises(mm.ModManagerError):
            mm.apply_import(self.game, plan, noop)
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods")))

    def test_export_then_import_round_trip(self):
        self._install("rt.m", "1.0.0")
        exported = mm.export_modlist(self.game, {}, "client_windows")
        # Re-serve rt.m at latest.json so the bare-id export entry resolves
        # on import, same as a real repo would.
        self.manifests[f"{self.BASE}/manifests/rt/m/latest.json"] = self.manifests[
            f"{self.BASE}/manifests/rt/m/1.0.0.json"]
        plan = mm.import_modlist(exported, [], [self.BASE], target="client_windows")
        self.assertFalse(plan.blocking)
        self.assertEqual([m.id for m in plan.roots], ["rt.m"])


class ZipBundleInstall(_FakeInstallFixture, unittest.TestCase):
    """package="zip" mods: archive is downloaded, verified, and extracted into
    Mods/<id>/. Zip-slip / symlink / absolute-path members are rejected."""

    def setUp(self):
        super().setUp()
        self.zip_bytes = {}          # download_url -> raw zip bytes

        # The fixture's downloader derives bytes from the URL; a zip install
        # needs the exact archive bytes _publish_zip registered for it.
        def fake_download(url, dest, on_progress=None):
            with open(dest, "wb") as f:
                f.write(self.zip_bytes[url])
            if on_progress:
                on_progress("downloaded")
        stub(self, install, "_download_with_progress", fake_download)

    def _make_zip(self, entries):
        """entries: (arcname, data) or (arcname, data, external_attr)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for entry in entries:
                if len(entry) == 3:
                    arc, data, attr = entry
                    zi = zipfile.ZipInfo(arc)
                    zi.external_attr = attr
                    zf.writestr(zi, data)
                else:
                    arc, data = entry
                    zf.writestr(arc, data)
        return buf.getvalue()

    def _publish_zip(self, mod_id, version, entries, **over):
        author, repo = mod_id.split(".", 1)
        url = f"https://github.com/{author}/{repo}/releases/download/v{version}/{repo}.zip"
        raw = self._make_zip(entries)
        self.zip_bytes[url] = raw
        d = manifest_dict(mod_id, version, package="zip", download_url=url, **over)
        d["artifacts"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        self.manifests[f"{self.BASE}/manifests/{author}/{repo}/{version}.json"] = d
        return d

    def _install(self, mod_id):
        self._closure(mod_id)

    def test_zip_bundle_extracts_into_mod_folder(self):
        self._publish_zip("bundle.m", "1.0.0", [
            ("Bundle.dll", b"MZfake"),
            ("Content/data.bundle", b"assetbundle-bytes"),
        ])
        self._install("bundle.m")
        self.assertTrue(os.path.isfile(self._mod_file("bundle.m", "Bundle.dll")))
        self.assertTrue(os.path.isfile(self._mod_file("bundle.m", "Content", "data.bundle")))
        rec = self._record("bundle.m")
        self.assertEqual(rec["artifacts"][0]["package"], "zip")
        self.assertNotIn("filename", rec)

    def _loose(self, name, data=b"MZloose"):
        os.makedirs(os.path.join(self.game, "Mods"), exist_ok=True)
        with open(os.path.join(self.game, "Mods", name), "wb") as f:
            f.write(data)

    def test_a_loose_copy_of_an_installed_dll_is_disabled(self):
        """Upstream 1.8.5 installs TavernEssentials as a loose
        Mods/TavernEssentials.dll. Installing it through the manager as well
        would have MelonLoader load it twice, so the loose copy goes."""
        self._loose("TavernEssentials.dll")
        self._loose("Unrelated.dll")
        self._publish_zip("Nyx12012.Tavern-Essentials", "1.0.0",
                          [("TavernEssentials.dll", b"MZfake")])
        self._install("Nyx12012.Tavern-Essentials")

        mods_dir = os.path.join(self.game, "Mods")
        self.assertFalse(os.path.exists(os.path.join(mods_dir, "TavernEssentials.dll")))
        self.assertTrue(os.path.isfile(os.path.join(mods_dir, "TavernEssentials.dll.disabled")))
        # Disabled, not deleted, and still listed so it can be turned back on.
        untracked = {u["name"]: u["enabled"] for u in mm.list_untracked_mods(self.game)}
        self.assertEqual(untracked, {"TavernEssentials.dll": False, "Unrelated.dll": True})

    def test_a_loose_copy_that_cant_be_disabled_doesnt_fail_the_install(self):
        """A .disabled of the same name is someone else's file and is never
        clobbered; the install it follows has already succeeded."""
        self._loose("TavernEssentials.dll")
        self._loose("TavernEssentials.dll.disabled", b"MZolder")
        self._publish_zip("Nyx12012.Tavern-Essentials", "1.0.0",
                          [("TavernEssentials.dll", b"MZfake")])
        self._install("Nyx12012.Tavern-Essentials")

        mods_dir = os.path.join(self.game, "Mods")
        self.assertTrue(os.path.isfile(
            self._mod_file("Nyx12012.Tavern-Essentials", "TavernEssentials.dll")))
        with open(os.path.join(mods_dir, "TavernEssentials.dll.disabled"), "rb") as f:
            self.assertEqual(f.read(), b"MZolder")

    def test_zip_shipping_its_own_manifest_json_is_not_left_damaged(self):
        """The record is written under that name after hashing, so extracting the
        archive's copy would leave the mod permanently 'damaged'."""
        self._publish_zip("ships.m", "1.0.0", [
            ("Ships.dll", b"MZfake"),
            ("manifest.json", b'{"not": "ours"}'),
            ("Content/manifest.json", b'{"nested": "fine"}'),
        ])
        self._install("ships.m")
        rec = self._record("ships.m")
        self.assertEqual(rec["id"], "ships.m")
        self.assertNotIn("manifest.json", rec["installed"]["client_windows"]["files"])
        self.assertIn("Content/manifest.json", rec["installed"]["client_windows"]["files"])
        self.assertTrue(mm.verify_mod_files(self.game, "ships.m", "client_windows"))

    def test_zip_record_name_skip_ignores_case_and_dot_segments(self):
        """NTFS resolves all of these to the record's own file."""
        for i, alias in enumerate(("Manifest.json", "./manifest.json",
                                   "Content/../MANIFEST.DISABLED.JSON")):
            with self.subTest(alias=alias):
                mod_id = f"alias{i}.m"
                self._publish_zip(mod_id, "1.0.0", [("A.dll", b"MZfake"),
                                                    (alias, b'{"not": "ours"}')])
                self._install(mod_id)
                rec = self._record(mod_id)
                self.assertEqual(rec["id"], mod_id)
                self.assertEqual(set(rec["installed"]["client_windows"]["files"]), {"A.dll"})
                self.assertTrue(mm.verify_mod_files(self.game, mod_id, "client_windows"))

    def test_unsafe_members_are_refused_and_nothing_is_installed(self):
        """Traversal, absolute paths, colons (a drive letter, or an NTFS
        alternate data stream the realpath check reads as inside the folder)
        and symlinks. Every refusal leaves no folder, staging or escapee."""
        cases = {
            "slip.m": [("ok.dll", b"fine"), ("../escape.dll", b"pwned")],
            "abs.m": [("/tmp/evil.dll", b"x")],
            "ads.m": [("ok.dll:payload", b"hidden")],
            "drv.m": [("C:evil.dll", b"x")],
            "sym.m": [("link", b"/etc/passwd", 0o120777 << 16)],
        }
        mods = os.path.join(self.game, "Mods")
        for mod_id, entries in cases.items():
            with self.subTest(mod_id=mod_id):
                self._publish_zip(mod_id, "1.0.0", entries)
                with self.assertRaises(mm.ModManagerError):
                    self._install(mod_id)
                self.assertFalse(os.path.exists(os.path.join(mods, mod_id)))
                self.assertFalse(os.path.exists(os.path.join(mods, f".{mod_id}.installing")))
        self.assertFalse(os.path.exists(os.path.join(mods, "escape.dll")))

    def test_zip_checksum_mismatch_aborts(self):
        d = self._publish_zip("badz.m", "1.0.0", [("a.dll", b"x")])
        d["artifacts"][0]["sha256"] = "f" * 64
        with self.assertRaises(mm.ModManagerError):
            self._install("badz.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "badz.m")))

    def test_an_artifact_of_unknown_package_type_is_ignored(self):
        with contextlib.redirect_stderr(io.StringIO()):
            m = make_manifest("a.b", "1.0.0", package="rar")
        self.assertEqual(m.artifacts, [])
        self.assertIsNone(m.artifact_for("client_windows"))

    def test_zip_bomb_total_size_aborts(self):
        # One member whose decompressed size exceeds the cap.
        stub(self, install, "_ZIP_MAX_TOTAL_UNCOMPRESSED", 4 * 1024 * 1024)
        self._publish_zip("bomb.m", "1.0.0", [
            ("mod.dll", b"MZ"),
            ("big.bin", b"\0" * (8 * 1024 * 1024)),   # 8 MB of zeros, > 4 MB cap
        ])
        with self.assertRaises(mm.ModManagerError):
            self._install("bomb.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "bomb.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".bomb.m.installing")))

    def test_zip_too_many_entries_aborts(self):
        stub(self, install, "_ZIP_MAX_ENTRIES", 5)
        self._publish_zip("many.m", "1.0.0",
                          [(f"f{i}.dat", b"x") for i in range(10)])
        with self.assertRaises(mm.ModManagerError):
            self._install("many.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "many.m")))


class DamageDetection(_FakeInstallFixture, unittest.TestCase):
    """verify_mod_files and the 'damaged' status: the record's per-file hashes,
    checked before anything about versions."""

    def _dll(self, mod_id):
        return self._mod_file(mod_id, f"{mod_id}.dll")

    def test_install_records_per_file_hashes(self):
        self._install("h.m", "1.0.0")
        rec = self._record("h.m")
        # Exactly the placed dll, hashed for real; the record itself is never
        # in its own map (it isn't in the target folder at all).
        with open(self._dll("h.m"), "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(rec["installed"]["client_windows"]["files"], {"h.m.dll": expected})

    def test_intact_install_verifies_true(self):
        self._install("ok.m", "1.0.0")
        self.assertIs(mm.verify_mod_files(self.game, "ok.m", "client_windows"), True)
        self.assertEqual(mm.mod_status(self.game, "ok.m",
                                       [summary("ok.m", ["1.0.0"])], "client_windows"), "current")

    def test_an_altered_file_reads_damaged(self):
        self._install("av.m", "1.0.0")
        with open(self._dll("av.m"), "wb") as f:
            f.write(b"eaten by antivirus")
        self.assertIs(mm.verify_mod_files(self.game, "av.m", "client_windows"), False)
        self.assertEqual(mm.mod_status(self.game, "av.m",
                                       [summary("av.m", ["1.0.0"])], "client_windows"), "damaged")

    def test_a_deleted_file_reads_damaged(self):
        self._install("gone.m", "1.0.0")
        os.remove(self._dll("gone.m"))
        self.assertIs(mm.verify_mod_files(self.game, "gone.m", "client_windows"), False)

    def test_damage_outranks_outdated(self):
        """A damaged install's version number is a claim about files that are
        no longer there, so it must not soften to 'Update ready'."""
        self._install("d.old", "1.0.0")
        with open(self._dll("d.old"), "wb") as f:
            f.write(b"junk")
        self.assertEqual(mm.mod_status(self.game, "d.old",
                                       [summary("d.old", ["1.0.0", "2.0.0"])], "client_windows"), "damaged")

    def test_a_record_without_a_files_map_is_never_damaged(self):
        """Pre-existing installs, from before the field, have no map: no
        evidence either way must read as None/current, or shipping this would
        mark every existing install Damaged overnight."""
        self._install("legacy.m", "1.0.0")
        record_path = mm._mod_record_path(self.game, "legacy.m")
        rec = read_json(record_path)
        del rec["installed"]["client_windows"]["files"]
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        with open(self._dll("legacy.m"), "wb") as f:
            f.write(b"altered, but unprovable")
        self.assertIsNone(mm.verify_mod_files(self.game, "legacy.m", "client_windows"))
        self.assertEqual(mm.mod_status(self.game, "legacy.m",
                                       [summary("legacy.m", ["1.0.0"])], "client_windows"), "current")

    def test_verification_is_memoized_until_invalidated(self):
        """Damage after a clean check is only seen once the memo is invalidated
        (an install of the mod, or Refresh clearing it, as here)."""
        self._install("memo.m", "1.0.0")
        self.assertIs(mm.verify_mod_files(self.game, "memo.m", "client_windows"), True)
        with open(self._dll("memo.m"), "wb") as f:
            f.write(b"corrupted after the check")
        self.assertIs(mm.verify_mod_files(self.game, "memo.m", "client_windows"), True)   # stale, by design
        mm._verify_memo.clear()
        self.assertIs(mm.verify_mod_files(self.game, "memo.m", "client_windows"), False)

    def test_reinstall_invalidates_the_memo_and_repairs(self):
        self._install("fix.m", "1.0.0")
        with open(self._dll("fix.m"), "wb") as f:
            f.write(b"junk")
        mm._verify_memo.clear()
        self.assertIs(mm.verify_mod_files(self.game, "fix.m", "client_windows"), False)
        self._install("fix.m", "1.0.0")    # reinstall over it
        self.assertIs(mm.verify_mod_files(self.game, "fix.m", "client_windows"), True)

    def test_handshake_entries_carry_the_artifact_shas(self):
        """Additive advisory fields; the fingerprint hash itself must not move
        (it's byte-identical with TavernLib's C#)."""
        self._install("s.m", "1.0.0")
        mods_hash, _, entries, _ = mm.handshake_snapshot(self.game, "client_windows")
        rec = self._record("s.m")
        self.assertEqual(entries[0]["target_sha256"],
                         {"client_windows": rec["installed"]["client_windows"]["sha256"],
                          "server": rec["installed"]["client_windows"]["sha256"]})
        self.assertEqual(mods_hash,
                         hashlib.sha256(b"s.m@1.0.0@req").hexdigest())


class CacheIntegrityAndPruning(_TempDataDirFixture, unittest.TestCase):
    """cache_restore_mod's restore-time verification and cache_store_mod's
    version pruning."""

    def test_restore_verifies_and_discards_a_rotten_entry(self):
        self._install("rot.m", "1.0.0")
        mm.cache_store_mod(self.game, "rot.m", "client_windows")
        entry = self._cache_entry("rot.m", "1.0.0")
        with open(os.path.join(entry, "tree", "rot.m.dll"), "wb") as f:
            f.write(b"bitrot")

        with self.assertRaises(mm.ModManagerError) as ctx:
            self._restore("rot.m", "1.0.0")
        self.assertIn("rot.m", str(ctx.exception))
        # The bad entry is gone (not poised to fail every future restore), and
        # the installed copy was never touched.
        self.assertFalse(os.path.isdir(entry))
        self.assertIs(mm.verify_mod_files(self.game, "rot.m", "client_windows"), True)

    def test_render_falls_back_to_download_when_the_cache_entry_is_bad(self):
        self._install("fb.m", "1.0.0")
        mm.cache_store_mod(self.game, "fb.m", "client_windows")
        mm.uninstall_mod(self.game, "fb.m")
        entry = self._cache_entry("fb.m", "1.0.0")
        with open(os.path.join(entry, "tree", "fb.m.dll"), "wb") as f:
            f.write(b"bitrot")

        server_mods = [_srv("fb.m", "1.0.0")]
        plan = self._plan(server_mods, [summary("fb.m", ["1.0.0"])])
        entry_plan = next(e for e in plan.entries if e.mod_id == "fb.m")
        self.assertTrue(entry_plan.cached)     # planned as a file move...
        mm.render_active_set(self.game, plan, self.progress.append, noop)
        # ...but rendered as a verified download once the entry failed its
        # restore-time check, leaving a correct install either way.
        self.assertIs(mm.verify_mod_files(self.game, "fb.m", "client_windows"), True)
        self.assertTrue(any("downloading instead" in m.lower() for m in self.progress))

    def test_an_entry_without_a_files_map_still_restores(self):
        """A cache entry stored from a pre-'files' install has nothing to
        check against; restoring it unchecked mirrors verify_mod_files'
        None."""
        self._install("old.m", "1.0.0")
        record_path = mm._mod_record_path(self.game, "old.m")
        rec = read_json(record_path)
        del rec["installed"]["client_windows"]["files"]
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        mm.cache_store_mod(self.game, "old.m", "client_windows")
        mm.uninstall_mod(self.game, "old.m")
        self._restore("old.m", "1.0.0")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "old.m", ".client_windows", "old.m.dll")))

    def test_store_prunes_oldest_stored_versions_past_the_cap(self):
        """By when each entry was stored, not by the installed folder's mtime
        that copytree carries over: here the first-stored install is stamped
        newest on disk, and it is still the one pruned."""
        base = os.path.join(mm._cache_base(), "pr.m")
        for i, version in enumerate(["1.0.0", "1.1.0", "1.2.0", "1.3.0"]):
            self._install("pr.m", version)
            os.utime(self._mod_file("pr.m"), (4000 - i, 4000 - i))
            mm.cache_store_mod(self.game, "pr.m", "client_windows")

        kept = sorted(d for d in os.listdir(base) if not d.endswith(".caching"))
        self.assertEqual(kept, ["1.1.0", "1.2.0", "1.3.0"])

    def test_restoring_an_already_cached_version_does_not_prune(self):
        """A re-store of a version already in the cache returns early - the
        set must come through untouched, at or under the cap or not."""
        base = os.path.join(mm._cache_base(), "pin.m")
        for version in ["1.0.0", "1.1.0", "1.2.0"]:
            self._install("pin.m", version)
            mm.cache_store_mod(self.game, "pin.m", "client_windows")
        self._install("pin.m", "1.0.0")
        mm.cache_store_mod(self.game, "pin.m", "client_windows")    # early return: already cached
        kept = sorted(d for d in os.listdir(base) if not d.endswith(".caching"))
        self.assertEqual(kept, ["1.0.0", "1.1.0", "1.2.0"])


class EnsureLibraries(_TempDataDirFixture, unittest.TestCase):
    """ensure_mod_libraries: restores what a join's prune removed when the
    manager window re-enables a mod."""

    def _install_with_lib(self, mod_id, version):
        self._install(mod_id, version, library_dependencies=[lib_dep("NeededLib")])

    def _lib_path(self):
        return os.path.join(self.game, "UserLibs", "NeededLib.dll")

    def test_restores_a_pruned_library_from_cache(self):
        self._install_with_lib("lib.m", "1.0.0")
        mm.cache_store_library(self.game, "NeededLib.dll")
        os.remove(self._lib_path())
        os.remove(self._lib_path() + ".meta.json")

        restored = mm.ensure_mod_libraries(self.game, "lib.m", self.progress.append)
        self.assertEqual(restored, ["NeededLib.dll"])
        self.assertTrue(os.path.isfile(self._lib_path()))
        self.assertTrue(any("cached" in m for m in self.progress))

    def test_downloads_when_not_cached(self):
        self._install_with_lib("lib.dl", "1.0.0")
        os.remove(self._lib_path())
        os.remove(self._lib_path() + ".meta.json")

        restored = mm.ensure_mod_libraries(self.game, "lib.dl", self.progress.append)
        self.assertEqual(restored, ["NeededLib.dll"])
        self.assertTrue(os.path.isfile(self._lib_path()))
        # And it lands in the cache, so the next prune/enable cycle is a move.
        self.assertTrue(os.path.isdir(mm._cache_library_dir(
            "NeededLib.dll", self._lib_sha("NeededLib"))))

    def test_a_library_whose_bytes_changed_is_reinstalled(self):
        """The file is hashed, not just its sidecar read."""
        self._install_with_lib("lib.av", "1.0.0")
        with open(self._lib_path(), "wb") as f:
            f.write(b"eaten by antivirus")
        self.assertEqual(mm.ensure_mod_libraries(self.game, "lib.av", noop), ["NeededLib.dll"])
        self.assertEqual(self._file_sha(self._lib_path()), self._lib_sha("NeededLib"))

    def test_a_rotten_cached_library_is_discarded_and_downloaded(self):
        self._install_with_lib("lib.rot", "1.0.0")
        sha = self._lib_sha("NeededLib")
        mm.cache_store_library(self.game, "NeededLib.dll")
        cached = mm._cache_library_dir("NeededLib.dll", sha)
        with open(os.path.join(cached, "NeededLib.dll"), "wb") as f:
            f.write(b"bitrot")
        os.remove(self._lib_path())

        self.assertEqual(mm.ensure_mod_libraries(self.game, "lib.rot", self.progress.append),
                         ["NeededLib.dll"])
        self.assertEqual(self._file_sha(self._lib_path()), sha)
        self.assertTrue(any("downloading instead" in m for m in self.progress))
        # Re-cached from the fresh download, not the rotten bytes.
        self.assertEqual(self._file_sha(os.path.join(cached, "NeededLib.dll")), sha)

    @staticmethod
    def _file_sha(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_noop_when_everything_is_in_place(self):
        self._install_with_lib("lib.ok", "1.0.0")
        self.assertEqual(mm.ensure_mod_libraries(self.game, "lib.ok", noop), [])

    def test_noop_for_a_mod_with_no_libraries(self):
        self._install("nolib.m", "1.0.0")
        self.assertEqual(mm.ensure_mod_libraries(self.game, "nolib.m", noop), [])

    def test_noop_for_a_mod_that_is_not_installed(self):
        self.assertEqual(mm.ensure_mod_libraries(self.game, "ghost.m", noop), [])


class Scenes(_TempDataDirFixture, unittest.TestCase):
    """The optional `scenes` field: travels on manifests, records and the
    handshake list, but never into mods_hash."""

    # parsing

    def test_absent_means_every_scene(self):
        self.assertIsNone(make_manifest("a.a", "1.0.0").scenes)

    def test_a_list_is_kept_in_order_without_duplicates(self):
        m = make_manifest("a.a", "1.0.0", scenes=["quest", "normal", "quest"])
        self.assertEqual(m.scenes, ["quest", "normal"])

    def test_an_unknown_scene_name_is_kept_not_rejected(self):
        """A scene added later mustn't make this build refuse the manifest."""
        self.assertEqual(make_manifest("a.a", "1.0.0", scenes=["arena"]).scenes, ["arena"])

    def test_malformed_values_are_refused(self):
        for bad in ([], "quest", ["quest", 1], [""], {"quest": True}):
            with self.subTest(bad=bad), self.assertRaises(mm.ModManagerError):
                make_manifest("a.a", "1.0.0", scenes=bad)

    def test_index_summary_carries_scenes(self):
        s = mm._summary_from_dict(dict(slim_entry(["1.0.0"]), scenes=["normal"]), "a.a", DEFAULT)
        self.assertEqual(s.scenes, ["normal"])

    def test_a_bad_index_value_costs_the_tag_not_the_mod(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            s = mm._summary_from_dict(dict(slim_entry(["1.0.0"]), scenes=[]), "a.a", DEFAULT)
        self.assertIsNotNone(s)
        self.assertIsNone(s.scenes)

    def test_runs_in_scene(self):
        self.assertTrue(mm.runs_in_scene(None, "quest"))
        self.assertTrue(mm.runs_in_scene(["normal"], None))
        self.assertTrue(mm.runs_in_scene(["normal", "quest"], "quest"))
        self.assertFalse(mm.runs_in_scene(["normal"], "tutorial"))

    def test_scene_from_launch_arg(self):
        self.assertEqual(mm.scene_from_arg("/tutorialScene"), "tutorial")
        self.assertEqual(mm.scene_from_arg("/questScene"), "quest")
        self.assertEqual(mm.scene_from_arg(None), "normal")
        self.assertEqual(mm.scene_from_arg(False), "normal")   # authenticate's failure value

    # record + handshake

    def test_the_record_keeps_scenes_only_when_set(self):
        self._install("set.m", scenes=["normal", "tutorial"])
        self._install("unset.m")
        self.assertEqual(self._record("set.m")["scenes"], ["normal", "tutorial"])
        self.assertNotIn("scenes", self._record("unset.m"))

    def test_the_handshake_reports_scenes_but_does_not_hash_them(self):
        self._install("m.m")
        before, _, entries, _ = mm.handshake_snapshot(self.game, "client_windows")
        self.assertNotIn("scenes", entries[0])
        record_path = mm._mod_record_path(self.game, "m.m")
        record = read_json(record_path)
        record["scenes"] = ["quest"]
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        after, _, entries, _ = mm.handshake_snapshot(self.game, "client_windows")
        self.assertEqual(entries[0]["scenes"], ["quest"])
        self.assertEqual(before, after)
        self.assertEqual(after, hashlib.sha256(b"m.m@1.0.0@req").hexdigest())

    # planning

    def _plan_with_pin(self, scenes, scene, installed=False):
        self._publish("pin.m", "1.0.0", **({"scenes": scenes} if scenes else {}))
        if installed:
            self._closure("pin.m", "1.0.0")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return self._plan([], [summary("pin.m", ["1.0.0"])],
                                pinned=["pin.m"], scene=scene)

    def test_a_pin_for_another_scene_is_left_out_of_the_join(self):
        plan = self._plan_with_pin(["normal"], "quest")
        self.assertEqual(plan.entries, [])
        self.assertFalse(plan.blocking)

    def test_an_enabled_pin_for_another_scene_is_offered_as_a_deactivation(self):
        plan = self._plan_with_pin(["normal"], "quest", installed=True)
        self.assertEqual(plan.to_deactivate, ["pin.m"])

    def test_a_pin_for_this_scene_or_any_scene_stays(self):
        for scenes in (["quest"], None):
            with self.subTest(scenes=scenes):
                self.assertEqual([e.mod_id for e in self._plan_with_pin(scenes, "quest").entries],
                                 ["pin.m"])

    def test_no_known_scene_filters_nothing(self):
        """Sync Mods and headless joins never learn the server's scene."""
        self.assertEqual([e.mod_id for e in self._plan_with_pin(["normal"], None).entries], ["pin.m"])

    def test_a_mod_the_server_runs_is_never_second_guessed(self):
        self._publish("srv.m", "1.0.0", scenes=["normal"])
        plan = self._plan([_srv("srv.m", "1.0.0")], [summary("srv.m", ["1.0.0"])], scene="quest")
        self.assertEqual([e.mod_id for e in plan.entries], ["srv.m"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
