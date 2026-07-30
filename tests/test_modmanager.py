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


def slim_entry(versions, name="M", author="a", description="", client=True, server=True):
    return {"name": name, "author": author, "description": description,
            "client_side": client, "server_side": server, "versions": list(versions)}


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
        mm._local_av_scan = lambda p: None
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


class HandshakeSnapshot(_FakeInstallFixture, unittest.TestCase):
    """handshake_snapshot(game_dir) - the ping/pong mod-sync fingerprint.
    Reuses the fake download/hash wiring to actually install mods, then
    checks the snapshot it produces."""

    def test_empty_mods_dir_gives_empty_snapshot(self):
        mods_hash, count, entries = mm.handshake_snapshot(self.game)
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

        mods_hash, count, entries = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 2)
        self.assertEqual([e["id"] for e in entries], ["a.a", "z.z"])
        self.assertEqual(entries[0]["version"], "2.1.0")
        self.assertTrue(entries[0]["client_side"])
        self.assertTrue(entries[0]["server_side"])

        expected = hashlib.sha256(b"a.a@2.1.0\nz.z@1.0.0").hexdigest()
        self.assertEqual(mods_hash, expected)

    def test_disabled_mod_excluded_from_snapshot(self):
        self._publish("d.d", "1.0.0")
        mm.install_mod_closure(self.game, summary("d.d", ["1.0.0"]), [summary("d.d", ["1.0.0"])],
                               [self.BASE], self.progress.append)
        mm.disable_mod(self.game, "d.d")

        mods_hash, count, entries = mm.handshake_snapshot(self.game)
        self.assertEqual(count, 0)
        self.assertEqual(entries, [])
        self.assertEqual(mods_hash, hashlib.sha256(b"").hexdigest())

    def test_hash_changes_when_a_version_changes(self):
        self._publish("v.v", "1.0.0")
        mm.install_mod_closure(self.game, summary("v.v", ["1.0.0"]), [summary("v.v", ["1.0.0"])],
                               [self.BASE], self.progress.append)
        before, _, _ = mm.handshake_snapshot(self.game)

        self._publish("v.v", "1.1.0")
        mm.install_mod_closure(self.game, summary("v.v", ["1.1.0"]), [summary("v.v", ["1.1.0"])],
                               [self.BASE], self.progress.append)
        after, _, _ = mm.handshake_snapshot(self.game)

        self.assertNotEqual(before, after)


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
        server_mods = [{"id": "req.m", "version": "1.0.0", "client_side": True, "server_side": True}]
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
        server_mods = [{"id": "srv.m", "version": "1.0.0", "client_side": False, "server_side": True}]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.missing, [])

    def test_dependency_pulled_in_with_reason_dependency(self):
        self._publish("dep.k", "1.0.0")
        self._publish("root.m", "1.0.0", dependencies={"dep.k": "1.0.0"})
        server_mods = [{"id": "root.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        index = [summary("dep.k", ["1.0.0"]), summary("root.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual({e.mod_id: e.reason for e in plan.entries},
                         {"root.m": "required", "dep.k": "dependency"})

    def test_missing_required_mod_blocks_with_no_hint(self):
        server_mods = [{"id": "ghost.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertTrue(plan.blocking)
        self.assertEqual(plan.missing, [("ghost.m", "1.0.0")])
        self.assertEqual(plan.needs_repo, [])

    def test_needs_repo_when_hint_names_unadded_repo_not_blocking(self):
        server_mods = [{"id": "elsewhere.m", "version": "1.0.0", "client_side": True,
                        "server_side": True, "source_repo": self.OTHER}]
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
        server_mods = [{"id": "conf.m", "version": "2.0.0", "client_side": True, "server_side": True}]
        index = [summary("conf.m", ["1.0.0", "2.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE], pinned=["conf.m@1.0.0"])
        self.assertEqual(plan.pin_conflicts, [("conf.m", "1.0.0", "2.0.0")])
        # The server's exact required version wins the single active entry -
        # the pin conflict is surfaced, not silently overridden or duplicated.
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(self._entry(plan, "conf.m").version, "2.0.0")

    def test_active_true_when_already_installed_at_matching_version(self):
        self._install("act.m", "1.0.0")
        server_mods = [{"id": "act.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        index = [summary("act.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        entry = self._entry(plan, "act.m")
        self.assertTrue(entry.active)

    def test_cached_true_when_version_cached_but_not_currently_active(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m")
        mm.uninstall_mod(self.game, "cch.m")
        server_mods = [{"id": "cch.m", "version": "1.0.0", "client_side": True, "server_side": True}]
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
        server_mods = [{"id": "root.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        index = [summary("root.m", ["1.0.0"])]

        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        self.assertEqual([l.filename for l in plan.libraries], ["PlanLib.dll"])


class RenderActiveSet(_FakeInstallFixture, unittest.TestCase):
    """render_active_set (4d): applying a JoinPlan to disk. Same throwaway
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
        server_mods = [{"id": "dl.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        index = [summary("dl.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])

        mm.render_active_set(self.game, plan)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "dl.m", "dl.m.dll")))
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("dl.m", "1.0.0")))

    def test_restores_from_cache_without_redownload(self):
        self._install("cch.m", "1.0.0")
        mm.cache_store_mod(self.game, "cch.m")
        mm.uninstall_mod(self.game, "cch.m")

        server_mods = [{"id": "cch.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        index = [summary("cch.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan)
        self.assertEqual(len(self.dl_dirs), downloads_before)   # no new download
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "cch.m", "cch.m.dll")))

    def test_already_active_entry_is_left_alone(self):
        self._install("act.m", "1.0.0")
        server_mods = [{"id": "act.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        index = [summary("act.m", ["1.0.0"])]
        plan = mm.plan_join(self.game, server_mods, index, [self.BASE])
        downloads_before = len(self.dl_dirs)

        mm.render_active_set(self.game, plan)
        self.assertEqual(len(self.dl_dirs), downloads_before)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "act.m", "act.m.dll")))

    def test_deactivated_mod_removed_from_mods_but_stays_cached(self):
        self._install("old.m", "1.0.0")
        plan = mm.plan_join(self.game, [], [], [self.BASE])   # nothing required -> deactivate everything

        mm.render_active_set(self.game, plan)
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "old.m")))
        self.assertTrue(os.path.isdir(mm._cache_mod_dir("old.m", "1.0.0")))

        # Recoverable later with no re-download.
        mm.cache_restore_mod(self.game, "old.m", "1.0.0")
        self.assertTrue(os.path.isfile(os.path.join(self.game, "Mods", "old.m", "old.m.dll")))

    def test_library_downloaded_then_orphan_pruned_after_deactivation(self):
        lib_url = "https://x/Prune.dll"
        lib = {"name": "Prune", "download_url": lib_url, "sha256": "", "filename": "Prune.dll"}
        self._publish("lib.m", "1.0.0", library_dependencies=[dict(lib)])
        server_mods = [{"id": "lib.m", "version": "1.0.0", "client_side": True, "server_side": True}]
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
        server_mods = [{"id": "lib2.m", "version": "1.0.0", "client_side": True, "server_side": True}]
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
        server_mods = [{"id": "ghost.m", "version": "1.0.0", "client_side": True, "server_side": True}]
        plan = mm.plan_join(self.game, server_mods, [], [self.BASE])
        self.assertTrue(plan.blocking)
        with self.assertRaises(mm.ModManagerError):
            mm.render_active_set(self.game, plan)
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods")))


class ResolveMissingMods(_FakeInstallFixture, unittest.TestCase):
    """resolve_missing_mods: turns a rejection payload's `missing` entries
    into installable manifests, via the client's own configured repos
    only; source_repo is never trusted directly (principle 14)."""

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
        mm._local_av_scan = lambda p: None
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


class LocalAvScan(unittest.TestCase):
    def test_scan_disabled_via_cfg_returns_none_without_running(self):
        saved = mm._load_cfg
        try:
            mm._load_cfg = lambda: {mm.CFG_SCAN_KEY: False}
            self.assertIsNone(mm._local_av_scan("does-not-exist.dll"))
        finally:
            mm._load_cfg = saved


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
            _btn="BTN", _mk_tree="TREE", _enable_dark_titlebar="DARK",
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
        self.assertEqual((mm._btn, mm._mk_tree, mm._enable_dark_titlebar),
                         ("BTN", "TREE", "DARK"))

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
