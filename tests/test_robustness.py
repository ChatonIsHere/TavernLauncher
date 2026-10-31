"""Edge cases the mod manager must survive: a folder in use, a host that plays
and serves from one folder, broken source data, a source that's down, and
replacements that point at each other."""
import contextlib
import io
import json
import os
import shutil
import sys
import types
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from conftest import stub, temp_dir
from test_modmanager import (
    DEFAULT, _TempDataDirFixture, _srv, lib_dep, make_manifest, manifest_dict, mm,
    noop, resolve_deps, slim_entry, slim_index, store, summary,
)
from tavern_shared import launcher_common, paths
from tavern_shared.mods import base, install, repos, resolve, ui


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


def write_record(game, mod_id, **over):
    """An owned server record written straight from the spec."""
    d = os.path.join(game, "Mods", mod_id)
    os.makedirs(os.path.join(d, ".server"), exist_ok=True)
    rec = {"manifest_version": 2, "id": mod_id, "name": mod_id, "version": "1.0.0",
           "parity_required": True, "dependencies": {}, "artifacts": [],
           "source_repo": DEFAULT, "installed": {"server": {"sha256": "0" * 64,
                                                          "libraries": [], "files": {}}}}
    rec.update(over)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)


class Placement(_TempDataDirFixture, unittest.TestCase):
    def _both(self, mod_id, version="1.0.0"):
        self._publish(mod_id, version)
        self._closure(mod_id, version, target="client_windows")
        self._closure(mod_id, version, target="server")

    def test_another_version_is_refused_while_the_other_target_is_in_use(self):
        """A running server holds .server/ open: nothing changes, nothing is lost."""
        self._both("lock.m")
        self._publish("lock.m", "2.0.0")
        real = os.replace

        def replace(src, dst):
            if src.endswith(os.sep + ".server"):
                raise PermissionError(13, "in use")
            real(src, dst)
        stub(self, install.os, "replace", replace)
        with self.assertRaisesRegex(mm.ModManagerError, "in use"):
            self._closure("lock.m", "2.0.0", target="client_windows")
        rec = self._record("lock.m")
        self.assertEqual((rec["version"], mm.installed_targets(rec)),
                         ("1.0.0", ["client_windows", "server"]))
        for target in ("client_windows", "server"):
            self.assertIs(mm.verify_mod_files(self.game, "lock.m", target), True)

    def test_data_a_mod_keeps_beside_its_build_survives_an_update(self):
        self._install("data.m")
        kept = os.path.join(self.game, "Mods", "data.m", "UserData", "key.xml")
        os.makedirs(os.path.dirname(kept))
        open(kept, "wb").close()
        self._publish("data.m", "2.0.0")
        self._closure("data.m", "2.0.0")
        self.assertEqual(self._record("data.m")["version"], "2.0.0")
        self.assertTrue(os.path.isfile(kept))

    def test_a_pre_v2_layouts_files_are_replaced(self):
        write_record(self.game, "old.m", installed=None)
        loose = os.path.join(self.game, "Mods", "old.m", "Old.dll")
        open(loose, "wb").close()
        self._install("old.m")
        self.assertFalse(os.path.exists(loose))
        self.assertEqual(sorted(os.listdir(os.path.join(self.game, "Mods", "old.m"))),
                         [".client_windows", "manifest.json"])

    def test_a_copied_folder_is_untracked(self):
        self._install("a.m")
        shutil.copytree(os.path.join(self.game, "Mods", "a.m"),
                        os.path.join(self.game, "Mods", "a.m - Copy"))
        installed, untracked = mm._scan_mods(self.game)
        self.assertEqual([r["id"] for r in installed], ["a.m"])
        self.assertEqual([u["name"] for u in untracked], ["a.m - Copy"])

    def test_an_unusable_record_is_warned_about_once(self):
        write_record(self.game, "old.m")
        rec = os.path.join(self.game, "Mods", "old.m", "manifest.json")
        with open(rec, encoding="utf-8") as f:
            data = json.load(f)
        del data["parity_required"]
        with open(rec, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with quiet() as err:
            for _ in range(3):
                mm._scan_mods(self.game)
        self.assertEqual(err.getvalue().count("unusable record"), 1)

    def test_an_unreadable_record_is_listed_but_never_displaced(self):
        self._install("a.m")
        with mock.patch.object(install, "open", side_effect=PermissionError(13, "locked"),
                               create=True):
            installed, untracked = mm._scan_mods(self.game)
            with self.assertRaises(PermissionError):
                mm._displace_foreign(self.game, "a.m")
        self.assertEqual(([r["id"] for r in installed], [u["name"] for u in untracked]),
                         ([], ["a.m"]))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", ".displaced")))

    def test_a_download_temp_that_cant_be_removed_fails_the_install(self):
        url = "https://x/a.dll"
        real = os.remove

        def remove(path):
            if path.endswith(".part"):
                raise PermissionError(13, "in use")
            real(path)
        stub(self, install.os, "remove", remove)
        with self.assertRaises(PermissionError):
            with mm._verified_download(url, self._sha_for(url), self.game, None):
                pass

    def test_a_loose_dll_that_cant_be_renamed_is_only_a_warning(self):
        loose = os.path.join(self.game, "Mods", "a.m.dll")
        os.makedirs(os.path.dirname(loose))
        open(loose, "wb").close()

        def locked(*_a):
            raise PermissionError(13, "in use")
        stub(self, install, "disable_untracked_dll", locked)
        with quiet() as err:
            self._install("a.m")
        self.assertIn("couldn't be disabled", err.getvalue())
        self.assertIs(mm.verify_mod_files(self.game, "a.m", self.TARGET), True)

    def test_names_ending_in_a_dot_or_space_are_refused(self):
        for name in ("a.dll.", "a.dll ", "a.b."):
            with self.assertRaises(mm.ModManagerError):
                mm._safe_basename(name)
        path = os.path.join(self.game, "bad.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("sub./A.dll", b"MZ")
        with self.assertRaisesRegex(mm.ModManagerError, "dot or space"):
            mm._safe_extract_zip(path, temp_dir(self))


class SharedFolder(_TempDataDirFixture, unittest.TestCase):
    """One game folder, both launchers: the client never switches off what
    the host's own server runs."""

    def setUp(self):
        super().setUp()
        self._publish("both.m", "1.0.0")
        self._closure("both.m", target="client_windows")
        self._closure("both.m", target="server")
        self._install("solo.m")

    def test_a_join_leaves_the_servers_mods_on(self):
        plan = self._plan([], [])
        self.assertEqual((plan.to_deactivate, plan.shared), (["solo.m"], ["both.m"]))
        row = next(r for r in mm.build_mod_diff(self.game, [], plan) if r.mod_id == "both.m")
        self.assertEqual((row.action, row.optional), (mm.ACTION_MATCH, False))

    def test_a_client_modlist_import_leaves_them_on_too(self):
        plan = mm.ImportPlan(roots=[], dependencies=[], unresolved=[], hinted_repos=[],
                             untracked=[], target="client_windows", no_build=[])
        self.assertEqual(mm.modlist_import_disables(self.game, plan), ["solo.m"])
        plan.target = "server"
        self.assertEqual(mm.modlist_import_disables(self.game, plan), ["both.m"])


class JoinPlanning(_TempDataDirFixture, unittest.TestCase):
    def test_a_pin_or_recommendation_with_a_broken_closure_is_left_out(self):
        self._publish("pin.m", "1.0.0", dependencies={"gone.m": "1.0.0"})
        self._publish("rec.m", "1.0.0", dependencies={"gone.m": "1.0.0"})
        index = [summary("pin.m", ["1.0.0"]), summary("rec.m", ["1.0.0"])]
        with quiet():
            plan = self._plan([_srv("rec.m", "1.0.0", required=False)], index, pinned=["pin.m"])
        self.assertEqual((plan.entries, plan.blocking), ([], False))

    def test_a_required_mod_with_a_broken_closure_still_fails(self):
        self._publish("req.m", "1.0.0", dependencies={"gone.m": "1.0.0"})
        with self.assertRaises(mm.ModManagerError):
            self._plan([_srv("req.m", "1.0.0")], [summary("req.m", ["1.0.0"])])

    def test_an_intact_match_needs_no_source(self):
        self._install("on.m")
        self.manifests.clear()
        resolve._manifest_memo.clear()
        plan = self._plan([_srv("on.m", "1.0.0")], [])
        self.assertEqual([(e.mod_id, e.active) for e in plan.entries], [("on.m", True)])
        self.assertFalse(plan.blocking)

    def test_a_damaged_match_still_needs_one(self):
        self._install("on.m")
        self.manifests.clear()
        resolve._manifest_memo.clear()
        with open(self._mod_file("on.m", "on.m.dll"), "wb") as f:
            f.write(b"changed")
        install._verify_memo.clear()
        self.assertEqual(self._plan([_srv("on.m", "1.0.0")], []).missing, [("on.m", "1.0.0")])

    def test_a_pinned_replacement_of_a_server_mod_must_go(self):
        self._publish("old.m", "1.0.0")
        self._install("new.m", "2.0.0")
        index = mm.ModIndex([summary("new.m", ["2.0.0"])],
                            [mm.Replacement("old.m", "new.m", "", DEFAULT)])
        with quiet():
            plan = self._plan([_srv("old.m", "1.0.0")], index, pinned=["new.m"])
        self.assertEqual(plan.forced_off, {"new.m": "old.m"})
        row = next(r for r in mm.build_mod_diff(self.game, [], plan) if r.mod_id == "new.m")
        self.assertEqual((row.action, row.optional), (mm.ACTION_DEACTIVATE, False))

    def test_libraries_come_from_the_entries_kept(self):
        self._publish("rec.m", "1.0.0", library_dependencies=[lib_dep("L")])
        plan = self._plan([_srv("rec.m", "1.0.0", required=False)], [summary("rec.m", ["1.0.0"])])
        self.assertEqual([l.filename for l in mm._plan_libraries(plan)], ["L.dll"])
        plan.entries = []
        self.assertEqual((mm._plan_libraries(plan), mm.render_plan_steps(plan)), ([], 0))


class Replacements(_TempDataDirFixture, unittest.TestCase):
    def _root_needing(self, dep_id):
        self._publish("root.m", "1.0.0", dependencies={dep_id: "1.0.0"})
        mm.install_mod_set(self.game, [mm._fetch_manifest([self.BASE], "root.m", "1.0.0")],
                           self.TARGET, noop)

    def _missing(self, index):
        installed = mm.list_installed_mods(self.game)
        rec = next(r for r in installed if r["id"] == "root.m")
        return mm.missing_dependencies(rec, installed, index, self.TARGET)

    def test_a_dependency_on_a_replaced_mod_is_met_by_its_replacement(self):
        self._install("new.m", "2.0.0")
        self._root_needing("old.m")
        index = mm.ModIndex([summary("new.m", ["2.0.0"]), summary("root.m", ["1.0.0"])],
                            [mm.Replacement("old.m", "new.m", "", DEFAULT)])
        self.assertEqual(self._missing(index), [])
        mm.disable_mod(self.game, "new.m")
        self.assertEqual(self._missing(index), [("new.m", None, "disabled")])

    def test_a_replacement_that_is_replaced_too_retires_nothing(self):
        self._install("old.m")
        self._install("new.m", "2.0.0")
        index = mm.ModIndex([summary("newer.m", ["3.0.0"])],
                            [mm.Replacement("old.m", "new.m", "", DEFAULT),
                             mm.Replacement("new.m", "old.m", "", DEFAULT)])
        self.assertEqual(mm.retire_replaced(self.game, index, noop), [])
        self.assertEqual(sorted(r["id"] for r in mm.list_installed_mods(self.game)),
                         ["new.m", "old.m"])

    def test_a_record_with_no_targets_is_never_retired(self):
        write_record(self.game, "old.m", installed={})
        self._install("new.m", "2.0.0")
        index = mm.ModIndex([summary("new.m", ["2.0.0"])],
                            [mm.Replacement("old.m", "new.m", "", DEFAULT)])
        self.assertEqual(mm.retire_replaced(self.game, index, noop), [])


class SourceData(_TempDataDirFixture, unittest.TestCase):
    """What an unreviewed source can serve, read the way TavernLib reads it."""

    def test_string_fields_refuse_other_types(self):
        self.assertEqual(make_manifest("a.m", "1.0.0", name=None).name, "a.m")
        for field, value in (("name", 5), ("version", 1.0), ("description", {}), ("id", None)):
            with self.assertRaises(mm.ModManagerError, msg=field):
                mm._manifest_from_dict(dict(manifest_dict("a.m", "1.0.0"), **{field: value}),
                                       DEFAULT)
        lib = dict(lib_dep("L"), name=5, sha256="0" * 64)
        with self.assertRaises(mm.ModManagerError):
            make_manifest("a.m", "1.0.0", library_dependencies=[lib])

    def test_dependencies_must_map_ids_to_version_strings(self):
        for deps in ({"x.y": 1}, "x.y", [["x.y", "1.0.0"]], [], "", False):
            with self.assertRaises(mm.ModManagerError, msg=repr(deps)):
                make_manifest("a.m", "1.0.0", dependencies=deps)
        self.assertEqual(make_manifest("a.m", "1.0.0", dependencies=None).dependencies, {})

    def test_a_manifest_that_isnt_an_object_is_skipped(self):
        with quiet():
            self.assertIsNone(mm._manifest_from_dict(["no"], DEFAULT))

    def test_a_malformed_index_lists_nothing_and_raises_nothing(self):
        for served in (slim_index([1, 2]), slim_index({"a.m": {"1": "x"}}),
                       {"index_version": 2.0, "mods": {"a.m": {"1": slim_entry(["1.0.0"])}}}):
            repos._index_cache.clear()
            self.manifests[f"{self.BASE}/repository.json"] = served
            with quiet():
                self.assertEqual(list(mm.fetch_indexes([self.BASE], force=True)), [])
        repos._index_cache.clear()

    def test_an_unsafe_id_is_refused_before_any_fetch(self):
        for mod_id in ("a.b/c", "a.b.", "..x"):
            with self.assertRaises(mm.ModManagerError, msg=mod_id):
                mm._fetch_manifest([self.BASE], mod_id, "1.0.0")

    def test_latest_naming_another_mod_fails_that_repo(self):
        self.manifests[f"{self.BASE}/manifests/a/m/latest.json"] = manifest_dict("b.m", "1.0.0")
        with self.assertRaisesRegex(mm.ModManagerError, "not the requested"):
            mm.resolve_mod_by_id([self.BASE], "a.m")

    def test_a_preferred_repo_outside_the_sources_is_never_fetched(self):
        urls = []
        stub(self, repos, "_get_json", lambda url, timeout=15: urls.append(url) or
             manifest_dict("a.m", "1.0.0"))
        mm._fetch_manifest([self.BASE], "a.m", "1.0.0", prefer_repo="https://evil.example")
        self.assertEqual(urls, [f"{self.BASE}/manifests/a/m/1.0.0.json"])

    def test_the_dll_name_is_percent_decoded(self):
        art = mm.Artifact(targets=["server"], download_url="https://x/v1/My%20Mod.dll",
                          sha256="0" * 64, package="dll", library_dependencies=[])
        self.assertEqual(art.install_name("a.m"), "My Mod.dll")
        art.download_url = "https://x/v1/sub%2FB.dll"
        self.assertEqual(art.install_name("a.m"), "B.dll")

    def test_an_empty_pin_version_means_latest(self):
        self.assertEqual(mm._parse_pin_entry("a.m@"), ("a.m", None))


class Fetching(unittest.TestCase):
    def test_json_over_8_mb_is_refused(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self, n):
                return b" " * n
        with mock.patch.object(repos.urllib.request, "urlopen", lambda *a, **k: Resp()):
            with self.assertRaisesRegex(mm.ModManagerError, "MB"):
                repos._get_json("https://x/repository.json")


class Resolution(unittest.TestCase):
    def test_a_root_is_never_swapped_for_another_version(self):
        a, a2 = make_manifest("a.m", "1.0.0"), make_manifest("a.m", "1.5.0")
        b = make_manifest("b.m", "1.0.0", dependencies={"a.m": "1.0.0"})
        index = [summary("a.m", ["1.0.0", "1.5.0"]), summary("b.m", ["1.0.0"])]
        fetched = []
        self.assertEqual(resolve_deps(self, [a, b], index, store(a, a2, b, fetched=fetched),
                                      "server"), [])
        self.assertEqual(fetched, [])
        b = make_manifest("b.m", "1.0.0", dependencies={"a.m": "1.2.0"})
        with self.assertRaisesRegex(mm.ModManagerError, "being installed"):
            resolve_deps(self, [a, b], index, store(a, a2, b), "server")

    def test_a_replaced_dependency_leading_back_to_the_root_is_a_cycle(self):
        r = make_manifest("r.m", "1.0.0", dependencies={"old.m": "1.0.0"})
        index = mm.ModIndex([summary("r.m", ["1.0.0"])],
                            [mm.Replacement("old.m", "r.m", "", DEFAULT)])
        with quiet(), self.assertRaisesRegex(mm.ModManagerError, "Circular"):
            resolve_deps(self, [r], index, store(r), "server")


class Handshake(unittest.TestCase):
    def setUp(self):
        self.game = temp_dir(self)

    def test_ids_sort_by_utf16_code_unit_as_in_tavernlib(self):
        for mod_id in ("x.m", "x\U0001f600.m"):
            write_record(self.game, mod_id)
        ids = [m["id"] for m in mm.handshake_snapshot(self.game, "server")[2]]
        self.assertEqual(ids, ["x\U0001f600.m", "x.m"])

    def test_an_untracked_stamp_truncates_the_nanoseconds(self):
        path = os.path.join(self.game, "Mods", "Loose.dll")
        os.makedirs(os.path.dirname(path))
        open(path, "wb").close()
        os.utime(path, ns=(1_700_000_000_999_999_900,) * 2)
        self.assertEqual(mm._untracked_stamp(self.game, {"name": "Loose.dll"}), "1700000000")

    def test_a_bad_artifact_costs_only_itself(self):
        good = {"targets": ["client_windows"], "download_url": "u", "package": "dll",
                "library_dependencies": []}
        rec = {"id": "a.m", "artifacts": [
            {"targets": ["server"], "download_url": "u", "sha256": "bad"},
            dict(good, sha256="a" * 64), dict(good, sha256="b" * 64)]}
        self.assertEqual(mm._record_targets(rec), (["client_windows"], {"client_windows": "a" * 64}))


class ServerMetadata(_TempDataDirFixture, unittest.TestCase):
    TARGET = "server"

    def test_parity_and_scenes_follow_the_manifest_in_place(self):
        self._install("a.m")
        self._publish("a.m", "1.0.0", parity_required=False, scenes=["quest"])
        resolve._manifest_memo.clear()
        self.assertEqual(mm.refresh_metadata(self.game, [self.BASE], "server"), ["a.m"])
        rec = self._record("a.m")
        keys = list(rec)
        self.assertEqual((rec["parity_required"], rec["scenes"]), (False, ["quest"]))
        self.assertEqual(keys[keys.index("parity_required") + 1], "scenes")
        self.assertIs(mm.verify_mod_files(self.game, "a.m", "server"), True)
        self.assertEqual(mm.refresh_metadata(self.game, [self.BASE], "server"), [])


class Windows(_TempDataDirFixture, unittest.TestCase):
    """Window methods bound onto a namespace, as test_mod_sync_flow does."""

    def _ns(self, **attrs):
        ns = types.SimpleNamespace(_game_dir=self.game, _target="client_windows", _index=[],
                                   _busy=False, _server_scene=None, statuses=[], **attrs)
        ns._status = types.SimpleNamespace(set=ns.statuses.append)
        ns._set_busy = lambda busy, msg="": ns.statuses.append(msg)
        for name in ("_install", "_confirm_install", "_retire", "_retired", "_finish"):
            setattr(ns, name, types.MethodType(getattr(ui.ModListWindow, name), ns))
        return ns

    def test_get_mods_asks_the_game_guard_once(self):
        asked = []
        stub(self, ui, "confirm_while_game_running", lambda *a: asked.append(a) or True)
        stub(self, ui, "_melonloader_installed", lambda d: True)
        stub(self, ui, "_tavernlib_installed", lambda d: True)
        stub(self, ui, "run_bg", lambda w, fn, on_ok=None, on_err=None: None)
        ns = self._ns(_is_game_running=lambda: True)
        ns._install(summary("a.m", ["1.0.0"]))
        self.assertEqual(asked, [])
        ns._confirm_install("a.m", make_manifest("a.m", "1.0.0"), [], [])
        self.assertEqual(len(asked), 1)

    def test_a_retire_failure_is_a_note_not_a_failed_install(self):
        def boom(*_a):
            raise OSError("locked")
        stub(self, ui, "retire_replaced", boom)
        ns = self._ns()
        self.assertEqual(ns._retire(noop), ([], " Couldn't remove the mod it replaces: locked"))
        cfg = {"pinned_mods": ["old.m"]}
        stub(self, ui, "load_cfg", lambda: cfg)
        self.assertIn("Removed old.m", ns._retired([("old.m", "new.m")], ""))
        self.assertEqual(cfg["pinned_mods"], ["new.m"])

    def test_the_badge_skips_disabled_mods(self):
        self._install("a.m")
        index = [summary("a.m", ["1.0.0", "2.0.0"])]
        stub(self, launcher_common, "fetch_indexes", lambda bases: index)
        stub(self, launcher_common, "list_repos", lambda cfg: [])
        stub(self, launcher_common, "load_cfg", lambda: {})
        self.assertEqual(launcher_common.count_mods_needing_attention(self.game, self.TARGET), 1)
        mm.disable_mod(self.game, "a.m")
        self.assertEqual(launcher_common.count_mods_needing_attention(self.game, self.TARGET), 0)

    def test_the_server_launcher_guards_its_mod_manager(self):
        from server.core.launcher_window import ServerLauncher
        ns = types.SimpleNamespace(_proc=None, _server_scene=lambda: "normal")
        self.assertIs(ServerLauncher._mod_manager_kwargs(ns)["is_game_running"](), False)

    def test_a_join_that_skips_the_sync_says_why(self):
        import client.core.launcher_window as lw
        from tests._tk_fakes import inline_run_bg
        printed, launched = [], []
        ns = types.SimpleNamespace(_print=lambda m, t="": printed.append(m),
                                   _set_sync_busy=noop, _report_server_untracked=noop,
                                   _build_mod_plan=lambda *a: ("skip", "No mod info."))
        ns._reconcile_mods = types.MethodType(lw.ClientLauncher._reconcile_mods, ns)
        stub(self, lw, "run_bg", inline_run_bg)
        stub(self, lw, "_enabled_mods", lambda d: [])
        ns._reconcile_mods("h", os.path.join(self.game, "game.exe"), then=launched.append)
        self.assertEqual((printed, launched), (["No mod info."], [[]]))


class WarningLog(unittest.TestCase):
    def test_without_stderr_warnings_go_to_a_log(self):
        data = temp_dir(self)
        stub(self, paths, "_tavern_data_dir", lambda: data)
        with mock.patch.object(base.sys, "stderr", None):
            base._warn("hello")
        with open(os.path.join(data, "modmanager.log"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "[modmanager] hello\n")
