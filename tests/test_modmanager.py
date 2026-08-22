"""
Tests for the community mod manager (tavern_shared/mods/).

repository.json holds slim ModSummary rows; the full manifest for each
version is fetched on demand. Tests stub _get_json (serves repository.json
and per-version manifests by URL) and the download/hash helpers. No network.

Run from TavernLauncher/:  python -m unittest -v   or   python tests/test_modmanager.py
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared.mods import (
    cache, hostcfg, install, layout, manifest, modlist, pins, plan, repos,
    resolve,
)
from tavern_shared.mods import errors as _errors
from tavern_shared.mods import version as _version


class _Facade:
    """Reads any mod-manager name off whichever submodule defines it.

    The mod manager used to be a single module and these tests were written
    against it as `mm.<name>`. Splitting it moved the code but changed none of
    the behaviour covered here, so reads still go through one object rather
    than rewriting ~1900 lines of assertions to chase names across eleven
    modules.

    Writes deliberately raise. Stubbing has to name the module it patches
    (repos._get_json, layout._tavern_data_dir, install._sha256_file, ...);
    assigning to the facade would set a dead attribute here and silently stub
    nothing, which is exactly the failure that would make these tests pass
    while testing the real network.
    """

    _MODULES = (_errors, _version, layout, manifest, repos, resolve,
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

def manifest_dict(mod_id, version, **over):
    author, repo = (mod_id.split(".", 1) + ["repo"])[:2]
    d = {
        "manifest_version": 1,
        "id": mod_id,
        "name": repo,
        "version": version,
        "author": author,
        "description": "",
        "client_side": True,
        "server_side": True,
        "parity_required": True,
        "dependencies": {},
        "library_dependencies": [],
        "download_url": f"https://github.com/{author}/{repo}/releases/download/v{version}/{mod_id}.dll",
        "sha256": "0" * 64,
    }
    d.update(over)
    return d


def make_manifest(mod_id, version, base=DEFAULT, **over):
    return mm._manifest_from_dict(manifest_dict(mod_id, version, **over), base)


def summary(mod_id, versions, client=True, server=True, source=DEFAULT, name=None, description=""):
    return mm.ModSummary(
        id=mod_id, name=name or mod_id.split(".", 1)[-1],
        author=mod_id.split(".", 1)[0], description=description,
        client_side=client, server_side=server,
        versions=list(versions), source_repo=source)


def slim_entry(versions, name="M", author="a", description="", client=True, server=True,
               parity_required=True):
    return {"name": name, "author": author, "description": description,
            "client_side": client, "server_side": server,
            "parity_required": parity_required, "versions": list(versions)}


def slim_index(mods):
    return {"index_version": 1, "mods": mods}


def _srv(mod_id, version, client=True, server=True, required=None,
         source_repo=None):
    """One entry of a server's handshake mods list, as plan_join receives
    them. parity_required is always stated - True unless `required` says
    otherwise - because the absent-field case is its own contract (a raise),
    pinned by test_a_server_entry_without_the_field_fails_the_plan."""
    entry = {"id": mod_id, "version": version,
             "client_side": client, "server_side": server,
             "parity_required": True if required is None else required}
    if source_repo is not None:
        entry["source_repo"] = source_repo
    return entry


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class VersionAndPath(unittest.TestCase):
    def test_numeric_ordering_not_lexical(self):
        self.assertLess(mm._parse_version("1.9.0"), mm._parse_version("1.10.0"))
        self.assertGreater(mm._parse_version("2.0.0"), mm._parse_version("1.99.99"))

    def test_bad_versions_rejected(self):
        for bad in ("1.0", "1.0.0.0", "1.0.x", "v1.0.0", "1.0.0-rc1"):
            with self.assertRaises(mm.ModManagerError):
                mm._parse_version(bad)

    def test_safe_basename_rejects_traversal(self):
        bad = ["..", ".", "", "a/b.dll", "../x.dll", "sub/x.dll",
               "a" + chr(92) + "b.dll"]   # backslash
        for name in bad:
            with self.assertRaises(mm.ModManagerError):
                mm._safe_basename(name)
        self.assertEqual(mm._safe_basename("Fine.dll"), "Fine.dll")

    def test_safe_basename_rejects_colons(self):
        """A drive-relative name and, on NTFS, an alternate data stream:
        "mod.dll:payload" writes a hidden stream hanging off mod.dll rather
        than a file of its own. os.path.basename treats neither as a directory
        part, so the traversal checks above let both through."""
        for name in ("C:evil.dll", "mod.dll:payload", "x:", ":stream"):
            with self.assertRaises(mm.ModManagerError):
                mm._safe_basename(name)


class ManifestParsing(unittest.TestCase):
    def test_unknown_major_is_skipped_not_misparsed(self):
        self.assertIsNone(mm._manifest_from_dict(
            manifest_dict("a.b", "1.0.0", manifest_version=2), DEFAULT))

    def test_install_name_keeps_published_name_and_guards_traversal(self):
        u = "https://github.com/a/b/releases/download/v1"
        # keeps the published asset name
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url=f"{u}/CoolMod-v1.0.0.dll").install_name(),
            "CoolMod-v1.0.0.dll")
        # non-.dll basename falls back to <id>.dll
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url=f"{u}/download").install_name(), "a.b.dll")
        # traversal segments are stripped to a bare basename
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url="https://evil/../../etc/passwd").install_name(),
            "a.b.dll")
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url="https://evil/x/..").install_name(), "a.b.dll")
        self.assertEqual(
            make_manifest("a.b", "1.0.0", download_url=f"{u}/../../../Sneaky.dll").install_name(),
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
        repos._get_json = fake

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
        self._serve({DEFAULT: {"index_version": 2, "mods": {
            "a.b": {"1": slim_entry(["1.0.0"])}}}})
        self.assertEqual(mm.fetch_indexes([DEFAULT], force=True), [])


class RepoSourcesConfig(unittest.TestCase):
    """list_repos/add_repo/remove_repo: user-added sources are stored keyed by
    their 'Author/Repo' shorthand, but that shorthand is only ever produced by
    add_repo's own fetch-and-validate check - never derived from, or accepted
    from, a modlist's `repos` field."""

    def setUp(self):
        def fake(url, timeout=15):
            if url == f"{OTHER}/repository.json":
                return slim_index({})
            raise mm.ModManagerError(f"no fake for {url}")
        repos._get_json = fake

    def test_shorthand_derived_from_url(self):
        self.assertEqual(mm._repo_shorthand(OTHER), "someone/theirmods")
        self.assertEqual(mm._repo_shorthand(mm.DEFAULT_REPO), "ChatonIsHere/CommunityMods")

    def test_source_display_github_uses_shorthand(self):
        self.assertEqual(mm._source_display(OTHER), "someone/theirmods")
        self.assertEqual(mm._source_display(mm.DEFAULT_REPO), "ChatonIsHere/CommunityMods")

    def test_source_display_non_github_uses_full_url(self):
        url = "https://example.com/mods/someone/theirmods"
        self.assertEqual(mm._source_display(url), url)

    def test_add_repo_stores_by_shorthand(self):
        cfg = {}
        mm.add_repo(cfg, OTHER)
        self.assertEqual(cfg[mm.CFG_REPOS_KEY], {"someone/theirmods": OTHER})
        self.assertIn(OTHER, mm.list_repos(cfg))
        self.assertEqual(mm.list_repos_named(cfg)["someone/theirmods"], OTHER)

    def test_add_repo_rejects_duplicate(self):
        cfg = {}
        mm.add_repo(cfg, OTHER)
        with self.assertRaises(mm.ModManagerError):
            mm.add_repo(cfg, OTHER)

    def test_add_repo_rejects_shorthand_collision_with_different_url(self):
        cfg = {mm.CFG_REPOS_KEY: {"someone/theirmods": "https://raw.githubusercontent.com/someone/theirmods/old-branch"}}
        with self.assertRaises(mm.ModManagerError):
            mm.add_repo(cfg, OTHER)

    def test_legacy_list_shape_migrates_on_read(self):
        cfg = {mm.CFG_REPOS_KEY: [OTHER]}
        self.assertIn(OTHER, mm.list_repos(cfg))
        self.assertEqual(mm.list_repos_named(cfg)["someone/theirmods"], OTHER)

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
            mm.remove_repo(cfg, "ChatonIsHere/CommunityMods")

    def test_list_repos_named_never_grants_pull_access(self):
        # A shorthand alone (e.g. lifted from an imported modlist) is not a URL
        # and is never treated as one - list_repos()/fetch_indexes only ever see
        # real URLs from the locally-maintained, add_repo-gated store.
        cfg = {}
        for url in mm.list_repos(cfg):
            self.assertTrue(url.startswith("http://") or url.startswith("https://"))


class PinnedModsConfig(unittest.TestCase):
    """list_pinned/add_pin/remove_pin - the client's always-on pin list."""

    def test_add_pin_stores_bare_id(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver")
        self.assertEqual(mm.list_pinned(cfg), ["Acme.QuestGiver"])

    def test_add_pin_stores_exact_version(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.AdminTools@1.4.0")
        self.assertEqual(mm.list_pinned(cfg), ["Acme.AdminTools@1.4.0"])

    def test_add_pin_replaces_existing_pin_for_same_id(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver@1.0.0")
        mm.add_pin(cfg, "Acme.QuestGiver@2.0.0")
        self.assertEqual(mm.list_pinned(cfg), ["Acme.QuestGiver@2.0.0"])

    def test_add_pin_rejects_empty(self):
        with self.assertRaises(mm.ModManagerError):
            mm.add_pin({}, "")
        with self.assertRaises(mm.ModManagerError):
            mm.add_pin({}, "   ")

    def test_add_pin_rejects_an_id_that_could_never_resolve(self):
        """Caught at the moment it is typed. A pin is unioned into every join,
        so one accepted here fails against every server the user ever visits,
        far from anything that would explain why."""
        for bad in ("QuestGiver", "@1.0.0", "  @1.0.0"):
            with self.assertRaises(mm.ModManagerError, msg=bad):
                mm.add_pin({}, bad)

    def test_add_pin_rejects_an_unusable_version(self):
        for bad in ("Acme.QuestGiver@", "Acme.QuestGiver@1.0",
                    "Acme.QuestGiver@latest", "Acme.QuestGiver@v1.0.0"):
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

    def test_multiple_distinct_pins_coexist(self):
        cfg = {}
        mm.add_pin(cfg, "Acme.QuestGiver")
        mm.add_pin(cfg, "Acme.AdminTools@1.4.0")
        self.assertEqual(sorted(mm.list_pinned(cfg)),
                         ["Acme.AdminTools@1.4.0", "Acme.QuestGiver"])


class Resolution(unittest.TestCase):
    """resolve_dependencies takes summaries and a fetch_manifest callable."""

    def _store(self, *manifests):
        d = {}
        for m in manifests:
            d[(m.id, m.version)] = m
        return lambda mid, ver, src: d[(mid, ver)]

    def test_major_lock_picks_highest_in_major(self):
        index = [summary("dep.k", ["1.1.0", "1.4.0"]), summary("dep.k", ["2.1.0"])]
        fetch = self._store(make_manifest("dep.k", "1.4.0"))
        root = make_manifest("root.m", "1.0.0", dependencies={"dep.k": "1.2.0"})
        got = mm.resolve_dependencies([root], index, fetch, side="client")
        self.assertEqual([m.version for m in got], ["1.4.0"])   # not 2.1.0, not 1.1.0

    def test_diamond_conflict_raises(self):
        index = [summary("dep.k", ["1.0.0"]), summary("dep.k", ["2.0.0"])]
        # a resolves dep.k@1, then b's major-2 requirement conflicts with it
        fetch = self._store(make_manifest("dep.k", "1.0.0"))
        a = make_manifest("a.a", "1.0.0", dependencies={"dep.k": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"dep.k": "2.0.0"})
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([a, b], index, fetch, side="client")

    def test_cycle_raises(self):
        index = [summary("a.a", ["1.0.0"]), summary("b.b", ["1.0.0"])]
        a = make_manifest("a.a", "1.0.0", dependencies={"b.b": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"a.a": "1.0.0"})
        fetch = self._store(a, b)
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([a], index, fetch, side="client")

    def test_missing_dependency_raises(self):
        root = make_manifest("root.m", "1.0.0", dependencies={"nope.x": "1.0.0"})
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([root], [], self._store(), side="client")


class SideFilteredResolution(unittest.TestCase):
    """A dependency that can't run on the side being installed into is dropped,
    along with anything only it needed.

    The motivating case: a mod that runs on both sides, whose client half needs a
    client-only support mod. Installing it on a server used to pull that support
    mod in and crash the server loading it."""

    def _store(self, *manifests):
        d = {}
        for m in manifests:
            d[(m.id, m.version)] = m
        return lambda mid, ver, src: d[(mid, ver)]

    def _tracking_store(self, *manifests):
        """Same, but records which ids were actually fetched - the only way to
        show a pruned subtree is never WALKED, as opposed to walked and then
        filtered out of the result."""
        d = {}
        for m in manifests:
            d[(m.id, m.version)] = m
        fetched = []

        def fetch(mid, ver, src):
            fetched.append(mid)
            return d[(mid, ver)]
        return fetch, fetched

    # A dual-side root whose dependency only runs on clients.
    def _dual_root_with_client_only_dep(self):
        index = [summary("ui.kit", ["1.0.0"], client=True, server=False)]
        dep = make_manifest("ui.kit", "1.0.0", client_side=True, server_side=False)
        root = make_manifest("dual.m", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        return index, dep, root

    def test_client_only_dependency_pruned_on_server(self):
        index, dep, root = self._dual_root_with_client_only_dep()
        got = mm.resolve_dependencies([root], index, self._store(dep), side="server")
        self.assertEqual(got, [])

    def test_same_dependency_kept_on_client(self):
        # Identical inputs to the test above; only the side differs. The pair is
        # the point: one input, two correct answers.
        index, dep, root = self._dual_root_with_client_only_dep()
        got = mm.resolve_dependencies([root], index, self._store(dep), side="client")
        self.assertEqual([m.id for m in got], ["ui.kit"])

    def test_server_only_dependency_pruned_on_client(self):
        index = [summary("db.tools", ["1.0.0"], client=False, server=True)]
        dep = make_manifest("db.tools", "1.0.0", client_side=False, server_side=True)
        root = make_manifest("dual.m", "1.0.0", dependencies={"db.tools": "1.0.0"})
        self.assertEqual(mm.resolve_dependencies([root], index, self._store(dep),
                                                  side="client"), [])
        self.assertEqual([m.id for m in mm.resolve_dependencies(
            [root], index, self._store(dep), side="server")], ["db.tools"])

    def test_dual_side_dependency_kept_on_both(self):
        index = [summary("shared.d", ["1.0.0"])]
        dep = make_manifest("shared.d", "1.0.0")
        root = make_manifest("root.m", "1.0.0", dependencies={"shared.d": "1.0.0"})
        for side in ("client", "server"):
            got = mm.resolve_dependencies([root], index, self._store(dep), side=side)
            self.assertEqual([m.id for m in got], ["shared.d"], side)

    def test_subtree_under_pruned_dependency_is_never_walked(self):
        index = [summary("ui.kit", ["1.0.0"], client=True, server=False),
                 summary("deep.d", ["1.0.0"])]
        dep = make_manifest("ui.kit", "1.0.0", client_side=True, server_side=False,
                            dependencies={"deep.d": "1.0.0"})
        deep = make_manifest("deep.d", "1.0.0")
        root = make_manifest("dual.m", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        fetch, fetched = self._tracking_store(dep, deep)
        self.assertEqual(mm.resolve_dependencies([root], index, fetch, side="server"), [])
        # ui.kit is fetched (its manifest is what decides the side), deep.d never is.
        self.assertEqual(fetched, ["ui.kit"])

    def test_dependency_also_reachable_by_a_valid_path_still_arrives(self):
        """Pruning ui.kit must not take shared.d with it when something else
        needs shared.d in its own right. Pruning is 'drop what only IT needed'."""
        index = [summary("ui.kit", ["1.0.0"], client=True, server=False),
                 summary("shared.d", ["1.0.0"])]
        ui = make_manifest("ui.kit", "1.0.0", client_side=True, server_side=False,
                           dependencies={"shared.d": "1.0.0"})
        shared = make_manifest("shared.d", "1.0.0")
        # a is visited first, so shared.d is first reached THROUGH the pruned dep.
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"shared.d": "1.0.0"})
        got = mm.resolve_dependencies([a, b], index, self._store(ui, shared), side="server")
        self.assertEqual([m.id for m in got], ["shared.d"])

    def test_pruned_dependency_raises_no_diamond_conflict(self):
        """A mod we aren't installing must not be able to fail the whole resolve
        over a version disagreement about itself - the reason its constraints are
        recorded only after the side check passes."""
        index = [summary("ui.kit", ["1.0.0"], client=True, server=False),
                 summary("ui.kit", ["2.0.0"], client=True, server=False)]
        ui1 = make_manifest("ui.kit", "1.0.0", client_side=True, server_side=False)
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"ui.kit": "2.0.0"})
        self.assertEqual(mm.resolve_dependencies([a, b], index, self._store(ui1),
                                                  side="server"), [])
        # The same disagreement is still a hard error on the side that installs it.
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([a, b], index, self._store(ui1), side="client")

    def test_wrong_side_dependency_fetched_only_once(self):
        index = [summary("ui.kit", ["1.0.0"], client=True, server=False)]
        ui = make_manifest("ui.kit", "1.0.0", client_side=True, server_side=False)
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        fetch, fetched = self._tracking_store(ui)
        self.assertEqual(mm.resolve_dependencies([a, b], index, fetch, side="server"), [])
        self.assertEqual(fetched, ["ui.kit"])

    def test_pruning_is_reported_not_silent(self):
        index, dep, root = self._dual_root_with_client_only_dep()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mm.resolve_dependencies([root], index, self._store(dep), side="server")
        msg = err.getvalue()
        self.assertIn("ui.kit", msg)
        self.assertIn("server", msg)

    def test_second_requirer_of_a_pruned_dependency_also_reported(self):
        index = [summary("ui.kit", ["1.0.0"], client=True, server=False)]
        ui = make_manifest("ui.kit", "1.0.0", client_side=True, server_side=False)
        a = make_manifest("a.a", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"ui.kit": "1.0.0"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mm.resolve_dependencies([a, b], index, self._store(ui), side="server")
        # Fetched once (test above), but both requirers still say so.
        self.assertEqual(err.getvalue().count("ui.kit"), 2)

    def test_roots_are_not_side_filtered(self):
        """Only DEPENDENCIES are filtered. A root is what the caller explicitly
        asked for, and list_mods already keeps a wrong-side mod out of the browse
        list it's picked from; silently resolving to nothing here would just hide
        a caller bug."""
        index = [summary("shared.d", ["1.0.0"])]
        dep = make_manifest("shared.d", "1.0.0")
        root = make_manifest("client.only", "1.0.0", client_side=True, server_side=False,
                             dependencies={"shared.d": "1.0.0"})
        got = mm.resolve_dependencies([root], index, self._store(dep), side="server")
        self.assertEqual([m.id for m in got], ["shared.d"])

    def test_side_is_mandatory_and_validated(self):
        root = make_manifest("root.m", "1.0.0")
        for bad in (None, "", "both", "Client", "SERVER", 0):
            with self.assertRaises(mm.ModManagerError, msg=repr(bad)):
                mm.resolve_dependencies([root], [], self._store(), side=bad)


class Libraries(unittest.TestCase):
    def _lib(self, sha):
        return {"name": "ExampleLib", "download_url": "https://x/ExampleLib.dll",
                "sha256": sha, "filename": "ExampleLib.dll"}

    def test_same_library_dedupes(self):
        a = make_manifest("a.a", "1.0.0", library_dependencies=[self._lib("a" * 64)])
        b = make_manifest("b.b", "1.0.0", library_dependencies=[self._lib("a" * 64)])
        self.assertEqual(len(mm.collect_library_dependencies([a, b])), 1)

    def test_conflicting_library_raises(self):
        a = make_manifest("a.a", "1.0.0", library_dependencies=[self._lib("a" * 64)])
        b = make_manifest("b.b", "1.0.0", library_dependencies=[self._lib("b" * 64)])
        with self.assertRaises(mm.ModManagerError):
            mm.collect_library_dependencies([a, b])


class Listing(unittest.TestCase):
    def test_side_filter_and_collapse(self):
        index = [
            summary("a.a", ["1.0.0"], client=True, server=False),
            summary("a.a", ["1.2.0"], client=True, server=False),   # separate major-1 entry, higher
            summary("b.b", ["1.0.0"], client=False, server=True),
        ]
        client = mm.list_mods(index, "client")
        self.assertEqual([(s.id, s.highest()) for s in client], [("a.a", "1.2.0")])
        server = mm.list_mods(index, "server")
        self.assertEqual([s.id for s in server], ["b.b"])


class _FakeInstallFixture:
    """Shared setUp/_publish for tests that need real installs against fake
    download/hash helpers, without inheriting another TestCase's own test_*
    methods. Mix this in alongside unittest.TestCase rather than subclassing
    a concrete test case."""
    BASE = DEFAULT

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="tavern_game_")
        self.progress = []
        self.manifests = {}          # url -> manifest dict served by _get_json

        def fake_get_json(url, timeout=15):
            if url in self.manifests:
                return self.manifests[url]
            raise mm.ModManagerError(f"404 {url}")
        repos._get_json = fake_get_json

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

        install._download_with_progress = fake_download
        install._sha256_file = fake_hash
        self.addCleanup(lambda: __import__("shutil").rmtree(self.game, ignore_errors=True))

    def _sha_for(self, url):
        return hashlib.sha256(("BYTES:" + url).encode()).hexdigest()

    def _publish(self, mod_id, version, **over):
        """Register a per-version manifest at its fetch URL, with a sha256
        matching what fake_download will produce."""
        d = manifest_dict(mod_id, version, **over)
        d["sha256"] = self._sha_for(d["download_url"])
        for lib in d.get("library_dependencies", []):
            lib["sha256"] = self._sha_for(lib["download_url"])
        author, repo = mod_id.split(".", 1)
        url = f"{self.BASE}/manifests/{author}/{repo}/{version}.json"
        self.manifests[url] = d
        return d

    def _install(self, mod_id, version="1.0.0", **over):
        """Publish + closure-install one mod: the installed state most cases
        start from."""
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]),
                               [summary(mod_id, [version])], [self.BASE],
                               self.progress.append, side="client")


class _TempDataDirFixture(_FakeInstallFixture):
    """_FakeInstallFixture plus _tavern_data_dir pointed at a throwaway temp
    dir, for anything that touches the client mod cache - tests must never
    read or write the real %AppData%. Replaced on the layout module because
    cache.py reads it through the module at call time (see the note there)."""

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        saved = layout._tavern_data_dir
        layout._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(layout, "_tavern_data_dir", saved))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))


class InstallEndToEnd(_FakeInstallFixture, unittest.TestCase):
    def test_full_closure_install_lands_files_and_sidecars(self):
        lib_url = "https://x/ExampleLib.dll"
        lib = {"name": "ExampleLib", "download_url": lib_url, "sha256": "", "filename": "ExampleLib.dll"}
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0",
                      dependencies={"dep.k": "1.0.0"},
                      library_dependencies=[dict(lib)])

        index = [summary("dep.k", ["1.0.0"]), summary("root.m", ["1.0.0"])]
        root_summary = summary("root.m", ["1.0.0"])
        mm.install_mod_closure(self.game, root_summary, index, [self.BASE], self.progress.append, side="client")

        # Each mod lands in its own folder, Mods/<id>/, with the dll inside.
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "root.m", "root.m.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dep.k", "dep.k.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "ExampleLib.dll")))

        # Mods/<id>/manifest.json is also MelonLoader's folder marker.
        mod_meta = read_json(os.path.join(self.game, "Mods", "root.m", "manifest.json"))
        self.assertEqual(mod_meta["version"], "1.0.0")
        self.assertEqual(mod_meta["package"], "dll")
        self.assertNotIn("filename", mod_meta)
        self.assertIn("source_repo", mod_meta)
        self.assertIn("client_side", mod_meta)
        self.assertEqual(mod_meta["libraries"], ["ExampleLib.dll"])
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
        mm.install_mod_closure(self.game, summary("v.v", ["1.0.0", "2.0.0"]), index,
                               [self.BASE], self.progress.append, version="1.0.0", side="client")
        mod_meta = read_json(os.path.join(self.game, "Mods", "v.v", "manifest.json"))
        self.assertEqual(mod_meta["version"], "1.0.0")

    def test_checksum_mismatch_aborts_leaving_nothing(self):
        d = self._publish("bad.m", "1.0.0")
        d["sha256"] = "f" * 64        # break it after publishing
        index = [summary("bad.m", ["1.0.0"])]
        with self.assertRaises(mm.ModManagerError):
            mm.install_mod_closure(self.game, summary("bad.m", ["1.0.0"]), index,
                                   [self.BASE], self.progress.append, side="client")
        # No mod folder and no staging left behind.
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "bad.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".bad.m.installing")))

    def test_status_current_then_outdated(self):
        self._publish("s.s", "1.0.0")
        index = [summary("s.s", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("s.s", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")
        self.assertEqual(mm.mod_status(self.game, "s.s", [summary("s.s", ["1.0.0"])]), "current")
        self.assertEqual(mm.mod_status(self.game, "s.s", [summary("s.s", ["1.0.0", "1.1.0"])]), "outdated")

    def test_status_agrees_with_the_listings_about_a_legacy_record(self):
        """A record written before parity_required existed isn't ours by the
        listings' test, so mod_status must not call it installed either - the
        Community Mods window and the Mod Manager table would otherwise
        describe the same folder two different ways."""
        self._publish("legacy.m", "1.0.0")
        index = [summary("legacy.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("legacy.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")

        record = os.path.join(self.game, "Mods", "legacy.m", "manifest.json")
        rec = read_json(record)
        del rec["parity_required"]
        with open(record, "w", encoding="utf-8") as f:
            json.dump(rec, f)

        self.assertEqual(mm.mod_status(self.game, "legacy.m", index), "missing")
        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], [])
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)], ["legacy.m"])

    def test_uninstall_removes_mod_and_its_orphaned_library(self):
        lib = {"name": "OrphanLib", "download_url": "https://x/OrphanLib.dll",
               "sha256": "", "filename": "OrphanLib.dll"}
        self._publish("u.m", "1.0.0", library_dependencies=[dict(lib)])
        index = [summary("u.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("u.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")

        self.assertTrue(mm.uninstall_mod(self.game, "u.m"))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "u.m")))
        self.assertEqual(mm.mod_status(self.game, "u.m", index), "missing")
        # No mod needs the library anymore, so it and its sidecar are gone too.
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "OrphanLib.dll")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "OrphanLib.dll.meta.json")))

    def test_uninstall_keeps_library_another_mod_still_needs(self):
        lib = {"name": "SharedLib", "download_url": "https://x/SharedLib.dll",
               "sha256": "", "filename": "SharedLib.dll"}
        self._publish("a.m", "1.0.0", library_dependencies=[dict(lib)])
        self._publish("b.m", "1.0.0", library_dependencies=[dict(lib)])
        index = [summary("a.m", ["1.0.0"]), summary("b.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("a.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")
        mm.install_mod_closure(self.game, summary("b.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")

        # Removing a leaves the library, since b still needs it.
        mm.uninstall_mod(self.game, "a.m")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "SharedLib.dll")))
        # Removing the last owner drops it.
        mm.uninstall_mod(self.game, "b.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "SharedLib.dll")))

    def test_uninstall_missing_mod_is_noop(self):
        self.assertFalse(mm.uninstall_mod(self.game, "never.installed"))

    def test_disable_enable_round_trips(self):
        self._publish("d.m", "1.0.0")
        index = [summary("d.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("d.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")
        folder = os.path.join(self.game, "Mods", "d.m")
        record = os.path.join(folder, "manifest.json")
        disabled = os.path.join(folder, "manifest.disabled.json")

        # Disable removes the MelonLoader marker (manifest.json); folder stays.
        self.assertTrue(mm.disable_mod(self.game, "d.m"))
        self.assertFalse(os.path.exists(record))
        self.assertTrue(os.path.isfile(disabled))
        self.assertTrue(os.path.isdir(folder))
        # Still installed and version-comparable while disabled.
        self.assertEqual(mm.mod_status(self.game, "d.m", index), "current")
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
        self._publish("n.m", "1.0.0")
        index = [summary("n.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("n.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")
        # Enabling an already-enabled mod, or disabling twice, is a no-op.
        self.assertFalse(mm.enable_mod(self.game, "n.m"))
        self.assertTrue(mm.disable_mod(self.game, "n.m"))
        self.assertFalse(mm.disable_mod(self.game, "n.m"))

    def test_uninstall_removes_disabled_mod_folder(self):
        self._publish("z.m", "1.0.0")
        index = [summary("z.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("z.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")
        self.assertTrue(mm.disable_mod(self.game, "z.m"))
        self.assertTrue(mm.uninstall_mod(self.game, "z.m"))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "z.m")))

    def test_temp_download_lives_on_destination_volume(self):
        # Temp .part files must be created under the game directory, not the
        # system temp dir, or os.replace fails with WinError 17 when the game
        # is on a different drive than TEMP.
        lib_url = "https://x/RegLib.dll"
        lib = {"name": "RegLib", "download_url": lib_url, "sha256": "", "filename": "RegLib.dll"}
        self._publish("t.m", "1.0.0", library_dependencies=[dict(lib)])
        index = [summary("t.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("t.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")
        game_root = os.path.normcase(os.path.abspath(self.game)) + os.sep
        self.assertTrue(self.dl_dirs)
        for d in self.dl_dirs:
            self.assertTrue(os.path.normcase(d).startswith(game_root))


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

    def test_a_loose_root_dll_is_reported_enabled(self):
        self._write("Manual.dll")
        self.assertEqual(self._names(),
                         {"Manual.dll": {"name": "Manual.dll", "kind": "file", "enabled": True}})

    def test_a_disabled_dll_reports_under_its_enabled_name(self):
        """So the same string toggles it either way - the caller never has to
        know about the suffix."""
        self._write("Manual.dll.disabled")
        self.assertEqual(self._names(),
                         {"Manual.dll": {"name": "Manual.dll", "kind": "file", "enabled": False}})

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

    def test_a_non_dll_root_file_is_not_a_mod(self):
        self._write("notes.txt")
        self._write("config.json", "{}")
        self.assertEqual(self._names(), {})

    # -- foreign folders ------------------------------------------------------

    def test_a_folder_with_a_foreign_manifest_is_reported(self):
        self._write("LegacyHUD/manifest.json", json.dumps({"whatever": True}))
        self.assertEqual(self._names(),
                         {"LegacyHUD": {"name": "LegacyHUD", "kind": "folder", "enabled": True}})

    def test_a_folder_whose_manifest_is_junk_is_still_reported(self):
        """MelonLoader's folder marker is an existence check with the content
        unread, so a folder with an unparseable manifest.json loads exactly like
        one with a valid manifest. Listing it on whether the JSON parses would
        hide a mod that is genuinely running."""
        self._write("BrokenHUD/manifest.json", "not json at all {{{")
        self.assertEqual(self._names(),
                         {"BrokenHUD": {"name": "BrokenHUD", "kind": "folder", "enabled": True}})

    def test_a_folder_with_only_a_disabled_marker_is_reported_disabled(self):
        self._write("LegacyHUD/manifest.disabled.json", json.dumps({"whatever": True}))
        self.assertEqual(self._names(),
                         {"LegacyHUD": {"name": "LegacyHUD", "kind": "folder", "enabled": False}})

    def test_an_empty_folder_is_not_reported(self):
        os.makedirs(self._mods("Empty"), exist_ok=True)
        self._write("JustFiles/README.txt")
        self.assertEqual(self._names(), {})

    def test_our_own_staging_and_ignore_folders_are_skipped(self):
        self._write(".p.p.installing/manifest.json", json.dumps({"id": "p.p"}))
        self._write("~backup/manifest.json", json.dumps({"id": "p.p"}))
        self.assertEqual(self._names(), {})

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
    """handshake_snapshot(game_dir) - the ping/pong mod-sync fingerprint.
    Reuses the fake download/hash wiring to actually install mods, then
    checks the snapshot it produces."""

    def test_empty_mods_dir_gives_empty_snapshot(self):
        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 0)
        self.assertEqual(entries, [])
        self.assertEqual(mods_hash, hashlib.sha256(b"").hexdigest())

    def test_installed_mods_appear_sorted_by_id(self):
        self._publish("z.z", "1.0.0")
        self._publish("a.a", "2.1.0")
        mm.install_mod_closure(self.game, summary("z.z", ["1.0.0"]), [summary("z.z", ["1.0.0"])],
                               [self.BASE], self.progress.append, side="client")
        mm.install_mod_closure(self.game, summary("a.a", ["2.1.0"]), [summary("a.a", ["2.1.0"])],
                               [self.BASE], self.progress.append, side="client")

        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 2)
        self.assertEqual([e["id"] for e in entries], ["a.a", "z.z"])
        self.assertEqual(entries[0]["version"], "2.1.0")
        self.assertTrue(entries[0]["client_side"])
        self.assertTrue(entries[0]["server_side"])

        expected = hashlib.sha256(b"a.a@2.1.0@req\nz.z@1.0.0@req").hexdigest()
        self.assertEqual(mods_hash, expected)

    def test_hash_changes_when_only_parity_changes(self):
        """The client caches the whole mod list against this hash, so a server
        flipping a mod between required and recommended - which needs no version
        bump - has to invalidate that cache. Otherwise a client keeps planning
        against the old answer and can skip a mod that became mandatory."""
        self._publish("p.p", "1.0.0")
        mm.install_mod_closure(self.game, summary("p.p", ["1.0.0"]), [summary("p.p", ["1.0.0"])],
                               [self.BASE], self.progress.append, side="client")
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
        self._publish("d.d", "1.0.0")
        mm.install_mod_closure(self.game, summary("d.d", ["1.0.0"]), [summary("d.d", ["1.0.0"])],
                               [self.BASE], self.progress.append, side="client")
        mm.disable_mod(self.game, "d.d")

        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 0)
        self.assertEqual(entries, [])
        self.assertEqual(mods_hash, hashlib.sha256(b"").hexdigest())

    def test_hash_changes_when_a_version_changes(self):
        self._publish("v.v", "1.0.0")
        mm.install_mod_closure(self.game, summary("v.v", ["1.0.0"]), [summary("v.v", ["1.0.0"])],
                               [self.BASE], self.progress.append, side="client")
        before, _, _, _ = mm.handshake_snapshot(self.game)

        self._publish("v.v", "1.1.0")
        mm.install_mod_closure(self.game, summary("v.v", ["1.1.0"]), [summary("v.v", ["1.1.0"])],
                               [self.BASE], self.progress.append, side="client")
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
        self._publish("m.m", "1.0.0")
        mm.install_mod_closure(self.game, summary("m.m", ["1.0.0"]),
                               [summary("m.m", ["1.0.0"])], [self.BASE],
                               self.progress.append, side="client")
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

    def test_the_hash_covers_untracked_mods(self):
        """Otherwise an operator drops a DLL into Mods/ and every client keeps
        reporting the old set until something else happens to move the hash."""
        before, _, _, _ = mm.handshake_snapshot(self.game)
        self._untracked("Manual.dll")
        after, _, _, _ = mm.handshake_snapshot(self.game)
        self.assertNotEqual(before, after)

    def test_the_hash_moves_when_an_untracked_mod_is_toggled(self):
        self._untracked("Manual.dll")
        before, _, _, _ = mm.handshake_snapshot(self.game)
        mm.disable_untracked_dll(self.game, "Manual.dll")
        after, _, _, _ = mm.handshake_snapshot(self.game)
        self.assertNotEqual(before, after)

    def test_the_hash_moves_when_an_untracked_mod_is_rewritten(self):
        """A replaced DLL under the same name is a different mod, and the stamp
        (mtime) is what catches it - the name alone wouldn't."""
        self._untracked("Manual.dll")
        before, _, _, _ = mm.handshake_snapshot(self.game)
        path = os.path.join(self.game, "Mods", "Manual.dll")
        os.utime(path, (1_700_000_000, 1_700_000_000))
        after, _, _, _ = mm.handshake_snapshot(self.game)
        self.assertNotEqual(before, after)

    def test_the_hash_is_stable_when_nothing_changed(self):
        """The whole point of the cache key: an unchanged server must not keep
        invalidating every client's cached list."""
        self._untracked("Manual.dll")
        self.assertEqual(mm.handshake_snapshot(self.game)[0],
                         mm.handshake_snapshot(self.game)[0])

    def test_untracked_fingerprint_lines_are_byte_exact(self):
        """Pins the exact "untracked:name@mtime" line format, same as
        test_installed_mods_appear_sorted_by_id pins the managed lines:
        TavernLib's ModHandshake computes this hash in C# and the two must stay
        byte-identical, or every join against a server running the other side's
        build silently reports a stale mod list."""
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
        mm.cache_store_mod(self.game, "p.p")
        __import__("shutil").rmtree(os.path.join(self.game, "Mods", "p.p"))
        self._foreign("p.p", marker="theirs, mid-join")

        mm.cache_restore_mod(self.game, "p.p", "1.0.0")

        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.p"])
        with io.open(self._displaced("p.p", "payload.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "theirs, mid-join")


class ClientModCache(_TempDataDirFixture, unittest.TestCase):
    """The client mod cache: mod_cache/<id>/<version>/ and the library
    equivalent, keyed by filename+sha256."""

    def test_store_then_restore_round_trip(self):
        self._install("c.m", "1.0.0")
        self.assertEqual(mm.cache_store_mod(self.game, "c.m"), "1.0.0")
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("c.m", "1.0.0")))

        mm.uninstall_mod(self.game, "c.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "c.m")))

        mm.cache_restore_mod(self.game, "c.m", "1.0.0")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "c.m", "c.m.dll")))
        restored = read_json(os.path.join(self.game, "Mods", "c.m", "manifest.json"))
        self.assertEqual(restored["version"], "1.0.0")

    def test_restoring_uncached_version_raises(self):
        with self.assertRaises(mm.ModManagerError):
            mm.cache_restore_mod(self.game, "never.installed", "9.9.9")

    def test_store_is_idempotent(self):
        self._install("i.m", "1.0.0")
        self.assertEqual(mm.cache_store_mod(self.game, "i.m"), "1.0.0")
        # A second call with the cached copy already present is a no-op, not
        # an error (shutil.copytree onto an existing dir would raise).
        self.assertEqual(mm.cache_store_mod(self.game, "i.m"), "1.0.0")

    def test_disabled_mod_still_caches_and_normalizes_record_name(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        self.assertEqual(mm.cache_store_mod(self.game, "dis.m"), "1.0.0")
        cached = mm._cache_mod_dir("dis.m", "1.0.0")
        self.assertTrue(os.path.isfile(os.path.join(cached, "manifest.json")))
        self.assertFalse(os.path.isfile(os.path.join(cached, "manifest.disabled.json")))

    def test_multiple_versions_coexist_in_cache(self):
        self._install("mv.m", "1.0.0")
        mm.cache_store_mod(self.game, "mv.m")
        self._install("mv.m", "2.0.0")
        mm.cache_store_mod(self.game, "mv.m")
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("mv.m", "1.0.0")))
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("mv.m", "2.0.0")))

    def test_library_store_and_restore_round_trip(self):
        lib_url = "https://x/Shared.dll"
        lib = {"name": "Shared", "download_url": lib_url, "sha256": "", "filename": "Shared.dll"}
        self._install("lib.m", "1.0.0", library_dependencies=[dict(lib)])
        sha = self._sha_for(lib_url)

        self.assertEqual(mm.cache_store_library(self.game, "Shared.dll"), sha)
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Shared.dll", sha)))

        os.remove(os.path.join(self.game, "UserLibs", "Shared.dll"))
        os.remove(os.path.join(self.game, "UserLibs", "Shared.dll.meta.json"))

        mm.cache_restore_library(self.game, "Shared.dll", sha)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "Shared.dll")))
        meta = read_json(os.path.join(self.game, "UserLibs", "Shared.dll.meta.json"))
        self.assertEqual(meta["sha256"], sha)

    def test_restoring_library_already_present_with_matching_hash_is_noop(self):
        lib_url = "https://x/AlreadyThere.dll"
        lib = {"name": "AlreadyThere", "download_url": lib_url, "sha256": "", "filename": "AlreadyThere.dll"}
        self._install("lib2.m", "1.0.0", library_dependencies=[dict(lib)])
        sha = self._sha_for(lib_url)
        mm.cache_store_library(self.game, "AlreadyThere.dll")
        # Already present with the right hash - must not raise even though
        # nothing was ever removed from UserLibs/.
        mm.cache_restore_library(self.game, "AlreadyThere.dll", sha)

    def test_adopt_caches_mod_and_its_library_returns_newly_adopted_only(self):
        lib_url = "https://x/Adopted.dll"
        lib = {"name": "Adopted", "download_url": lib_url, "sha256": "", "filename": "Adopted.dll"}
        self._install("ad.m", "1.0.0", library_dependencies=[dict(lib)])
        self._install("ad2.m", "1.0.0")

        adopted = mm.adopt_installed_mods(self.game)
        self.assertEqual(sorted(adopted), ["ad.m", "ad2.m"])
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("ad.m", "1.0.0")))
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("ad2.m", "1.0.0")))
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Adopted.dll", self._sha_for(lib_url))))

        # Re-running adopts nothing new - both mods are already cached.
        self.assertEqual(mm.adopt_installed_mods(self.game), [])


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

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.needs_repo, [])
        entry = self._entry(plan, "req.m")
        self.assertEqual(entry.version, "1.0.0")
        self.assertEqual(entry.reason, "required")
        self.assertFalse(entry.cached)
        self.assertFalse(entry.active)

    def test_server_side_only_mod_not_required(self):
        server_mods = [_srv("srv.m", "1.0.0", client=False)]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.missing, [])

    def test_dependency_pulled_in_with_reason_dependency(self):
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        server_mods = [_srv("root.m", "1.0.0")]
        index = [summary("dep.k", ["1.0.0"]), summary("root.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual({e.mod_id: e.reason for e in plan.entries},
                         {"root.m": "required", "dep.k": "dependency"})

    def test_missing_required_mod_blocks_with_no_hint(self):
        server_mods = [_srv("ghost.m", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.needs_repo, [])

    def test_needs_repo_when_hint_names_unadded_repo_blocks_with_the_hint(self):
        """A required mod resolvable only from an unadded repo blocks the same
        as a missing one - an apply that skips it can only end in the parity
        check failing after the render, so offering Apply was a button that
        could never succeed. The distinction that matters survives in the
        list itself: needs_repo names the exact source to add, missing has
        nothing to suggest."""
        server_mods = [_srv("elsewhere.m", "1.0.0", source_repo=self.OTHER)]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.needs_repo, [("elsewhere.m", "1.0.0", self.OTHER)])

    def test_pinned_mod_with_no_version_resolves_to_index_highest(self):
        self._publish("pin.m", "2.5.0")
        index = [summary("pin.m", ["1.0.0", "2.5.0"])]
        plan = mm.plan_join(self.game, [], index, [self.BASE], pinned=["pin.m"])
        entry = self._entry(plan, "pin.m")
        self.assertEqual(entry.version, "2.5.0")
        self.assertEqual(entry.reason, "pinned")

    def test_pinned_mod_with_exact_version(self):
        self._publish("pin.m", "1.0.0")
        self._publish("pin.m", "2.5.0")
        index = [summary("pin.m", ["1.0.0", "2.5.0"])]
        plan = mm.plan_join(self.game, [], index, [self.BASE], pinned=["pin.m@1.0.0"])
        entry = self._entry(plan, "pin.m")
        self.assertEqual(entry.version, "1.0.0")

    def test_an_unresolvable_pin_does_not_block_the_join(self):
        """A pin is the user's own setting, unioned into every join. Routing a
        broken one into `missing` (as this once did) meant one typo'd or
        unpublished pin blocked EVERY server, reported as though the server had
        demanded it."""
        plan = mm.plan_join(self.game, [], [], [self.BASE], pinned=["ghost.pin"])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.unresolved_pins, [("ghost.pin", "latest")])

    def test_an_unresolvable_exact_pin_does_not_block_either(self):
        """The version-pinned path resolves through a different branch than the
        track-latest one above, so it gets its own case."""
        plan = mm.plan_join(self.game, [], [summary("gone.pin", ["9.9.9"])],
                            [self.BASE], pinned=["gone.pin@9.9.9"])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.unresolved_pins, [("gone.pin", "9.9.9")])

    def test_a_broken_pin_still_lets_a_real_server_mod_resolve(self):
        """The whole point: the join goes ahead, carrying everything the server
        actually asked for."""
        self._publish("req.m", "1.0.0")
        server_mods = [_srv("req.m", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [summary("req.m", ["1.0.0"])],
                            [self.BASE], pinned=["ghost.pin"])
        self.assertFalse(plan.blocking)
        self.assertEqual([e.mod_id for e in plan.entries], ["req.m"])
        self.assertEqual(plan.unresolved_pins, [("ghost.pin", "latest")])

    def test_a_recommended_mod_from_an_unadded_repo_is_not_needs_repo(self):
        """needs_repo means "add this or you can't join". A recommendation is
        never that, and every consumer of the list labels its entries required,
        so putting one there stated the opposite of the truth - and forced the
        comparison window open on every join over a mod the server would have
        let the player in without."""
        server_mods = [_srv("opt.elsewhere", "1.0.0", required=False,
                            source_repo=self.OTHER)]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.needs_repo, [])
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.entries, [])

    def test_a_required_mod_from_an_unadded_repo_is_still_needs_repo(self):
        """The reordering above must not cost the required case its hint."""
        server_mods = [_srv("req.elsewhere", "1.0.0", source_repo=self.OTHER)]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertEqual(plan.needs_repo, [("req.elsewhere", "1.0.0", self.OTHER)])

    def test_pin_conflict_is_reported_for_a_recommended_mod_too(self):
        """The plan installs the server's version either way, so a pin that
        disagrees is overruled. Checking only `required` meant that happened
        with nothing on screen to say so."""
        self._publish("rec.conf", "1.0.0", parity_required=False)
        self._publish("rec.conf", "2.0.0", parity_required=False)
        server_mods = [_srv("rec.conf", "1.0.0", required=False)]
        index = [summary("rec.conf", ["1.0.0"]), summary("rec.conf", ["2.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE],
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
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE],
                            pinned=["rec.conf@2.0.0"], declined=["rec.conf"])
        self.assertEqual(plan.pin_conflicts, [])

    def test_an_unresolvable_required_mod_is_not_also_a_pin_conflict(self):
        """It's already reported as blocking; a second complaint about the
        version it would have been is noise."""
        server_mods = [_srv("ghost.m", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE],
                            pinned=["ghost.m@2.0.0"])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.pin_conflicts, [])

    def test_pin_conflict_when_server_requires_a_different_exact_version(self):
        self._publish("conf.m", "2.0.0")
        server_mods = [_srv("conf.m", "2.0.0")]
        index = [summary("conf.m", ["1.0.0", "2.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE], pinned=["conf.m@1.0.0"])
        self.assertEqual(plan.pin_conflicts, [("conf.m", "1.0.0", "2.0.0")])
        # The server's exact required version wins the single active entry -
        # the pin conflict is surfaced, not silently overridden or duplicated.
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(self._entry(plan, "conf.m").version, "2.0.0")

    def test_active_true_when_already_installed_at_matching_version(self):
        self._install("act.m", "1.0.0")
        server_mods = [_srv("act.m", "1.0.0")]
        index = [summary("act.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        entry = self._entry(plan, "act.m")
        self.assertTrue(entry.active)

    def test_cached_true_when_version_cached_but_not_currently_active(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m")
        mm.uninstall_mod(self.game, "cch.m")
        server_mods = [_srv("cch.m", "1.0.0")]
        index = [summary("cch.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        entry = self._entry(plan, "cch.m")
        self.assertTrue(entry.cached)
        self.assertFalse(entry.active)

    def test_to_deactivate_lists_enabled_mod_no_longer_required(self):
        self._install("old.m", "1.0.0")
        plan = mm.plan_join(self.game, [], [], [self.BASE])
        self.assertEqual(plan.to_deactivate, ["old.m"])

    def test_already_disabled_mod_not_listed_to_deactivate(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        plan = mm.plan_join(self.game, [], [], [self.BASE])
        self.assertEqual(plan.to_deactivate, [])

    def test_libraries_collected_for_active_set(self):
        lib_url = "https://x/PlanLib.dll"
        lib = {"name": "PlanLib", "download_url": lib_url, "sha256": "", "filename": "PlanLib.dll"}
        self._publish("root.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("root.m", "1.0.0")]
        index = [summary("root.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual([l.filename for l in plan.libraries], ["PlanLib.dll"])


class ModDiff(_TempDataDirFixture, unittest.TestCase):
    """build_mod_diff: the client-vs-server comparison the diff window renders.
    Pure, so none of this needs a display."""

    def _diff(self, server_mods, index):
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
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
        mm.cache_store_mod(self.game, "d.cch")
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
        rows, _ = self._diff([_srv("d.srv", "1.0.0", client=False, server=True)], [])
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
        plan = mm.plan_join(self.game, server_mods, [summary("d.add", ["1.0.0"])], [self.BASE])
        order = [r.action for r in mm.build_mod_diff(self.game, server_mods, plan)]
        self.assertEqual(order[0], mm.ACTION_MISSING)
        self.assertLess(order.index(mm.ACTION_INSTALL), order.index(mm.ACTION_DEACTIVATE))

    def test_an_unresolvable_pin_gets_a_visible_but_non_blocking_row(self):
        """Shown, because a pin that silently does nothing is worse than one
        that says why - but not blocking, since no server asked for it. The
        empty server column is what tells the two apart on screen."""
        server_mods = [_srv("d.fine", "1.0.0")]
        self._publish("d.fine", "1.0.0")
        plan = mm.plan_join(self.game, server_mods, [summary("d.fine", ["1.0.0"])],
                            [self.BASE], pinned=["ghost.pin"])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        row = rows["ghost.pin"]
        self.assertEqual(row.action, mm.ACTION_MISSING)
        self.assertEqual(row.reason, "pinned")
        self.assertEqual(row.server_version, "")
        self.assertFalse(row.blocking)
        # No toggle: it isn't a choice, there is simply nothing to install.
        self.assertFalse(row.optional)
        self.assertIn("pinned", row.note)

    def test_a_server_required_missing_mod_still_blocks(self):
        """The counterpart to the row above - same action, opposite verdict,
        told apart only by `reason`."""
        server_mods = [_srv("d.ghost", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}
        self.assertEqual(rows["d.ghost"].action, mm.ACTION_MISSING)
        self.assertTrue(rows["d.ghost"].blocking)

    def test_pin_conflict_annotates_the_row_without_duplicating_it(self):
        self._publish("d.pin", "1.0.0")
        self._publish("d.pin", "2.0.0")
        index = [summary("d.pin", ["1.0.0"]), summary("d.pin", ["2.0.0"])]
        server_mods = [_srv("d.pin", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE], pinned=["d.pin@2.0.0"])
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
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        mm.render_active_set(self.game, plan)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dl.m", "dl.m.dll")))
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("dl.m", "1.0.0")))

    def test_restores_from_cache_without_redownload(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m")
        mm.uninstall_mod(self.game, "cch.m")

        server_mods = [_srv("cch.m", "1.0.0")]
        index = [summary("cch.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan)
        self.assertEqual(len(self.dl_dirs), downloads_before)   # no new download
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "cch.m", "cch.m.dll")))

    def test_adopting_first_preserves_the_version_a_render_replaces(self):
        """The reason the client launcher calls adopt_installed_mods before
        planning (see _build_mod_plan).

        A mod installed straight to Mods/ (Community Mods window, or an older
        launcher with no cache) is unknown to the cache. Rendering a server's
        different required version over it must not be allowed to destroy the
        only copy of what was there, or switching back later is a re-download
        rather than the file move the cache exists to guarantee."""
        self._install("adopt.m", "1.0.0")
        self._publish("adopt.m", "2.0.0")
        self.assertFalse(os.path.isdir(mm._cache_mod_dir("adopt.m", "1.0.0")))

        mm.adopt_installed_mods(self.game)
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("adopt.m", "1.0.0")))

        server_mods = [_srv("adopt.m", "2.0.0")]
        index = [summary("adopt.m", ["1.0.0"]), summary("adopt.m", ["2.0.0"])]
        mm.render_active_set(self.game, mm.plan_join(self.game, server_mods, index, [self.BASE]))

        # 2.0.0 is now what's live, and 1.0.0 survived the swap in the cache.
        self.assertEqual(mm._read_mod_record(self.game, "adopt.m")["version"], "2.0.0")
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("adopt.m", "1.0.0")))

        # Dropping back to 1.0.0 is therefore a file move, not a download.
        back = [_srv("adopt.m", "1.0.0")]
        plan_back = mm.plan_join(self.game, back, index, [self.BASE])
        self.assertTrue(plan_back.entries[0].cached)
        downloads_before = len(self.dl_dirs)
        mm.render_active_set(self.game, plan_back)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertEqual(mm._read_mod_record(self.game, "adopt.m")["version"], "1.0.0")

    def test_already_active_entry_is_left_alone(self):
        self._install("act.m", "1.0.0")
        server_mods = [_srv("act.m", "1.0.0")]
        index = [summary("act.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "act.m", "act.m.dll")))

    def test_deactivated_mod_is_disabled_not_uninstalled(self):
        """Deactivating is a rename, not a delete. The mod stays installed and
        listed, its files stay on disk, and only the record moves so
        MelonLoader stops scanning the folder."""
        self._install("old.m", "1.0.0")
        plan = mm.plan_join(self.game, [], [], [self.BASE])   # nothing required -> deactivate everything

        mm.render_active_set(self.game, plan)
        mod_dir = os.path.join(self.game, "Mods", "old.m")
        self.assertTrue(os.path.isfile(os.path.join(mod_dir, "old.m.dll")))
        self.assertFalse(os.path.isfile(os.path.join(mod_dir, mm.RECORD_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(mod_dir, mm.DISABLED_RECORD_NAME)))

        # Still installed, just not enabled - so it keeps its place in the list.
        listed = {r["id"]: r for r in mm.list_installed_mods(self.game)}
        self.assertIn("old.m", listed)
        self.assertFalse(listed["old.m"]["enabled"])
        # And not reported to a server, which is what makes the parity check pass.
        self.assertEqual(mm.handshake_snapshot(self.game)[2], [])
        # Cached all the same, so a version switch later is still a file move.
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("old.m", "1.0.0")))
        # And a second sync finds nothing left to do, since to_deactivate only
        # counts enabled mods. That's what stops it disabling the same mod forever.
        self.assertEqual(mm.plan_join(self.game, [], [], [self.BASE]).to_deactivate, [])

    def test_reactivating_a_disabled_mod_needs_no_download_or_cache_copy(self):
        """The counterpart: a mod disabled at the version now wanted comes back
        with a rename. Nothing is fetched, and it works with the cache emptied."""
        self._install("back.m", "1.0.0")
        mm.render_active_set(self.game, mm.plan_join(self.game, [], [], [self.BASE]))

        # Wipe the cache to prove the files on disk are what's being reused.
        __import__("shutil").rmtree(mm._cache_mod_dir("back.m", "1.0.0"))
        downloads_before = len(self.dl_dirs)

        server_mods = [_srv("back.m", "1.0.0")]
        index = [summary("back.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual([e.cached for e in plan.entries], [False])

        mm.render_active_set(self.game, plan)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        mod_dir = os.path.join(self.game, "Mods", "back.m")
        self.assertTrue(os.path.isfile(os.path.join(mod_dir, mm.RECORD_NAME)))
        self.assertFalse(os.path.isfile(os.path.join(mod_dir, mm.DISABLED_RECORD_NAME)))

    def test_a_disabled_mod_at_the_wrong_version_still_switches_properly(self):
        """Enabling in place is only right when the version already matches;
        otherwise the wanted version has to come from the cache or the network."""
        self._install("ver.m", "1.0.0")
        self._publish("ver.m", "2.0.0")
        mm.render_active_set(self.game, mm.plan_join(self.game, [], [], [self.BASE]))

        server_mods = [_srv("ver.m", "2.0.0")]
        index = [summary("ver.m", ["1.0.0"]), summary("ver.m", ["2.0.0"])]
        mm.render_active_set(self.game, mm.plan_join(self.game, server_mods, index, [self.BASE]))

        listed = {r["id"]: r for r in mm.list_installed_mods(self.game)}
        self.assertTrue(listed["ver.m"]["enabled"])
        self.assertEqual(listed["ver.m"]["version"], "2.0.0")
        # The version it was disabled at is still cached, so switching back is a move.
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("ver.m", "1.0.0")))

    def test_library_downloaded_then_orphan_pruned_after_deactivation(self):
        lib_url = "https://x/Prune.dll"
        lib = {"name": "Prune", "download_url": lib_url, "sha256": "", "filename": "Prune.dll"}
        self._publish("lib.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("lib.m", "1.0.0")]
        index = [summary("lib.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        mm.render_active_set(self.game, plan)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "Prune.dll")))
        sha = self._sha_for(lib_url)
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Prune.dll", sha)))

        # Now nothing requires lib.m any more - deactivating it should prune
        # the now-orphaned library from UserLibs/ (it stays cached, though).
        plan2 = mm.plan_join(self.game, [], [], [self.BASE])
        mm.render_active_set(self.game, plan2)
        self.assertFalse(os.path.exists(os.path.join(self.game, "UserLibs", "Prune.dll")))
        self.assertTrue(os.path.isdir(mm._cache_library_dir("Prune.dll", sha)))

    def test_library_restored_from_cache_without_redownload(self):
        lib_url = "https://x/Reuse.dll"
        lib = {"name": "Reuse", "download_url": lib_url, "sha256": "", "filename": "Reuse.dll"}
        self._publish("lib2.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("lib2.m", "1.0.0")]
        index = [summary("lib2.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        mm.render_active_set(self.game, plan)

        # Deactivate, then reactivate - the library should come back from
        # cache, not a fresh download.
        plan_off = mm.plan_join(self.game, [], [], [self.BASE])
        mm.render_active_set(self.game, plan_off)
        downloads_before = len(self.dl_dirs)

        plan_on = mm.plan_join(self.game, server_mods, index, [self.BASE])
        mm.render_active_set(self.game, plan_on)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "UserLibs", "Reuse.dll")))

    def test_blocking_plan_raises_and_changes_nothing(self):
        server_mods = [_srv("ghost.m", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertTrue(plan.blocking)
        with self.assertRaises(mm.ModManagerError):
            mm.render_active_set(self.game, plan)
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods")))

    # on_step / render_plan_steps: what ModDiffWindow shows progress from.

    def _stepped(self, plan):
        """Renders and returns the on_step calls in order."""
        steps = []
        mm.render_active_set(self.game, plan,
                             on_step=lambda i, s: steps.append((i, s)))
        return steps

    def test_every_item_reports_a_start_and_a_done(self):
        self._publish("step.a", "1.0.0")
        self._publish("step.b", "1.0.0")
        server_mods = [_srv(i, "1.0.0")
                       for i in ("step.a", "step.b")]
        index = [summary("step.a", ["1.0.0"]), summary("step.b", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        steps = self._stepped(plan)
        self.assertEqual(steps, [("step.a", "start"), ("step.a", "done"),
                                 ("step.b", "start"), ("step.b", "done")])

    def test_deactivation_reports_a_step_too(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        server_mods = [_srv("keep.m", "1.0.0")]
        index = [summary("keep.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        self.assertEqual(plan.to_deactivate, ["drop.m"])
        self.assertEqual(self._stepped(plan),
                         [("drop.m", "start"), ("drop.m", "done")])

    def test_step_count_matches_render_plan_steps_including_libraries(self):
        """The bar is sized from render_plan_steps before anything runs, so the
        two must agree exactly or it stalls short of the end or overshoots."""
        lib = {"name": "Shared", "download_url": "https://x/Shared.dll",
               "sha256": "", "filename": "Shared.dll"}
        self._publish("libbed.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("libbed.m", "1.0.0")]
        index = [summary("libbed.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        self.assertEqual(mm.render_plan_steps(plan), 2)      # the mod + its library
        steps = self._stepped(plan)
        self.assertEqual([s for _, s in steps].count("done"), mm.render_plan_steps(plan))
        self.assertIn(("Shared.dll", "done"), steps)

    def test_library_already_active_still_reports_its_step(self):
        """It's skipped for work, not for counting: a bar sized against
        render_plan_steps would never fill if the skip stayed silent."""
        lib = {"name": "Shared", "download_url": "https://x/Shared.dll",
               "sha256": "", "filename": "Shared.dll"}
        self._install("libbed.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [_srv("libbed.m", "1.0.0")]
        index = [summary("libbed.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        # Nothing to do for the mod itself, so the library is the whole render.
        self.assertEqual([e.active for e in plan.entries], [True])
        self.assertEqual(mm.render_plan_steps(plan), 1)
        self.assertEqual(self._stepped(plan),
                         [("Shared.dll", "start"), ("Shared.dll", "done")])

    def test_rendering_without_a_step_hook_still_works(self):
        self._publish("nohook.m", "1.0.0")
        server_mods = [_srv("nohook.m", "1.0.0")]
        index = [summary("nohook.m", ["1.0.0"])]
        mm.render_active_set(self.game, mm.plan_join(self.game, server_mods, index, [self.BASE]))
        self.assertTrue(os.path.isdir(os.path.join(self.game, "Mods", "nohook.m")))


class Parity(_TempDataDirFixture, unittest.TestCase):
    """parity: whether a server running a mod obliges the client to match it.

    There is no default in either direction: a manifest, record, index entry,
    or server handshake entry that doesn't state parity_required outright is
    rejected or ignored, never guessed at - the field decides whether a
    joining client is obliged to match, and guessing is the one mistake that
    matters here. "optional" means the server won't refuse a join over it, so
    the client is offered the mod and may decline."""

    # -- reading the value ---------------------------------------------------

    def test_absent_parity_is_an_error_not_a_default(self):
        """No default anywhere. Guessing is the one mistake that matters here,
        since this decides whether a joining client is obliged to match."""
        with self.assertRaises(mm.ModManagerError) as caught:
            mm.parity_required({"id": "a.b"})
        self.assertIn("parity_required", str(caught.exception))

    def test_malformed_parity_is_an_error_not_a_coercion(self):
        """Rejected outright rather than read as required. Failing closed would
        be safe on its own, but TavernLib's ModParityField.Read throws on the
        same bytes, so coercing here meant one third-party manifest installed
        via the launcher and was unresolvable on a headless host. 0 and 1 are
        included deliberately: isinstance(1, bool) is False in Python, so an int
        is not quietly accepted as a boolean."""
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
        self.assertEqual(mm.handshake_snapshot(self.game)[2], [])
        # The files are still there, so reinstalling is what fixes it.
        self.assertTrue(os.path.isdir(mm._mod_dir_path(self.game, "p.old")))
        # Not ours by the shared predicate, so the folder demotes to untracked
        # rather than vanishing - it IS still loading. TavernLib reads the same
        # record as null (parity is Required.Always) and reports the same set.
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)],
                         ["p.old"])

    def test_a_record_with_a_malformed_field_is_ignored_too(self):
        """Same treatment as an absent one, and for the same reason: neither can
        say whether a joining client has to match. It also keeps
        handshake_snapshot, which re-reads the field off these same records,
        unable to raise partway through building a fingerprint."""
        self._install("p.junk", "1.0.0")
        self._break_record("p.junk", lambda r: r.update(parity_required="yes"))

        self.assertEqual(mm.list_installed_mods(self.game), [])
        self.assertEqual(mm.handshake_snapshot(self.game)[2], [])
        # Same demotion as an absent field: unusable record, untracked folder.
        self.assertEqual([u["name"] for u in mm.list_untracked_mods(self.game)],
                         ["p.junk"])

    def test_an_index_entry_without_a_usable_field_is_skipped(self):
        """One bad entry is dropped; the rest of the index still loads."""
        d = {"name": "M", "author": "a", "description": "",
             "client_side": True, "server_side": True, "versions": ["1.0.0"]}
        self.assertIsNone(mm._summary_from_dict(d, "no.parity", self.BASE))
        d["parity_required"] = "yes"
        self.assertIsNone(mm._summary_from_dict(d, "no.parity", self.BASE))
        d["parity_required"] = True
        self.assertIsNotNone(mm._summary_from_dict(d, "no.parity", self.BASE))

    def test_a_server_entry_without_the_field_fails_the_plan(self):
        """A server on an older TavernLib can't be planned against, rather than
        having its requirements guessed at."""
        with self.assertRaises(mm.ModManagerError):
            mm.plan_join(self.game,
                         [{"id": "p.x", "version": "1.0.0",
                           "client_side": True, "server_side": True}],
                         [], [self.BASE])

    # -- carrying it through -------------------------------------------------

    def test_manifest_parity_reaches_the_install_record_and_the_handshake(self):
        self._install("p.opt", "1.0.0", parity_required=False)
        self._install("p.req", "1.0.0")

        rec = mm._read_mod_record(self.game, "p.opt")
        self.assertIs(rec["parity_required"], False)

        _, _, mods, _ = mm.handshake_snapshot(self.game)
        by_id = {m["id"]: m for m in mods}
        self.assertIs(by_id["p.opt"]["parity_required"], False)
        self.assertIs(by_id["p.req"]["parity_required"], True)

    # -- planning ------------------------------------------------------------

    def test_optional_mod_is_planned_in_by_default(self):
        """Recommended still means installed unless the user says otherwise."""
        self._publish("p.rec", "1.0.0", parity_required=False)
        plan = mm.plan_join(self.game, [_srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])], [self.BASE])
        self.assertEqual([(e.mod_id, e.reason) for e in plan.entries],
                         [("p.rec", "recommended")])
        self.assertEqual(plan.declined, [])

    def test_declining_leaves_an_optional_mod_out_of_the_plan(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        plan = mm.plan_join(self.game, [_srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])], [self.BASE],
                            declined=["p.rec"])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.declined, [("p.rec", "1.0.0")])

    def test_declining_a_required_mod_is_ignored(self):
        """No client-side choice can make that join work, so pretending it can
        would just move the rejection to the server."""
        self._publish("p.req", "1.0.0")
        plan = mm.plan_join(self.game, [_srv("p.req", "1.0.0", required=True)],
                            [summary("p.req", ["1.0.0"])], [self.BASE],
                            declined=["p.req"])
        self.assertEqual([e.mod_id for e in plan.entries], ["p.req"])
        self.assertEqual(plan.declined, [])

    def test_declining_never_deactivates_something_already_installed(self):
        """Declining is 'don't add it', not 'take it away'."""
        self._install("p.rec", "1.0.0", parity_required=False)
        plan = mm.plan_join(self.game, [_srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])], [self.BASE],
                            declined=["p.rec"])
        self.assertEqual(plan.to_deactivate, [])
        self.assertEqual(plan.entries, [])

    def test_an_unresolvable_optional_mod_does_not_block(self):
        """Required and unresolvable blocks the join; recommended just doesn't
        happen, since the server allows a client not to have it."""
        plan = mm.plan_join(self.game, [_srv("p.gone", "1.0.0", required=False)],
                            [], [self.BASE])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.entries, [])

    def test_an_unresolvable_required_mod_still_blocks(self):
        plan = mm.plan_join(self.game, [_srv("p.gone", "1.0.0", required=True)],
                            [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [("p.gone", "1.0.0")])

    # -- the comparison ------------------------------------------------------

    def test_recommended_rows_are_the_users_call_required_ones_are_not(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        self._publish("p.req", "1.0.0")
        server_mods = [_srv("p.rec", "1.0.0", required=False),
                       _srv("p.req", "1.0.0", required=True)]
        index = [summary("p.rec", ["1.0.0"]), summary("p.req", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        self.assertTrue(rows["p.rec"].optional)
        self.assertTrue(rows["p.rec"].recommended)
        self.assertIn("doesn't require it", rows["p.rec"].note)
        self.assertFalse(rows["p.req"].optional)
        self.assertFalse(rows["p.req"].recommended)

    def test_a_declined_mod_gets_a_row_so_the_choice_can_be_taken_back(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        server_mods = [_srv("p.rec", "1.0.0", required=False)]
        plan = mm.plan_join(self.game, server_mods, [summary("p.rec", ["1.0.0"])],
                            [self.BASE], declined=["p.rec"])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}

        self.assertEqual(rows["p.rec"].action, mm.ACTION_SKIPPED)
        self.assertTrue(rows["p.rec"].optional)
        self.assertTrue(rows["p.rec"].recommended)
        self.assertEqual(rows["p.rec"].server_version, "1.0.0")

    def test_an_already_matching_recommended_mod_is_not_offered_as_a_choice(self):
        """Nothing to decide: it's installed, current, and the server is happy."""
        self._install("p.rec", "1.0.0", parity_required=False)
        server_mods = [_srv("p.rec", "1.0.0", required=False)]
        plan = mm.plan_join(self.game, server_mods, [summary("p.rec", ["1.0.0"])],
                            [self.BASE])
        rows = {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}
        self.assertEqual(rows["p.rec"].action, mm.ACTION_MATCH)
        self.assertFalse(rows["p.rec"].optional)

    # -- remembering the choice ----------------------------------------------

    def _cfg(self):
        """set_declined saves through the host's save_cfg, so wire a recorder in
        its place and hand back the dict it writes to."""
        saved = []
        prev_load, prev_save = hostcfg._load, hostcfg._save
        hostcfg.set_config_accessors(prev_load, saved.append)
        self.addCleanup(lambda: hostcfg.set_config_accessors(prev_load, prev_save))
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

        out = mm.export_modlist(self.game, {})
        self.assertEqual(out["schema"], mm.MODLIST_SCHEMA)
        self.assertEqual(out["mods"], ["en.m"])

    def test_export_pin_versions_writes_exact_versions(self):
        self._install("pin.m", "1.4.0")
        out = mm.export_modlist(self.game, {}, pin_versions=True)
        self.assertEqual(out["mods"], ["pin.m@1.4.0"])

    def test_export_repos_are_informational_shorthands(self):
        cfg = {}
        self.manifests[f"{OTHER}/repository.json"] = slim_index({})
        mm.add_repo(cfg, OTHER)
        out = mm.export_modlist(self.game, cfg)
        self.assertIn("someone/theirmods", out["repos"])
        self.assertIn("ChatonIsHere/CommunityMods", out["repos"])

    def test_import_resolves_bare_id_via_latest(self):
        self._publish_latest("lat.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["lat.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")
        self.assertFalse(plan.blocking)
        self.assertEqual([(m.id, m.version) for m in plan.roots], [("lat.m", "2.0.0")])

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

        out = mm.export_modlist(self.game, {})
        self.assertEqual(out["mods"], ["en.m"])
        self.assertEqual(out["untracked"], [{"name": "Manual.dll", "kind": "file"}])

    def test_export_skips_disabled_untracked_mods(self):
        """Same enabled-only rule as `mods` - the file records the active set,
        not everything sitting on disk."""
        self._untracked("On.dll")
        self._untracked("Off.dll")
        mm.disable_untracked_dll(self.game, "Off.dll")

        out = mm.export_modlist(self.game, {})
        self.assertEqual([u["name"] for u in out["untracked"]], ["On.dll"])

    def test_export_has_the_key_even_with_nothing_untracked(self):
        """Always present, so a reader never has to tell "none" from "an
        exporter too old to say"."""
        self._install("en.m", "1.0.0")
        self.assertEqual(mm.export_modlist(self.game, {})["untracked"], [])

    def test_import_carries_untracked_through_without_resolving_it(self):
        self._publish_latest("lat.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["lat.m"],
                   "untracked": [{"name": "Manual.dll", "kind": "file"}]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")

        self.assertFalse(plan.blocking)
        self.assertEqual(plan.untracked, [{"name": "Manual.dll", "kind": "file"}])
        # Display only: it never becomes something to install, and never counts
        # as unresolved either - there was nothing to resolve in the first place.
        self.assertEqual([m.id for m in plan.roots], ["lat.m"])
        self.assertEqual(plan.unresolved, [])

    def test_an_untracked_block_never_blocks_an_import(self):
        modlist = {"schema": 1, "repos": [], "mods": [],
                   "untracked": [{"name": "Manual.dll", "kind": "file"}]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")
        self.assertFalse(plan.blocking)

    def test_a_modlist_with_no_untracked_key_still_imports(self):
        """Every modlist written before the key existed, and every hand-written
        one, has to keep working."""
        self._publish_latest("lat.m", "2.0.0")
        plan = mm.import_modlist({"schema": 1, "repos": [], "mods": ["lat.m"]},
                                 [], [self.BASE], side="client")
        self.assertEqual(plan.untracked, [])

    def test_importing_never_disables_the_importers_own_untracked_mods(self):
        """The invariant: nothing automatic touches an untracked mod. A modlist
        that doesn't mention your hand-installed mod is not a request to remove
        it - only `mods` describes an active set anyone can act on."""
        self._publish_latest("lat.m", "2.0.0")
        self._install("mine.m", "1.0.0")
        self._untracked("Manual.dll")

        plan = mm.import_modlist({"schema": 1, "repos": [], "mods": ["lat.m"]},
                                 [], [self.BASE], side="client")
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

        exported = mm.export_modlist(self.game, {})
        plan = mm.import_modlist(exported, [], [self.BASE], side="client")
        self.assertEqual([u["name"] for u in plan.untracked], ["Manual.dll"])

    def test_import_resolves_pinned_exact_version(self):
        self._publish("pin.m", "1.0.0")
        self._publish("pin.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["pin.m@1.0.0"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")
        self.assertEqual([(m.id, m.version) for m in plan.roots], [("pin.m", "1.0.0")])

    def test_import_pulls_in_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE], side="client")
        self.assertEqual([m.id for m in plan.roots], ["root.m"])
        self.assertEqual([m.id for m in plan.dependencies], ["dep.k"])

    def test_import_unresolved_entry_blocks(self):
        modlist = {"schema": 1, "repos": ["Some/Pack"], "mods": ["ghost.m@1.0.0"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.unresolved, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.hinted_repos, ["Some/Pack"])

    def test_import_rejects_unsupported_schema(self):
        modlist = {"schema": 2, "repos": [], "mods": []}
        with self.assertRaises(mm.ModManagerError):
            mm.import_modlist(modlist, [], [self.BASE], side="client")

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
            mm.import_modlist(modlist, [], [self.BASE], side="client")
        self.assertIn("Shared.dll", str(caught.exception))

    def test_apply_import_installs_nothing_on_a_library_conflict(self):
        """apply_import has to be safe on its own, not merely unreachable with a
        bad plan. Collecting libraries only between the two loops meant every
        mod was already on disk by the time the conflict raised - installed, but
        with the disable pass never reached: a half-applied import."""
        self._publish_conflicting_pair()
        plan = mm.ImportPlan(
            roots=[mm._fetch_manifest([self.BASE], "one.m", "1.0.0"),
                   mm._fetch_manifest([self.BASE], "two.m", "1.0.0")],
            dependencies=[], unresolved=[], hinted_repos=[], untracked=[])

        with self.assertRaises(mm.ModManagerError):
            mm.apply_import(self.game, plan, self.progress.append)

        self.assertEqual(mm.list_installed_mods(self.game), [])
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "one.m")))

    def test_apply_import_installs_roots_and_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE], side="client")

        mm.apply_import(self.game, plan, self.progress.append)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "root.m", "root.m.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dep.k", "dep.k.dll")))

    def test_apply_import_disables_installed_mods_not_in_list(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        self._publish_latest("keep.m", "1.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["keep.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")

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
        plan = mm.import_modlist(modlist, index, [self.BASE], side="client")

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["dep.k"])

    def test_apply_import_leaves_already_disabled_mods_alone(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        modlist = {"schema": 1, "repos": [], "mods": []}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertFalse(installed["dis.m"])

    def test_modlist_import_disables_previews_without_side_effects(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        self._publish_latest("keep.m", "1.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["keep.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")

        self.assertEqual(mm.modlist_import_disables(self.game, plan), ["drop.m"])
        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["drop.m"])

    def test_apply_import_blocking_plan_raises_without_installing(self):
        modlist = {"schema": 1, "repos": [], "mods": ["ghost.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE], side="client")
        with self.assertRaises(mm.ModManagerError):
            mm.apply_import(self.game, plan)
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods")))

    def test_export_then_import_round_trip(self):
        self._install("rt.m", "1.0.0")
        exported = mm.export_modlist(self.game, {})
        # Re-serve rt.m at latest.json so the bare-id export entry resolves
        # on import, same as a real repo would.
        self.manifests[f"{self.BASE}/manifests/rt/m/latest.json"] = self.manifests[
            f"{self.BASE}/manifests/rt/m/1.0.0.json"]
        plan = mm.import_modlist(exported, [], [self.BASE], side="client")
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
        install._download_with_progress = fake_download

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
        d["sha256"] = hashlib.sha256(raw).hexdigest()
        self.manifests[f"{self.BASE}/manifests/{author}/{repo}/{version}.json"] = d
        return d

    def _install(self, mod_id):
        index = [summary(mod_id, ["1.0.0"])]
        mm.install_mod_closure(self.game, summary(mod_id, ["1.0.0"]), index,
                               [self.BASE], self.progress.append, side="client")

    def test_zip_bundle_extracts_into_mod_folder(self):
        self._publish_zip("bundle.m", "1.0.0", [
            ("Bundle.dll", b"MZfake"),
            ("Content/data.bundle", b"assetbundle-bytes"),
        ])
        self._install("bundle.m")
        modf = os.path.join(self.game, "Mods", "bundle.m")
        self.assertTrue(os.path.isfile(os.path.join(modf, "Bundle.dll")))
        self.assertTrue(os.path.isfile(os.path.join(modf, "Content", "data.bundle")))
        rec = read_json(os.path.join(modf, "manifest.json"))
        self.assertEqual(rec["package"], "zip")
        self.assertNotIn("filename", rec)

    def test_zip_shipping_its_own_manifest_json_is_not_left_damaged(self):
        """The archive's root manifest.json must not be extracted: the record
        is written under that name after the tree is hashed, so extracting it
        would record the archive's bytes, overwrite them, and leave the mod
        permanently 'damaged' (and reinstalled on every headless boot)."""
        self._publish_zip("ships.m", "1.0.0", [
            ("Ships.dll", b"MZfake"),
            ("manifest.json", b'{"not": "ours"}'),
            ("Content/manifest.json", b'{"nested": "fine"}'),
        ])
        self._install("ships.m")
        modf = os.path.join(self.game, "Mods", "ships.m")
        rec = read_json(os.path.join(modf, "manifest.json"))
        self.assertEqual(rec["id"], "ships.m")
        self.assertNotIn("manifest.json", rec["files"])
        self.assertIn("Content/manifest.json", rec["files"])
        self.assertTrue(mm.verify_mod_files(self.game, "ships.m"))

    def test_zip_slip_traversal_rejected_nothing_installed(self):
        self._publish_zip("evil.m", "1.0.0", [
            ("ok.dll", b"fine"),
            ("../escape.dll", b"pwned"),
        ])
        with self.assertRaises(mm.ModManagerError):
            self._install("evil.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "evil.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "escape.dll")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".evil.m.installing")))

    def test_zip_absolute_path_rejected(self):
        self._publish_zip("abs.m", "1.0.0", [("/tmp/evil.dll", b"x")])
        with self.assertRaises(mm.ModManagerError):
            self._install("abs.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "abs.m")))

    def test_zip_alternate_data_stream_entry_rejected(self):
        """"mod.dll:payload" is an NTFS stream hanging off mod.dll, not a file.
        It contains no separator and no "..", so the traversal guard reads it as
        an ordinary relative name and realpath resolves it inside the mod
        folder - the containment check passes and a hidden stream gets written."""
        self._publish_zip("ads.m", "1.0.0", [("ok.dll:payload", b"hidden")])
        with self.assertRaises(mm.ModManagerError):
            self._install("ads.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "ads.m")))

    def test_zip_drive_relative_entry_rejected(self):
        self._publish_zip("drv.m", "1.0.0", [("C:evil.dll", b"x")])
        with self.assertRaises(mm.ModManagerError):
            self._install("drv.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "drv.m")))

    def test_zip_symlink_entry_rejected(self):
        symlink_attr = 0o120777 << 16
        self._publish_zip("sym.m", "1.0.0", [("link", b"/etc/passwd", symlink_attr)])
        with self.assertRaises(mm.ModManagerError):
            self._install("sym.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "sym.m")))

    def test_zip_checksum_mismatch_aborts(self):
        d = self._publish_zip("badz.m", "1.0.0", [("a.dll", b"x")])
        d["sha256"] = "f" * 64
        with self.assertRaises(mm.ModManagerError):
            self._install("badz.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "badz.m")))

    def test_unknown_package_type_manifest_skipped(self):
        self.assertIsNone(mm._manifest_from_dict(
            manifest_dict("a.b", "1.0.0", package="rar"), DEFAULT))

    def test_zip_bomb_total_size_aborts(self):
        # One member whose decompressed size exceeds the cap.
        saved = install._ZIP_MAX_TOTAL_UNCOMPRESSED
        install._ZIP_MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024
        self.addCleanup(
            lambda: setattr(install, "_ZIP_MAX_TOTAL_UNCOMPRESSED", saved))
        self._publish_zip("bomb.m", "1.0.0", [
            ("mod.dll", b"MZ"),
            ("big.bin", b"\0" * (8 * 1024 * 1024)),   # 8 MB of zeros, > 4 MB cap
        ])
        with self.assertRaises(mm.ModManagerError):
            self._install("bomb.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "bomb.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".bomb.m.installing")))

    def test_zip_too_many_entries_aborts(self):
        saved = install._ZIP_MAX_ENTRIES
        install._ZIP_MAX_ENTRIES = 5
        self.addCleanup(lambda: setattr(install, "_ZIP_MAX_ENTRIES", saved))
        self._publish_zip("many.m", "1.0.0",
                          [(f"f{i}.dat", b"x") for i in range(10)])
        with self.assertRaises(mm.ModManagerError):
            self._install("many.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "many.m")))


class HostConfigSeam(unittest.TestCase):
    """The one thing the mod manager can't import for itself.

    The palette, the downloader and the hasher used to be handed over at
    startup by set_helpers(host); they're plain imports now, because they're
    the same code for both apps. Config isn't: the client reads its own file
    and a server reads its own, so which load_cfg/save_cfg is correct depends
    on which app is running. That last piece is what hostcfg carries, and
    these cover the states it can be in.
    """

    def setUp(self):
        prev = (hostcfg._load, hostcfg._save)
        self.addCleanup(lambda: hostcfg.set_config_accessors(*prev))
        hostcfg.set_config_accessors(None, None)

    def test_wired_accessors_are_used(self):
        store = {"mod_repos": {"a/b": "https://example.invalid/b"}}
        written = []
        hostcfg.set_config_accessors(lambda: store, written.append)
        self.assertEqual(hostcfg.load_cfg(), store)
        hostcfg.save_cfg({"x": 1})
        self.assertEqual(written, [{"x": 1}])

    def test_load_without_wiring_is_a_clear_error(self):
        with self.assertRaises(mm.ModManagerError) as ctx:
            hostcfg.load_cfg()
        self.assertIn("set_config_accessors", str(ctx.exception))

    def test_save_without_wiring_is_a_silent_no_op(self):
        """The logic layer is usable, and tested, with no host config at all,
        so an unwired save is survivable rather than fatal."""
        hostcfg.save_cfg({"anything": True})   # must not raise

    def test_callers_that_need_persistence_get_a_hard_error(self):
        """set_declined can't quietly lose a write - the window hands back the
        whole declined set and expects it to stick."""
        with self.assertRaises(mm.ModManagerError):
            hostcfg.require_save_cfg()
        with self.assertRaises(mm.ModManagerError):
            mm.set_declined({}, "some.host", ["a.b"])

    def test_require_returns_the_wired_saver(self):
        written = []
        saver = written.append
        hostcfg.set_config_accessors(dict, saver)
        self.assertIs(hostcfg.require_save_cfg(), saver)


class FacadeGuardsAgainstDeadStubs(unittest.TestCase):
    """The read-only facade these tests use is load-bearing: a stub written to
    it instead of to the owning module would leave the real network helper in
    place and quietly test production."""

    def test_writing_to_the_facade_raises(self):
        with self.assertRaises(AttributeError) as ctx:
            mm._get_json = lambda *_a, **_k: {}
        self.assertIn("owning module", str(ctx.exception))

    def test_unknown_name_raises(self):
        with self.assertRaises(AttributeError):
            mm.no_such_name_anywhere


class DamageDetection(_FakeInstallFixture, unittest.TestCase):
    """verify_mod_files + the 'damaged' status: installed community mods get
    the same integrity story the core components (patch/ML/TavernLib) have -
    the record carries per-file hashes written at install, and mod_status
    checks them before it says anything about versions."""

    def _dll(self, mod_id):
        return os.path.join(self.game, "Mods", mod_id, f"{mod_id}.dll")

    def test_install_records_per_file_hashes(self):
        self._install("h.m", "1.0.0")
        rec = read_json(os.path.join(self.game, "Mods", "h.m", "manifest.json"))
        # Exactly the placed dll, hashed for real; the record itself is never
        # in its own map (it didn't exist when the map was computed).
        with open(self._dll("h.m"), "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(rec["files"], {"h.m.dll": expected})

    def test_intact_install_verifies_true(self):
        self._install("ok.m", "1.0.0")
        self.assertIs(mm.verify_mod_files(self.game, "ok.m"), True)
        self.assertEqual(mm.mod_status(self.game, "ok.m",
                                       [summary("ok.m", ["1.0.0"])]), "current")

    def test_an_altered_file_reads_damaged(self):
        self._install("av.m", "1.0.0")
        with open(self._dll("av.m"), "wb") as f:
            f.write(b"eaten by antivirus")
        self.assertIs(mm.verify_mod_files(self.game, "av.m"), False)
        self.assertEqual(mm.mod_status(self.game, "av.m",
                                       [summary("av.m", ["1.0.0"])]), "damaged")

    def test_a_deleted_file_reads_damaged(self):
        self._install("gone.m", "1.0.0")
        os.remove(self._dll("gone.m"))
        self.assertIs(mm.verify_mod_files(self.game, "gone.m"), False)

    def test_damage_outranks_outdated(self):
        """A damaged install's version number is a claim about files that are
        no longer there, so it must not soften to 'Update ready'."""
        self._install("d.old", "1.0.0")
        with open(self._dll("d.old"), "wb") as f:
            f.write(b"junk")
        self.assertEqual(mm.mod_status(self.game, "d.old",
                                       [summary("d.old", ["1.0.0", "2.0.0"])]), "damaged")

    def test_a_record_without_a_files_map_is_never_damaged(self):
        """Pre-existing installs, from before the field, have no map: no
        evidence either way must read as None/current, or shipping this would
        mark every existing install Damaged overnight."""
        self._install("legacy.m", "1.0.0")
        record_path = mm._mod_record_path(self.game, "legacy.m")
        rec = read_json(record_path)
        del rec["files"]
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        with open(self._dll("legacy.m"), "wb") as f:
            f.write(b"altered, but unprovable")
        self.assertIsNone(mm.verify_mod_files(self.game, "legacy.m"))
        self.assertEqual(mm.mod_status(self.game, "legacy.m",
                                       [summary("legacy.m", ["1.0.0"])]), "current")

    def test_verification_is_memoized_until_invalidated(self):
        """The memo is the price of hashing being affordable on every status
        refresh: damage appearing after a clean check isn't seen until the
        memo is invalidated (an install of that mod, or Refresh clearing it
        whole - which is what this pins)."""
        self._install("memo.m", "1.0.0")
        self.assertIs(mm.verify_mod_files(self.game, "memo.m"), True)
        with open(self._dll("memo.m"), "wb") as f:
            f.write(b"corrupted after the check")
        self.assertIs(mm.verify_mod_files(self.game, "memo.m"), True)   # stale, by design
        mm._invalidate_verify_memo()
        self.assertIs(mm.verify_mod_files(self.game, "memo.m"), False)

    def test_reinstall_invalidates_the_memo_and_repairs(self):
        self._install("fix.m", "1.0.0")
        with open(self._dll("fix.m"), "wb") as f:
            f.write(b"junk")
        mm._invalidate_verify_memo()
        self.assertIs(mm.verify_mod_files(self.game, "fix.m"), False)
        self._install("fix.m", "1.0.0")    # reinstall over it
        self.assertIs(mm.verify_mod_files(self.game, "fix.m"), True)

    def test_handshake_entries_carry_the_artifact_sha(self):
        """Additive advisory field; the fingerprint hash itself must not move
        (it's byte-identical with TavernLib's C#)."""
        self._install("s.m", "1.0.0")
        mods_hash, _, entries, _ = mm.handshake_snapshot(self.game)
        rec = read_json(os.path.join(self.game, "Mods", "s.m", "manifest.json"))
        self.assertEqual(entries[0]["sha256"], rec["sha256"])
        self.assertEqual(mods_hash,
                         hashlib.sha256(b"s.m@1.0.0@req").hexdigest())


class CacheIntegrityAndPruning(_TempDataDirFixture, unittest.TestCase):
    """cache_restore_mod's restore-time verification and cache_store_mod's
    version pruning."""

    def test_restore_verifies_and_discards_a_rotten_entry(self):
        self._install("rot.m", "1.0.0")
        mm.cache_store_mod(self.game, "rot.m")
        entry = mm._cache_mod_dir("rot.m", "1.0.0")
        with open(os.path.join(entry, "rot.m.dll"), "wb") as f:
            f.write(b"bitrot")

        with self.assertRaises(mm.ModManagerError) as ctx:
            mm.cache_restore_mod(self.game, "rot.m", "1.0.0")
        self.assertIn("rot.m", str(ctx.exception))
        # The bad entry is gone (not poised to fail every future restore), and
        # the installed copy was never touched.
        self.assertFalse(os.path.isdir(entry))
        self.assertIs(mm.verify_mod_files(self.game, "rot.m"), True)

    def test_render_falls_back_to_download_when_the_cache_entry_is_bad(self):
        self._install("fb.m", "1.0.0")
        mm.cache_store_mod(self.game, "fb.m")
        mm.uninstall_mod(self.game, "fb.m")
        entry = mm._cache_mod_dir("fb.m", "1.0.0")
        with open(os.path.join(entry, "fb.m.dll"), "wb") as f:
            f.write(b"bitrot")

        server_mods = [_srv("fb.m", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [summary("fb.m", ["1.0.0"])], [self.BASE])
        entry_plan = next(e for e in plan.entries if e.mod_id == "fb.m")
        self.assertTrue(entry_plan.cached)     # planned as a file move...
        mm.render_active_set(self.game, plan, self.progress.append)
        # ...but rendered as a verified download once the entry failed its
        # restore-time check, leaving a correct install either way.
        self.assertIs(mm.verify_mod_files(self.game, "fb.m"), True)
        self.assertTrue(any("downloading instead" in m.lower() for m in self.progress))

    def test_an_entry_without_a_files_map_still_restores(self):
        """A cache entry stored from a pre-'files' install has nothing to
        check against; restoring it unchecked mirrors verify_mod_files'
        None."""
        self._install("old.m", "1.0.0")
        record_path = mm._mod_record_path(self.game, "old.m")
        rec = read_json(record_path)
        del rec["files"]
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        mm.cache_store_mod(self.game, "old.m")
        mm.uninstall_mod(self.game, "old.m")
        mm.cache_restore_mod(self.game, "old.m", "1.0.0")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "old.m", "old.m.dll")))

    def test_store_prunes_oldest_versions_past_the_cap(self):
        base = os.path.join(mm._cache_base(), "pr.m")
        for i, version in enumerate(["1.0.0", "1.1.0", "1.2.0", "1.3.0"]):
            self._install("pr.m", version)
            mm.cache_store_mod(self.game, "pr.m")
            # Stamp distinct store times: pruning orders by directory mtime,
            # and four stores can land within one clock tick.
            os.utime(os.path.join(base, version), (1000 + i, 1000 + i))

        kept = sorted(d for d in os.listdir(base) if not d.endswith(".caching"))
        self.assertEqual(kept, ["1.1.0", "1.2.0", "1.3.0"])

    def test_restoring_an_already_cached_version_does_not_prune(self):
        """A re-store of a version already in the cache returns early - the
        set must come through untouched, at or under the cap or not."""
        base = os.path.join(mm._cache_base(), "pin.m")
        for i, version in enumerate(["1.0.0", "1.1.0", "1.2.0"]):
            self._install("pin.m", version)
            mm.cache_store_mod(self.game, "pin.m")
            os.utime(os.path.join(base, version), (1000 + i, 1000 + i))
        self._install("pin.m", "1.0.0")
        mm.cache_store_mod(self.game, "pin.m")    # early return: already cached
        kept = sorted(d for d in os.listdir(base) if not d.endswith(".caching"))
        self.assertEqual(kept, ["1.0.0", "1.1.0", "1.2.0"])


class EnsureLibraries(_TempDataDirFixture, unittest.TestCase):
    """ensure_mod_libraries: the bare-re-enable repair. A join that
    deactivates a mod prunes its orphaned libraries; re-enabling from the
    manager window is just a record rename, so this is what puts the
    libraries back."""

    LIB_URL = "https://x/NeededLib.dll"

    def _install_with_lib(self, mod_id, version):
        lib = {"name": "NeededLib", "download_url": self.LIB_URL,
               "sha256": "", "filename": "NeededLib.dll"}
        self._publish(mod_id, version, library_dependencies=[lib])
        mm.install_mod_closure(self.game, summary(mod_id, [version]),
                               [summary(mod_id, [version])],
                               [self.BASE], self.progress.append, side="client")

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
            "NeededLib.dll", self._sha_for(self.LIB_URL))))

    def test_noop_when_everything_is_in_place(self):
        self._install_with_lib("lib.ok", "1.0.0")
        self.assertEqual(mm.ensure_mod_libraries(self.game, "lib.ok"), [])

    def test_noop_for_a_mod_with_no_libraries(self):
        self._publish("nolib.m", "1.0.0")
        mm.install_mod_closure(self.game, summary("nolib.m", ["1.0.0"]),
                               [summary("nolib.m", ["1.0.0"])],
                               [self.BASE], self.progress.append, side="client")
        self.assertEqual(mm.ensure_mod_libraries(self.game, "nolib.m"), [])

    def test_noop_for_a_mod_that_is_not_installed(self):
        self.assertEqual(mm.ensure_mod_libraries(self.game, "ghost.m"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
