"""
Tests for modmanager.py.

repository.json holds slim ModSummary rows; the full manifest for each
version is fetched on demand. Tests stub _get_json (serves repository.json
and per-version manifests by URL) and the download/hash helpers. No network.

Run from TavernLauncher/:  python -m unittest -v   or   python tests/test_modmanager.py
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import modmanager as mm


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
        mm._get_json = fake

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
        mm._get_json = fake

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
        got = mm.resolve_dependencies([root], index, fetch)
        self.assertEqual([m.version for m in got], ["1.4.0"])   # not 2.1.0, not 1.1.0

    def test_diamond_conflict_raises(self):
        index = [summary("dep.k", ["1.0.0"]), summary("dep.k", ["2.0.0"])]
        # a resolves dep.k@1, then b's major-2 requirement conflicts with it
        fetch = self._store(make_manifest("dep.k", "1.0.0"))
        a = make_manifest("a.a", "1.0.0", dependencies={"dep.k": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"dep.k": "2.0.0"})
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([a, b], index, fetch)

    def test_cycle_raises(self):
        index = [summary("a.a", ["1.0.0"]), summary("b.b", ["1.0.0"])]
        a = make_manifest("a.a", "1.0.0", dependencies={"b.b": "1.0.0"})
        b = make_manifest("b.b", "1.0.0", dependencies={"a.a": "1.0.0"})
        fetch = self._store(a, b)
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([a], index, fetch)

    def test_missing_dependency_raises(self):
        root = make_manifest("root.m", "1.0.0", dependencies={"nope.x": "1.0.0"})
        with self.assertRaises(mm.ModManagerError):
            mm.resolve_dependencies([root], [], self._store())


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
        mm._get_json = fake_get_json

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

        mm.set_helpers(types.SimpleNamespace(_download_with_progress=fake_download,
                                             _sha256_file=fake_hash))
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
        mm.install_mod_closure(self.game, root_summary, index, [self.BASE], self.progress.append)

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
                               [self.BASE], self.progress.append, version="1.0.0")
        mod_meta = read_json(os.path.join(self.game, "Mods", "v.v", "manifest.json"))
        self.assertEqual(mod_meta["version"], "1.0.0")

    def test_checksum_mismatch_aborts_leaving_nothing(self):
        d = self._publish("bad.m", "1.0.0")
        d["sha256"] = "f" * 64        # break it after publishing
        index = [summary("bad.m", ["1.0.0"])]
        with self.assertRaises(mm.ModManagerError):
            mm.install_mod_closure(self.game, summary("bad.m", ["1.0.0"]), index,
                                   [self.BASE], self.progress.append)
        # No mod folder and no staging left behind.
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "bad.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".bad.m.installing")))

    def test_status_current_then_outdated(self):
        self._publish("s.s", "1.0.0")
        index = [summary("s.s", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("s.s", ["1.0.0"]), index,
                               [self.BASE], self.progress.append)
        self.assertEqual(mm.mod_status(self.game, "s.s", [summary("s.s", ["1.0.0"])]), "current")
        self.assertEqual(mm.mod_status(self.game, "s.s", [summary("s.s", ["1.0.0", "1.1.0"])]), "outdated")

    def test_uninstall_removes_mod_and_its_orphaned_library(self):
        lib = {"name": "OrphanLib", "download_url": "https://x/OrphanLib.dll",
               "sha256": "", "filename": "OrphanLib.dll"}
        self._publish("u.m", "1.0.0", library_dependencies=[dict(lib)])
        index = [summary("u.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("u.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append)

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
                               [self.BASE], self.progress.append)
        mm.install_mod_closure(self.game, summary("b.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append)

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
                               [self.BASE], self.progress.append)
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
                               [self.BASE], self.progress.append)
        # Enabling an already-enabled mod, or disabling twice, is a no-op.
        self.assertFalse(mm.enable_mod(self.game, "n.m"))
        self.assertTrue(mm.disable_mod(self.game, "n.m"))
        self.assertFalse(mm.disable_mod(self.game, "n.m"))

    def test_uninstall_removes_disabled_mod_folder(self):
        self._publish("z.m", "1.0.0")
        index = [summary("z.m", ["1.0.0"])]
        mm.install_mod_closure(self.game, summary("z.m", ["1.0.0"]), index,
                               [self.BASE], self.progress.append)
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
                               [self.BASE], self.progress.append)
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

    def _install(self, mod_id, version="1.0.0"):
        self._publish(mod_id, version)
        mm.install_mod_closure(self.game, summary(mod_id, [version]),
                               [summary(mod_id, [version])], [self.BASE],
                               self.progress.append)

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
                               [self.BASE], self.progress.append)
        mm.install_mod_closure(self.game, summary("a.a", ["2.1.0"]), [summary("a.a", ["2.1.0"])],
                               [self.BASE], self.progress.append)

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
                               [self.BASE], self.progress.append)
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
                               [self.BASE], self.progress.append)
        mm.disable_mod(self.game, "d.d")

        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 0)
        self.assertEqual(entries, [])
        self.assertEqual(mods_hash, hashlib.sha256(b"").hexdigest())

    def test_hash_changes_when_a_version_changes(self):
        self._publish("v.v", "1.0.0")
        mm.install_mod_closure(self.game, summary("v.v", ["1.0.0"]), [summary("v.v", ["1.0.0"])],
                               [self.BASE], self.progress.append)
        before, _, _, _ = mm.handshake_snapshot(self.game)

        self._publish("v.v", "1.1.0")
        mm.install_mod_closure(self.game, summary("v.v", ["1.1.0"]), [summary("v.v", ["1.1.0"])],
                               [self.BASE], self.progress.append)
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
                               self.progress.append)
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


class DisplacedFolders(_FakeInstallFixture, unittest.TestCase):
    """Installing over a Mods/<id>/ folder we didn't create moves it aside
    instead of deleting it. TavernLib's ClearInstallPath does the same thing for
    headless servers, where there's nobody to prompt."""

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        self._saved_data_dir = mm._tavern_data_dir
        mm._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(mm, "_tavern_data_dir", self._saved_data_dir))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))

    def _install(self, mod_id, version="1.0.0"):
        self._publish(mod_id, version)
        mm.install_mod_closure(self.game, summary(mod_id, [version]),
                               [summary(mod_id, [version])], [self.BASE],
                               self.progress.append)

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


class ClientModCache(_FakeInstallFixture, unittest.TestCase):
    """The client mod cache: mod_cache/<id>/<version>/ and the library
    equivalent, keyed by filename+sha256. Points _tavern_data_dir at a throwaway
    temp dir so tests never touch the real %AppData%."""

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        self._saved_data_dir = mm._tavern_data_dir
        mm._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(mm, "_tavern_data_dir", self._saved_data_dir))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))

    def _install(self, mod_id, version, **over):
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]), [summary(mod_id, [version])],
                               [self.BASE], self.progress.append)

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


class PlanJoin(_FakeInstallFixture, unittest.TestCase):
    """plan_join: the pure resolution core. Points _tavern_data_dir at a
    throwaway temp dir (needed for the cached/active-entry checks) the same
    way ClientModCache does."""

    OTHER = OTHER

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        self._saved_data_dir = mm._tavern_data_dir
        mm._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(mm, "_tavern_data_dir", self._saved_data_dir))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))

    def _install(self, mod_id, version, **over):
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]), [summary(mod_id, [version])],
                               [self.BASE], self.progress.append)

    def _entry(self, plan, mod_id):
        return next(e for e in plan.entries if e.mod_id == mod_id)

    def test_required_mod_becomes_active_entry(self):
        self._publish("req.m", "1.0.0")
        server_mods = [{"id": "req.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "srv.m", "version": "1.0.0", "client_side": False, "server_side": True, "parity_required": True}]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.missing, [])

    def test_dependency_pulled_in_with_reason_dependency(self):
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        server_mods = [{"id": "root.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("dep.k", ["1.0.0"]), summary("root.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual({e.mod_id: e.reason for e in plan.entries},
                         {"root.m": "required", "dep.k": "dependency"})

    def test_missing_required_mod_blocks_with_no_hint(self):
        server_mods = [{"id": "ghost.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.needs_repo, [])

    def test_needs_repo_when_hint_names_unadded_repo_not_blocking(self):
        server_mods = [{"id": "elsewhere.m", "version": "1.0.0", "client_side": True,
                        "server_side": True, "parity_required": True,
                        "source_repo": self.OTHER}]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertFalse(plan.blocking)
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

    def test_pin_conflict_when_server_requires_a_different_exact_version(self):
        self._publish("conf.m", "2.0.0")
        server_mods = [{"id": "conf.m", "version": "2.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("conf.m", ["1.0.0", "2.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE], pinned=["conf.m@1.0.0"])
        self.assertEqual(plan.pin_conflicts, [("conf.m", "1.0.0", "2.0.0")])
        # The server's exact required version wins the single active entry -
        # the pin conflict is surfaced, not silently overridden or duplicated.
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(self._entry(plan, "conf.m").version, "2.0.0")

    def test_active_true_when_already_installed_at_matching_version(self):
        self._install("act.m", "1.0.0")
        server_mods = [{"id": "act.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("act.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        entry = self._entry(plan, "act.m")
        self.assertTrue(entry.active)

    def test_cached_true_when_version_cached_but_not_currently_active(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m")
        mm.uninstall_mod(self.game, "cch.m")
        server_mods = [{"id": "cch.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "root.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("root.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual([l.filename for l in plan.libraries], ["PlanLib.dll"])


class ModDiff(_FakeInstallFixture, unittest.TestCase):
    """build_mod_diff: the client-vs-server comparison the diff window renders.
    Pure, so none of this needs a display."""

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        self._saved_data_dir = mm._tavern_data_dir
        mm._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(mm, "_tavern_data_dir", self._saved_data_dir))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))

    def _install(self, mod_id, version, **over):
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]), [summary(mod_id, [version])],
                               [self.BASE], self.progress.append)

    def _diff(self, server_mods, index):
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        return {r.mod_id: r for r in mm.build_mod_diff(self.game, server_mods, plan)}, plan

    @staticmethod
    def _srv(mod_id, version, client=True, server=True, required=None):
        return {"id": mod_id, "version": version,
                "client_side": client, "server_side": server,
                "parity_required": True if required is None else required}

    def test_not_installed_is_an_install(self):
        self._publish("d.new", "1.0.0")
        rows, _ = self._diff([self._srv("d.new", "1.0.0")], [summary("d.new", ["1.0.0"])])
        self.assertEqual(rows["d.new"].action, mm.ACTION_INSTALL)
        self.assertEqual(rows["d.new"].server_version, "1.0.0")
        self.assertEqual(rows["d.new"].client_version, "")

    def test_matching_version_is_a_match(self):
        self._install("d.same", "1.0.0")
        rows, _ = self._diff([self._srv("d.same", "1.0.0")], [summary("d.same", ["1.0.0"])])
        self.assertEqual(rows["d.same"].action, mm.ACTION_MATCH)
        self.assertEqual(rows["d.same"].client_version, "1.0.0")

    def test_older_installed_is_an_update_newer_is_a_downgrade(self):
        index = [summary("d.up", ["1.0.0"]), summary("d.up", ["2.0.0"])]
        self._install("d.up", "1.0.0")
        self._publish("d.up", "2.0.0")
        rows, _ = self._diff([self._srv("d.up", "2.0.0")], index)
        self.assertEqual(rows["d.up"].action, mm.ACTION_UPDATE)

        # The same pair the other way round: the client is ahead and the server
        # pins the older build, which parity makes mandatory rather than optional.
        self._install("d.up", "2.0.0")
        rows, _ = self._diff([self._srv("d.up", "1.0.0")], index)
        self.assertEqual(rows["d.up"].action, mm.ACTION_DOWNGRADE)
        self.assertEqual(rows["d.up"].client_version, "2.0.0")
        self.assertEqual(rows["d.up"].server_version, "1.0.0")
        self.assertFalse(rows["d.up"].optional)

    def test_installed_but_disabled_is_an_enable(self):
        self._install("d.off", "1.0.0")
        mm.disable_mod(self.game, "d.off")
        rows, _ = self._diff([self._srv("d.off", "1.0.0")], [summary("d.off", ["1.0.0"])])
        self.assertEqual(rows["d.off"].action, mm.ACTION_ENABLE)
        self.assertFalse(rows["d.off"].client_enabled)

    def test_cached_version_activates_rather_than_downloads(self):
        self._install("d.cch", "1.0.0")
        mm.cache_store_mod(self.game, "d.cch")
        mm.uninstall_mod(self.game, "d.cch")
        rows, _ = self._diff([self._srv("d.cch", "1.0.0")], [summary("d.cch", ["1.0.0"])])
        row = rows["d.cch"]
        self.assertEqual(row.action, mm.ACTION_ACTIVATE)
        self.assertTrue(row.cached)
        self.assertFalse(row.downloads)     # a file move, no bytes fetched

    def test_downloads_flag_marks_only_rows_that_fetch_bytes(self):
        self._publish("d.dl", "1.0.0")
        rows, _ = self._diff([self._srv("d.dl", "1.0.0")], [summary("d.dl", ["1.0.0"])])
        self.assertTrue(rows["d.dl"].downloads)      # not here and not cached

        # An uncached update fetches; a match or a deactivate never does.
        self._install("d.upd", "1.0.0")
        self._publish("d.upd", "2.0.0")
        rows, _ = self._diff([self._srv("d.upd", "2.0.0")],
                             [summary("d.upd", ["1.0.0"]), summary("d.upd", ["2.0.0"])])
        self.assertTrue(rows["d.upd"].downloads)

        self._install("d.ok", "1.0.0")
        rows, _ = self._diff([self._srv("d.ok", "1.0.0")], [summary("d.ok", ["1.0.0"])])
        self.assertFalse(rows["d.ok"].downloads)

    def test_extra_client_mod_is_an_optional_deactivate(self):
        self._install("d.mine", "1.0.0")
        self._publish("d.theirs", "1.0.0")
        rows, _ = self._diff([self._srv("d.theirs", "1.0.0")],
                             [summary("d.theirs", ["1.0.0"])])
        mine = rows["d.mine"]
        self.assertEqual(mine.action, mm.ACTION_DEACTIVATE)
        # The server never checks a mod it doesn't run, so this one is the
        # player's call rather than something applied silently.
        self.assertTrue(mine.optional)
        self.assertEqual(mine.server_version, "")
        self.assertFalse(rows["d.theirs"].optional)

    def test_server_only_mod_is_listed_but_not_required(self):
        rows, _ = self._diff([self._srv("d.srv", "1.0.0", client=False, server=True)], [])
        self.assertEqual(rows["d.srv"].action, mm.ACTION_SERVER_ONLY)
        self.assertEqual(rows["d.srv"].client_version, "")
        self.assertFalse(rows["d.srv"].blocking)

    def test_unresolvable_required_mod_blocks(self):
        rows, plan = self._diff([self._srv("d.gone", "1.0.0")], [])
        self.assertEqual(rows["d.gone"].action, mm.ACTION_MISSING)
        self.assertTrue(rows["d.gone"].blocking)
        self.assertTrue(plan.blocking)

    def test_needs_repo_row_names_the_source_to_add(self):
        server_mods = [{"id": "d.other", "version": "1.0.0", "client_side": True,
                        "server_side": True, "parity_required": True,
                        "source_repo": OTHER}]
        rows, plan = self._diff(server_mods, [])
        row = rows["d.other"]
        self.assertEqual(row.action, mm.ACTION_NEEDS_REPO)
        self.assertFalse(row.blocking)      # informational, not a launch stopper
        self.assertIn(mm._repo_shorthand(OTHER), row.note)

    def test_problems_sort_above_changes_and_matches(self):
        self._install("d.keep", "1.0.0")     # becomes an optional deactivate
        self._publish("d.add", "1.0.0")
        server_mods = [self._srv("d.add", "1.0.0"), self._srv("d.gone", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, [summary("d.add", ["1.0.0"])], [self.BASE])
        order = [r.action for r in mm.build_mod_diff(self.game, server_mods, plan)]
        self.assertEqual(order[0], mm.ACTION_MISSING)
        self.assertLess(order.index(mm.ACTION_INSTALL), order.index(mm.ACTION_DEACTIVATE))

    def test_pin_conflict_annotates_the_row_without_duplicating_it(self):
        self._publish("d.pin", "1.0.0")
        self._publish("d.pin", "2.0.0")
        index = [summary("d.pin", ["1.0.0"]), summary("d.pin", ["2.0.0"])]
        server_mods = [self._srv("d.pin", "1.0.0")]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE], pinned=["d.pin@2.0.0"])
        rows = mm.build_mod_diff(self.game, server_mods, plan)
        self.assertEqual(len([r for r in rows if r.mod_id == "d.pin"]), 1)
        self.assertIn("2.0.0", rows[0].note)
        self.assertIn("1.0.0", rows[0].note)


class RenderActiveSet(_FakeInstallFixture, unittest.TestCase):
    """render_active_set: applying a JoinPlan to disk. Same throwaway
    _tavern_data_dir patch as ClientModCache/PlanJoin."""

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        self._saved_data_dir = mm._tavern_data_dir
        mm._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(mm, "_tavern_data_dir", self._saved_data_dir))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))

    def _install(self, mod_id, version, **over):
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]), [summary(mod_id, [version])],
                               [self.BASE], self.progress.append)

    def test_downloads_missing_required_mod_and_caches_it(self):
        self._publish("dl.m", "1.0.0")
        server_mods = [{"id": "dl.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("dl.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        mm.render_active_set(self.game, plan)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dl.m", "dl.m.dll")))
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("dl.m", "1.0.0")))

    def test_restores_from_cache_without_redownload(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m")
        mm.uninstall_mod(self.game, "cch.m")

        server_mods = [{"id": "cch.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("cch.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan)
        self.assertEqual(len(self.dl_dirs), downloads_before)   # no new download
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "cch.m", "cch.m.dll")))

    def test_adopting_first_preserves_the_version_a_render_replaces(self):
        """The reason att_client calls adopt_installed_mods before planning.

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

        server_mods = [{"id": "adopt.m", "version": "2.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("adopt.m", ["1.0.0"]), summary("adopt.m", ["2.0.0"])]
        mm.render_active_set(self.game, mm.plan_join(self.game, server_mods, index, [self.BASE]))

        # 2.0.0 is now what's live, and 1.0.0 survived the swap in the cache.
        self.assertEqual(mm._read_mod_record(self.game, "adopt.m")["version"], "2.0.0")
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("adopt.m", "1.0.0")))

        # Dropping back to 1.0.0 is therefore a file move, not a download.
        back = [{"id": "adopt.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        plan_back = mm.plan_join(self.game, back, index, [self.BASE])
        self.assertTrue(plan_back.entries[0].cached)
        downloads_before = len(self.dl_dirs)
        mm.render_active_set(self.game, plan_back)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertEqual(mm._read_mod_record(self.game, "adopt.m")["version"], "1.0.0")

    def test_already_active_entry_is_left_alone(self):
        self._install("act.m", "1.0.0")
        server_mods = [{"id": "act.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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

        server_mods = [{"id": "back.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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

        server_mods = [{"id": "ver.m", "version": "2.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "lib.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "lib2.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "ghost.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": i, "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}
                       for i in ("step.a", "step.b")]
        index = [summary("step.a", ["1.0.0"]), summary("step.b", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        steps = self._stepped(plan)
        self.assertEqual(steps, [("step.a", "start"), ("step.a", "done"),
                                 ("step.b", "start"), ("step.b", "done")])

    def test_deactivation_reports_a_step_too(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        server_mods = [{"id": "keep.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "libbed.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
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
        server_mods = [{"id": "libbed.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("libbed.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        # Nothing to do for the mod itself, so the library is the whole render.
        self.assertEqual([e.active for e in plan.entries], [True])
        self.assertEqual(mm.render_plan_steps(plan), 1)
        self.assertEqual(self._stepped(plan),
                         [("Shared.dll", "start"), ("Shared.dll", "done")])

    def test_rendering_without_a_step_hook_still_works(self):
        self._publish("nohook.m", "1.0.0")
        server_mods = [{"id": "nohook.m", "version": "1.0.0", "client_side": True, "server_side": True, "parity_required": True}]
        index = [summary("nohook.m", ["1.0.0"])]
        mm.render_active_set(self.game, mm.plan_join(self.game, server_mods, index, [self.BASE]))
        self.assertTrue(os.path.isdir(os.path.join(self.game, "Mods", "nohook.m")))


class Parity(_FakeInstallFixture, unittest.TestCase):
    """parity: whether a server running a mod obliges the client to match it.

    "required" is the stricter answer, so it's what a malformed value falls back
    to - an unreviewed source must not be able to downgrade a mod a server needs
    by inventing one. "optional" means the server won't refuse a join over it,
    so the client is offered the mod and may decline. Absent entirely is neither:
    that's malformed data, and every reader rejects it."""

    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="tavern_appdata_")
        self._saved_data_dir = mm._tavern_data_dir
        mm._tavern_data_dir = lambda: self.data_dir
        self.addCleanup(lambda: setattr(mm, "_tavern_data_dir", self._saved_data_dir))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))

    def _install(self, mod_id, version, **over):
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]), [summary(mod_id, [version])],
                               [self.BASE], self.progress.append)

    @staticmethod
    def _srv(mod_id, version, required=None, client=True, server=True):
        return {"id": mod_id, "version": version,
                "client_side": client, "server_side": server,
                "parity_required": True if required is None else required}

    # -- reading the value ---------------------------------------------------

    def test_absent_parity_is_an_error_not_a_default(self):
        """No default anywhere. Guessing is the one mistake that matters here,
        since this decides whether a joining client is obliged to match."""
        with self.assertRaises(mm.ModManagerError) as caught:
            mm.parity_required({"id": "a.b"})
        self.assertIn("parity_required", str(caught.exception))

    def test_unknown_parity_reads_as_required(self):
        """Present but malformed still fails closed: an unreviewed source must
        not be able to downgrade a mod the server needs by inventing a value."""
        for bad in ("false", "", None, 0, [], "optional"):
            self.assertTrue(mm.parity_required({"parity_required": bad}), bad)

    def test_only_a_real_false_relaxes_it(self):
        self.assertFalse(mm.parity_required({"parity_required": False}))

    def test_a_manifest_without_the_field_is_rejected(self):
        """Every published manifest states it outright, so one that doesn't is
        malformed rather than merely old."""
        d = manifest_dict("no.parity", "1.0.0")
        d.pop("parity_required")
        with self.assertRaises(mm.ModManagerError) as caught:
            mm._manifest_from_dict(d, self.BASE)
        self.assertIn("parity_required", str(caught.exception))

    def test_a_record_without_the_field_fails_loudly_not_silently(self):
        """Every record we write carries the field, so one that doesn't is
        corrupt. Nothing filters it out on that basis: it stays in the installed
        list, and the read raises where it's actually needed. The alternative -
        quietly dropping it - would report a mod the server really is running as
        not installed at all, and demote it to untracked in the launcher."""
        self._install("p.old", "1.0.0")
        record_path = mm._mod_record_path(self.game, "p.old")
        with io.open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        del record["parity_required"]
        with io.open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f)

        self.assertEqual([m["id"] for m in mm.list_installed_mods(self.game)], ["p.old"])
        with self.assertRaises(mm.ModManagerError) as caught:
            mm.handshake_snapshot(self.game)
        self.assertIn("parity_required", str(caught.exception))
        # Still a managed mod, not something the untracked lister picks up.
        self.assertEqual(mm.list_untracked_mods(self.game), [])

    def test_an_index_entry_without_the_field_is_skipped(self):
        d = {"name": "M", "author": "a", "description": "",
             "client_side": True, "server_side": True, "versions": ["1.0.0"]}
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
        plan = mm.plan_join(self.game, [self._srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])], [self.BASE])
        self.assertEqual([(e.mod_id, e.reason) for e in plan.entries],
                         [("p.rec", "recommended")])
        self.assertEqual(plan.declined, [])

    def test_declining_leaves_an_optional_mod_out_of_the_plan(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        plan = mm.plan_join(self.game, [self._srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])], [self.BASE],
                            declined=["p.rec"])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.declined, [("p.rec", "1.0.0")])

    def test_declining_a_required_mod_is_ignored(self):
        """No client-side choice can make that join work, so pretending it can
        would just move the rejection to the server."""
        self._publish("p.req", "1.0.0")
        plan = mm.plan_join(self.game, [self._srv("p.req", "1.0.0", required=True)],
                            [summary("p.req", ["1.0.0"])], [self.BASE],
                            declined=["p.req"])
        self.assertEqual([e.mod_id for e in plan.entries], ["p.req"])
        self.assertEqual(plan.declined, [])

    def test_declining_never_deactivates_something_already_installed(self):
        """Declining is 'don't add it', not 'take it away'."""
        self._install("p.rec", "1.0.0", parity_required=False)
        plan = mm.plan_join(self.game, [self._srv("p.rec", "1.0.0", required=False)],
                            [summary("p.rec", ["1.0.0"])], [self.BASE],
                            declined=["p.rec"])
        self.assertEqual(plan.to_deactivate, [])
        self.assertEqual(plan.entries, [])

    def test_an_unresolvable_optional_mod_does_not_block(self):
        """Required and unresolvable blocks the join; recommended just doesn't
        happen, since the server allows a client not to have it."""
        plan = mm.plan_join(self.game, [self._srv("p.gone", "1.0.0", required=False)],
                            [], [self.BASE])
        self.assertFalse(plan.blocking)
        self.assertEqual(plan.missing, [])
        self.assertEqual(plan.entries, [])

    def test_an_unresolvable_required_mod_still_blocks(self):
        plan = mm.plan_join(self.game, [self._srv("p.gone", "1.0.0", required=True)],
                            [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [("p.gone", "1.0.0")])

    def test_a_server_mod_with_no_parity_field_is_still_required(self):
        plan = mm.plan_join(self.game, [self._srv("p.old", "1.0.0")], [], [self.BASE])
        self.assertTrue(plan.blocking)

    # -- the comparison ------------------------------------------------------

    def test_recommended_rows_are_the_users_call_required_ones_are_not(self):
        self._publish("p.rec", "1.0.0", parity_required=False)
        self._publish("p.req", "1.0.0")
        server_mods = [self._srv("p.rec", "1.0.0", required=False),
                       self._srv("p.req", "1.0.0", required=True)]
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
        server_mods = [self._srv("p.rec", "1.0.0", required=False)]
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
        server_mods = [self._srv("p.rec", "1.0.0", required=False)]
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
        self._saved_save_cfg = mm._save_cfg
        mm._save_cfg = saved.append
        self.addCleanup(lambda: setattr(mm, "_save_cfg", self._saved_save_cfg))
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

    def _install(self, mod_id, version, **over):
        self._publish(mod_id, version, **over)
        mm.install_mod_closure(self.game, summary(mod_id, [version]), [summary(mod_id, [version])],
                               [self.BASE], self.progress.append)

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
        plan = mm.import_modlist(modlist, [], [self.BASE])
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
        plan = mm.import_modlist(modlist, [], [self.BASE])

        self.assertFalse(plan.blocking)
        self.assertEqual(plan.untracked, [{"name": "Manual.dll", "kind": "file"}])
        # Display only: it never becomes something to install, and never counts
        # as unresolved either - there was nothing to resolve in the first place.
        self.assertEqual([m.id for m in plan.roots], ["lat.m"])
        self.assertEqual(plan.unresolved, [])

    def test_an_untracked_block_never_blocks_an_import(self):
        modlist = {"schema": 1, "repos": [], "mods": [],
                   "untracked": [{"name": "Manual.dll", "kind": "file"}]}
        plan = mm.import_modlist(modlist, [], [self.BASE])
        self.assertFalse(plan.blocking)

    def test_a_modlist_with_no_untracked_key_still_imports(self):
        """Every modlist written before the key existed, and every hand-written
        one, has to keep working."""
        self._publish_latest("lat.m", "2.0.0")
        plan = mm.import_modlist({"schema": 1, "repos": [], "mods": ["lat.m"]},
                                 [], [self.BASE])
        self.assertEqual(plan.untracked, [])

    def test_importing_never_disables_the_importers_own_untracked_mods(self):
        """The invariant: nothing automatic touches an untracked mod. A modlist
        that doesn't mention your hand-installed mod is not a request to remove
        it - only `mods` describes an active set anyone can act on."""
        self._publish_latest("lat.m", "2.0.0")
        self._install("mine.m", "1.0.0")
        self._untracked("Manual.dll")

        plan = mm.import_modlist({"schema": 1, "repos": [], "mods": ["lat.m"]},
                                 [], [self.BASE])
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
        plan = mm.import_modlist(exported, [], [self.BASE])
        self.assertEqual([u["name"] for u in plan.untracked], ["Manual.dll"])

    def test_import_resolves_pinned_exact_version(self):
        self._publish("pin.m", "1.0.0")
        self._publish("pin.m", "2.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["pin.m@1.0.0"]}
        plan = mm.import_modlist(modlist, [], [self.BASE])
        self.assertEqual([(m.id, m.version) for m in plan.roots], [("pin.m", "1.0.0")])

    def test_import_pulls_in_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE])
        self.assertEqual([m.id for m in plan.roots], ["root.m"])
        self.assertEqual([m.id for m in plan.dependencies], ["dep.k"])

    def test_import_unresolved_entry_blocks(self):
        modlist = {"schema": 1, "repos": ["Some/Pack"], "mods": ["ghost.m@1.0.0"]}
        plan = mm.import_modlist(modlist, [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.unresolved, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.hinted_repos, ["Some/Pack"])

    def test_import_rejects_unsupported_schema(self):
        modlist = {"schema": 2, "repos": [], "mods": []}
        with self.assertRaises(mm.ModManagerError):
            mm.import_modlist(modlist, [], [self.BASE])

    def test_apply_import_installs_roots_and_dependencies(self):
        self._publish("dep.k", "1.0.0")
        self._publish_latest("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        index = [summary("dep.k", ["1.0.0"])]
        modlist = {"schema": 1, "repos": [], "mods": ["root.m"]}
        plan = mm.import_modlist(modlist, index, [self.BASE])

        mm.apply_import(self.game, plan, self.progress.append)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "root.m", "root.m.dll")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dep.k", "dep.k.dll")))

    def test_apply_import_disables_installed_mods_not_in_list(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        self._publish_latest("keep.m", "1.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["keep.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE])

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
        plan = mm.import_modlist(modlist, index, [self.BASE])

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["dep.k"])

    def test_apply_import_leaves_already_disabled_mods_alone(self):
        self._install("dis.m", "1.0.0")
        mm.disable_mod(self.game, "dis.m")
        modlist = {"schema": 1, "repos": [], "mods": []}
        plan = mm.import_modlist(modlist, [], [self.BASE])

        mm.apply_import(self.game, plan, self.progress.append)

        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertFalse(installed["dis.m"])

    def test_modlist_import_disables_previews_without_side_effects(self):
        self._install("keep.m", "1.0.0")
        self._install("drop.m", "1.0.0")
        self._publish_latest("keep.m", "1.0.0")
        modlist = {"schema": 1, "repos": [], "mods": ["keep.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE])

        self.assertEqual(mm.modlist_import_disables(self.game, plan), ["drop.m"])
        installed = {r["id"]: r["enabled"] for r in mm.list_installed_mods(self.game)}
        self.assertTrue(installed["drop.m"])

    def test_apply_import_blocking_plan_raises_without_installing(self):
        modlist = {"schema": 1, "repos": [], "mods": ["ghost.m"]}
        plan = mm.import_modlist(modlist, [], [self.BASE])
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
        plan = mm.import_modlist(exported, [], [self.BASE])
        self.assertFalse(plan.blocking)
        self.assertEqual([m.id for m in plan.roots], ["rt.m"])


class ZipBundleInstall(unittest.TestCase):
    """package="zip" mods: archive is downloaded, verified, and extracted into
    Mods/<id>/. Zip-slip / symlink / absolute-path members are rejected."""
    BASE = DEFAULT

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="tavern_game_")
        self.progress = []
        self.manifests = {}
        self.zip_bytes = {}          # download_url -> raw zip bytes

        def fake_get_json(url, timeout=15):
            if url in self.manifests:
                return self.manifests[url]
            raise mm.ModManagerError(f"404 {url}")
        mm._get_json = fake_get_json

        def fake_download(url, dest, on_progress=None):
            with open(dest, "wb") as f:
                f.write(self.zip_bytes[url])
            if on_progress:
                on_progress("downloaded")

        def fake_hash(path):
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        mm.set_helpers(types.SimpleNamespace(_download_with_progress=fake_download,
                                             _sha256_file=fake_hash))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.game, ignore_errors=True))

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
                               [self.BASE], self.progress.append)

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
        saved = mm._ZIP_MAX_TOTAL_UNCOMPRESSED
        mm._ZIP_MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024
        self.addCleanup(lambda: setattr(mm, "_ZIP_MAX_TOTAL_UNCOMPRESSED", saved))
        self._publish_zip("bomb.m", "1.0.0", [
            ("mod.dll", b"MZ"),
            ("big.bin", b"\0" * (8 * 1024 * 1024)),   # 8 MB of zeros, > 4 MB cap
        ])
        with self.assertRaises(mm.ModManagerError):
            self._install("bomb.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "bomb.m")))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".bomb.m.installing")))

    def test_zip_too_many_entries_aborts(self):
        saved = mm._ZIP_MAX_ENTRIES
        mm._ZIP_MAX_ENTRIES = 5
        self.addCleanup(lambda: setattr(mm, "_ZIP_MAX_ENTRIES", saved))
        self._publish_zip("many.m", "1.0.0",
                          [(f"f{i}.dat", b"x") for i in range(10)])
        with self.assertRaises(mm.ModManagerError):
            self._install("many.m")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "many.m")))


class SetHelpersHostHandoff(unittest.TestCase):
    """set_helpers(host) copies attributes the host exposes into modmanager's
    like-named globals, renaming load_cfg/save_cfg to _load_cfg/_save_cfg.
    Each launcher calls this at startup with its own module object."""

    def setUp(self):
        # Snapshot every global set_helpers can touch and restore it after,
        # so wiring here doesn't leak into other tests.
        self._saved = {dst: getattr(mm, dst) for _src, dst in mm._HOST_ATTRS}
        self.addCleanup(
            lambda: [setattr(mm, dst, val) for dst, val in self._saved.items()])

    def test_full_host_populates_every_global(self):
        host = types.SimpleNamespace(
            _download_with_progress="DL", _sha256_file="HASH",
            load_cfg="LOAD", save_cfg="SAVE",
            BG="#bg", SURF="#surf", BORDER="#border", AMBER="#amber",
            AMBERDIM="#amberdim", PARCH="#parch", MUTED="#muted", GREEN="#green",
            RED="#red", CYAN="#cyan",
            _btn="BTN", _mk_tree="TREE", _mk_scrollbar="SCROLL",
            _enable_dark_titlebar="DARK",
        )
        mm.set_helpers(host)
        self.assertEqual(mm._download_with_progress, "DL")
        self.assertEqual(mm._sha256_file, "HASH")
        # public on the host, private here
        self.assertEqual(mm._load_cfg, "LOAD")
        self.assertEqual(mm._save_cfg, "SAVE")
        self.assertEqual(mm.BG, "#bg")
        self.assertEqual(mm.AMBERDIM, "#amberdim")
        self.assertEqual(mm.CYAN, "#cyan")
        self.assertEqual(
            (mm._btn, mm._mk_tree, mm._mk_scrollbar, mm._enable_dark_titlebar),
            ("BTN", "TREE", "SCROLL", "DARK"))

    def test_partial_host_leaves_unmentioned_globals_untouched(self):
        mm.BG = "#keep"
        mm._save_cfg = "keep_save"
        mm.set_helpers(types.SimpleNamespace(_download_with_progress="only_dl"))
        self.assertEqual(mm._download_with_progress, "only_dl")
        self.assertEqual(mm.BG, "#keep")            # not on host -> unchanged
        self.assertEqual(mm._save_cfg, "keep_save")  # not on host -> unchanged

    def test_a_real_module_is_a_valid_host(self):
        # The launcher passes sys.modules[__name__]; a plain module works the same.
        host = types.ModuleType("fake_host")
        host._sha256_file = "modhash"
        host.load_cfg = "modload"
        mm.set_helpers(host)
        self.assertEqual(mm._sha256_file, "modhash")
        self.assertEqual(mm._load_cfg, "modload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
