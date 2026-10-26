"""Manifest v2's per-target rules (TavernMods docs/REPO_STRUCTURE.md):
artifacts and targets, the Mods/<id>/.<target>/ layout and its record, and
what each launcher reads back for its own target. TavernLib implements the
same spec, so each case here is a claim about both installers."""
import contextlib
import hashlib
import io
import json
import os
import types
import unittest

from conftest import stub
from test_modmanager import (
    DEFAULT, _FakeInstallFixture, _TempDataDirFixture, _srv, lib_dep, make_manifest,
    manifest_dict, mm, slim_entry, summary,
)
from tavern_shared.mods import install

import client.core.launcher_window as lw


def two_builds(mod_id, version, **over):
    """A manifest with separate pc and server dlls, plus an unused quest zip."""
    d = manifest_dict(mod_id, version, targets=("pc",), **over)
    base = d["artifacts"][0]["download_url"].rsplit("/", 1)[0]
    d["artifacts"] += [
        {"targets": ["server"], "download_url": f"{base}/{mod_id}-server.dll",
         "sha256": "0" * 64, "package": "dll", "library_dependencies": []},
        {"targets": ["quest"], "download_url": f"{base}/{mod_id}-quest.zip",
         "sha256": "1" * 64, "package": "zip", "library_dependencies": []},
    ]
    return d


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


class ArtifactParsing(unittest.TestCase):
    def _parse(self, artifacts):
        return mm._manifest_from_dict(manifest_dict("a.b", "1.0.0", artifacts=artifacts), DEFAULT)

    def _art(self, targets, package="dll", sha="0"):
        return {"targets": list(targets), "download_url": "https://x/a.dll",
                "sha256": sha * 64, "package": package, "library_dependencies": []}

    def test_a_target_listed_twice_refuses_the_manifest(self):
        for artifacts in ([self._art(["pc"]), self._art(["pc", "server"])],
                          [self._art(["server", "server"])],
                          # Even when the second copy's package is one it ignores.
                          [self._art(["pc"]), self._art(["pc"], package="apk")]):
            with self.subTest(artifacts=artifacts), self.assertRaises(mm.ModManagerError):
                self._parse(artifacts)

    def test_unknown_targets_are_ignored(self):
        """A target added later mustn't break this build; listed twice is fine too."""
        m = self._parse([self._art(["pc", "vr"]), self._art(["vr"], sha="1")])
        self.assertEqual([a.targets for a in m.artifacts], [["pc"]])
        self.assertEqual(sorted({t for a in m.artifacts for t in a.targets}), ["pc"])
        # The record still copies both, verbatim.
        self.assertEqual([a["targets"] for a in m.record_artifacts], [["pc", "vr"], ["vr"]])

    def test_an_artifact_of_unknown_package_is_ignored(self):
        with quiet():
            m = self._parse([self._art(["pc"], package="apk"), self._art(["server"], sha="1")])
        self.assertIsNone(m.artifact_for("pc"))
        self.assertEqual(sorted({t for a in m.artifacts for t in a.targets}), ["server"])
        self.assertEqual(len(m.record_artifacts), 2)

    def test_package_matches_exactly(self):
        """The tooling writes "dll" or "zip"; any other spelling is unknown, as
        it is to TavernLib."""
        for package in ("DLL", "Zip", None):
            with self.subTest(package=package), quiet():
                m = self._parse([self._art(["pc"], package=package)])
                self.assertIsNone(m.artifact_for("pc"))

    def test_libraries_conflict_on_bytes_not_urls(self):
        def pinning(mod_id, url, sha):
            return mm._manifest_from_dict(manifest_dict(mod_id, "1.0.0", library_dependencies=[
                {"name": "L", "download_url": url, "sha256": sha, "filename": "L.dll"}]), DEFAULT)

        same = [pinning("a.m", "https://x/one/L.dll", "a" * 64),
                pinning("b.m", "https://y/two/L.dll", "a" * 64)]
        self.assertEqual(len(mm.collect_library_dependencies(same, "pc")), 1)
        with self.assertRaises(mm.ModManagerError):
            mm.collect_library_dependencies(
                [same[0], pinning("c.m", "https://x/one/L.dll", "b" * 64)], "pc")

    def test_quest_is_parsed_and_each_target_finds_its_own_build(self):
        m = mm._manifest_from_dict(two_builds("a.b", "1.0.0"), DEFAULT)
        self.assertEqual(sorted({t for a in m.artifacts for t in a.targets}), ["pc", "quest", "server"])
        self.assertTrue(m.artifact_for("pc").download_url.endswith("/a.b.dll"))
        self.assertTrue(m.artifact_for("server").download_url.endswith("-server.dll"))
        self.assertEqual(m.artifact_for("quest").package, "zip")

    def test_malformed_artifacts_refuse_the_manifest(self):
        good = self._art(["pc"])
        for bad in (None, [], "pc", [dict(good, targets=[])], [dict(good, targets="pc")],
                    [dict(good, sha256="xyz")], [{k: v for k, v in good.items()
                                                  if k != "download_url"}],
                    [dict(good, library_dependencies=[{"name": "L"}])], ["pc"]):
            with self.subTest(artifacts=bad), self.assertRaises(mm.ModManagerError):
                self._parse(bad)

    def test_the_index_reads_targets_ignoring_unknown_names(self):
        s = mm._summary_from_dict(slim_entry(["1.0.0"], targets=("server", "vr", "pc")),
                                  "a.b", DEFAULT)
        self.assertEqual(s.targets, ["pc", "server"])


class RecordBytes(_FakeInstallFixture, unittest.TestCase):
    """The record's exact bytes: TavernLib writes the same for the same state."""

    def _raw(self, mod_id):
        with open(os.path.join(self.game, "Mods", mod_id, "manifest.json"), "rb") as f:
            return f.read()

    def test_crlf_ascii_escaped_and_no_trailing_newline(self):
        self._install("r.m", description="Ünïcödé ✓")
        raw = self._raw("r.m")
        raw.decode("ascii")
        self.assertIn(b"\\u00dcn\\u00efc\\u00f6d\\u00e9 \\u2713", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertTrue(raw.endswith(b"}"))

    def test_keys_in_spec_order_without_screenshots(self):
        self._install("r.m", scenes=["quest"], screenshots=["https://x/s.png"])
        rec = self._record("r.m")
        self.assertEqual(list(rec), [
            "manifest_version", "id", "name", "version", "author", "description",
            "parity_required", "scenes", "dependencies", "artifacts", "source_repo",
            "installed"])
        self.assertEqual(list(rec["artifacts"][0]), [
            "targets", "download_url", "sha256", "package", "library_dependencies"])
        self.assertEqual(list(rec["installed"]["pc"]), ["sha256", "libraries", "files"])

    def test_files_sort_by_utf16_code_unit_not_code_point(self):
        """C#'s ordinal order: U+1F600 (a surrogate pair, D83D...) before U+FF21."""
        art = types.SimpleNamespace(sha256="a" * 64, library_dependencies=[])
        files = {"Ａ.txt": "1", "\U0001F600.txt": "2", "a/b.dll": "3", "B.dll": "4"}
        entry = mm._installed_entry_for(art, files)
        self.assertEqual(list(entry["files"]),
                         ["B.dll", "a/b.dll", "\U0001F600.txt", "Ａ.txt"])

    def test_library_sidecars_use_the_same_formatting(self):
        self._install("r.m", library_dependencies=[lib_dep("L")])
        with open(os.path.join(self.game, "UserLibs", "L.dll.meta.json"), "rb") as f:
            raw = f.read()
        self.assertEqual(raw, json.dumps(
            {"filename": "L.dll", "sha256": self._lib_sha("L"),
             "download_url": "https://x/L.dll"}, indent=2).replace("\n", "\r\n").encode())


class PerTargetInstalls(_FakeInstallFixture, unittest.TestCase):
    def _both(self, mod_id, version="1.0.0", **over):
        self._publish(mod_id, version, **over)
        self._closure(mod_id, version, target="pc")
        self._closure(mod_id, version, target="server")

    def test_pc_and_server_installs_share_one_folder(self):
        self._both("both.m")
        self.assertEqual(sorted(os.listdir(os.path.join(self.game, "Mods", "both.m"))),
                         [".pc", ".server", "manifest.json"])
        self.assertEqual(mm.installed_targets(self._record("both.m")), ["pc", "server"])
        for target in ("pc", "server"):
            self.assertIs(mm.verify_mod_files(self.game, "both.m", target), True)

    def test_reinstalling_one_target_leaves_the_other_untouched(self):
        self._both("both.m")
        server_dll = self._mod_file("both.m", "both.m.dll", target="server")
        os.utime(server_dll, (1_000_000, 1_000_000))
        server_entry = self._record("both.m")["installed"]["server"]
        mm.install_mod(self.game, mm._fetch_manifest([self.BASE], "both.m", "1.0.0"), "pc",
                       lambda _m: None)
        self.assertEqual(os.path.getmtime(server_dll), 1_000_000)
        self.assertEqual(self._record("both.m")["installed"]["server"], server_entry)

    def test_a_version_change_drops_the_other_target(self):
        self._both("ver.m", "1.0.0")
        self._publish("ver.m", "2.0.0")
        self._closure("ver.m", "2.0.0", target="pc")
        rec = self._record("ver.m")
        self.assertEqual((rec["version"], mm.installed_targets(rec)), ("2.0.0", ["pc"]))
        self.assertFalse(os.path.exists(self._mod_file("ver.m", target="server")))

    def test_a_locked_other_target_folder_is_left_behind_unlisted(self):
        """A running server holding .server/ open: it stays, but nothing lists
        it, so nothing loads it."""
        self._both("lock.m", "1.0.0")
        self._publish("lock.m", "2.0.0")
        real = install._remove_best_effort
        stub(self, install, "_remove_best_effort",
             lambda path: False if path.endswith(".server") else real(path))
        with quiet() as err:
            self._closure("lock.m", "2.0.0", target="pc")
        self.assertTrue(os.path.isdir(self._mod_file("lock.m", target="server")))
        self.assertIn(".server", err.getvalue())
        self.assertEqual(mm.installed_targets(self._record("lock.m")), ["pc"])
        self.assertEqual(mm.handshake_snapshot(self.game, "server")[2], [])
        self.assertEqual(mm.mod_status(self.game, "lock.m", [], "server"), "missing")

    def test_a_pc_only_dependency_is_pruned_for_a_server_install(self):
        self._publish("pc.dep", "1.0.0", targets=("pc",))
        self._publish("root.m", "1.0.0", dependencies={"pc.dep": "1.0.0"})
        with quiet():
            mm.install_mod_closure(self.game, summary("root.m", ["1.0.0"]),
                                   [summary("pc.dep", ["1.0.0"], targets=("pc",))],
                                   [self.BASE], self.progress.append, "server")
        self.assertEqual(os.listdir(os.path.join(self.game, "Mods")), ["root.m"])
        self.assertEqual(mm.installed_targets(self._record("root.m")), ["server"])

    def test_a_mod_without_a_build_for_the_target_is_refused(self):
        self._publish("pc.only", "1.0.0", targets=("pc",))
        with self.assertRaises(mm.ModManagerError):
            self._closure("pc.only", "1.0.0", target="server")
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "pc.only")))

    def test_installing_rewrites_an_enabled_record(self):
        """Disabling is one switch for the whole mod; an install turns it on."""
        self._publish("off.m", "1.0.0")
        self._closure("off.m", target="pc")
        mm.disable_mod(self.game, "off.m")
        self._closure("off.m", target="server")
        self.assertTrue(os.path.isfile(mm._mod_record_path(self.game, "off.m")))
        self.assertFalse(os.path.exists(mm._disabled_record_path(self.game, "off.m")))
        self.assertEqual(mm.installed_targets(self._record("off.m")), ["pc", "server"])

    def test_a_pre_v2_install_reads_damaged_and_is_replaced_whole(self):
        folder = os.path.join(self.game, "Mods", "old.m")
        os.makedirs(folder)
        with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "old.m", "version": "1.0.0", "parity_required": True,
                       "files": {"old.m.dll": "0" * 64}}, f)
        with open(os.path.join(folder, "old.m.dll"), "wb") as f:
            f.write(b"MZ v1")
        self.assertEqual(mm.mod_status(self.game, "old.m", [], "pc"), "damaged")

        self._install("old.m", "1.0.0")
        self.assertEqual(sorted(os.listdir(folder)), [".pc", "manifest.json"])
        self.assertEqual(mm.installed_targets(self._record("old.m")), ["pc"])


class PerTargetDamage(_FakeInstallFixture, unittest.TestCase):
    def test_damage_is_per_target(self):
        self._publish("d.m", "1.0.0")
        self._closure("d.m", target="pc")
        self._closure("d.m", target="server")
        with open(self._mod_file("d.m", "d.m.dll", target="server"), "wb") as f:
            f.write(b"eaten")
        index = [summary("d.m", ["1.0.0"])]
        self.assertIs(mm.verify_mod_files(self.game, "d.m", "pc"), True)
        self.assertIs(mm.verify_mod_files(self.game, "d.m", "server"), False)
        self.assertEqual(mm.mod_status(self.game, "d.m", index, "pc"), "current")
        self.assertEqual(mm.mod_status(self.game, "d.m", index, "server"), "damaged")

    def test_a_target_not_installed_is_missing_not_damaged(self):
        self._install("pc.m")
        self.assertIsNone(mm.verify_mod_files(self.game, "pc.m", "server"))
        self.assertEqual(mm.mod_status(self.game, "pc.m", [summary("pc.m", ["1.0.0"])],
                                       "server"), "missing")


class LibrariesAcrossTargets(_FakeInstallFixture, unittest.TestCase):
    def test_uninstall_counts_every_targets_libraries(self):
        self._publish("a.m", "1.0.0", library_dependencies=[lib_dep("L")])
        self._publish("b.m", "1.0.0", library_dependencies=[lib_dep("L")])
        self._closure("a.m", target="pc")
        self._closure("b.m", target="server")
        lib = os.path.join(self.game, "UserLibs", "L.dll")
        mm.uninstall_mod(self.game, "a.m")
        self.assertTrue(os.path.isfile(lib))        # b.m's server build pins it
        mm.uninstall_mod(self.game, "b.m")
        self.assertFalse(os.path.exists(lib))

    def _publish_pinning(self, mod_id, version, url):
        self._publish(mod_id, version, library_dependencies=[
            {"name": "L", "download_url": url, "sha256": "", "filename": "L.dll"}])

    def test_a_library_conflict_spans_the_folder(self):
        """A server build's library is the one the server loads, whichever
        launcher installs next."""
        self._publish_pinning("srv.m", "1.0.0", "https://x/one/L.dll")
        self._publish_pinning("pc.m", "1.0.0", "https://x/two/L.dll")
        self._closure("srv.m", target="server")
        with self.assertRaises(mm.ModManagerError) as ctx:
            self._closure("pc.m", target="pc")
        self.assertIn("L.dll", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.game, "Mods", "pc.m")))

    def test_a_build_being_replaced_is_no_conflict(self):
        self._publish_pinning("m.m", "1.0.0", "https://x/one/L.dll")
        self._publish_pinning("m.m", "2.0.0", "https://x/two/L.dll")
        self._closure("m.m", "1.0.0", target="server")
        self._closure("m.m", "2.0.0", target="pc")      # drops the server build
        self.assertEqual(mm.installed_targets(self._record("m.m")), ["pc"])


class HandshakeByTarget(_FakeInstallFixture, unittest.TestCase):
    def test_only_mods_installed_for_the_server_are_advertised(self):
        for mod_id in ("a.pc", "b.srv", "c.both"):
            self._publish(mod_id, "1.0.0")
        self._closure("a.pc", target="pc")
        self._closure("b.srv", target="server")
        self._closure("c.both", target="pc")
        self._closure("c.both", target="server")

        mods_hash, count, entries, _ = mm.handshake_snapshot(self.game)
        self.assertEqual([e["id"] for e in entries], ["b.srv", "c.both"])
        self.assertEqual(count, 2)
        self.assertEqual(mods_hash, hashlib.sha256(
            b"b.srv@1.0.0@req\nc.both@1.0.0@req").hexdigest())
        self.assertEqual(list(entries[0]), ["id", "version", "parity_required",
                                            "source_repo", "targets", "target_sha256"])

    def test_targets_and_hashes_come_from_the_artifacts(self):
        """Every known target the mod has a build for, installed here or not."""
        d = two_builds("split.m", "1.0.0")
        for art in d["artifacts"][:2]:
            art["sha256"] = self._sha_for(art["download_url"])
        self.manifests[f"{self.BASE}/manifests/split/m/1.0.0.json"] = d
        self._closure("split.m", target="server")
        entry = mm.handshake_snapshot(self.game)[2][0]
        self.assertEqual(entry["targets"], ["pc", "quest", "server"])
        self.assertEqual(entry["target_sha256"], {a["targets"][0]: a["sha256"]
                                                  for a in d["artifacts"]})


class PlanByTarget(_TempDataDirFixture, unittest.TestCase):
    def test_entries_without_a_pc_build_are_left_alone(self):
        self._publish("both.m", "1.0.0")
        server_mods = [_srv("srv.m", "1.0.0", targets=("server",)),
                       _srv("q.m", "1.0.0", targets=("quest", "server")),
                       _srv("both.m", "1.0.0")]
        plan = self._plan(server_mods, [summary("both.m", ["1.0.0"])])
        self.assertEqual([e.mod_id for e in plan.entries], ["both.m"])
        self.assertFalse(plan.blocking)
        rows = {r.mod_id: r.action for r in mm.build_mod_diff(self.game, server_mods, plan)}
        self.assertEqual(rows["srv.m"], mm.ACTION_SERVER_ONLY)
        self.assertEqual(rows["q.m"], mm.ACTION_SERVER_ONLY)

    def test_the_hosts_server_build_is_never_deactivated(self):
        self._publish("host.m", "1.0.0")
        self._closure("host.m", target="server")
        self.assertEqual(self._plan([], []).to_deactivate, [])

    def test_a_pc_build_lands_beside_the_hosts_server_build(self):
        self._publish("host.m", "1.0.0")
        self._closure("host.m", target="server")
        server_mods = [_srv("host.m", "1.0.0")]
        plan = self._plan(server_mods, [summary("host.m", ["1.0.0"])])
        self.assertFalse(plan.entries[0].active)
        mm.render_active_set(self.game, plan, lambda *_a: None, lambda *_a: None)
        self.assertEqual(mm.installed_targets(self._record("host.m")), ["pc", "server"])
        self.assertTrue(self._plan(server_mods, [summary("host.m", ["1.0.0"])]).entries[0].active)

    def test_a_join_refuses_a_library_conflict_with_the_hosts_server(self):
        url = "https://x/{}/L.dll"
        self._publish("srv.m", "1.0.0", library_dependencies=[
            {"name": "L", "download_url": url.format("a"), "sha256": "", "filename": "L.dll"}])
        self._publish("pc.m", "1.0.0", library_dependencies=[
            {"name": "L", "download_url": url.format("b"), "sha256": "", "filename": "L.dll"}])
        self._closure("srv.m", target="server")
        with self.assertRaises(mm.ModManagerError):
            self._plan([_srv("pc.m", "1.0.0")], [summary("pc.m", ["1.0.0"])])


class CacheBySha(_TempDataDirFixture, unittest.TestCase):
    def test_two_builds_of_one_version_are_two_entries(self):
        self._install("c.m", "1.0.0")
        mm.cache_store_mod(self.game, "c.m", "pc")
        # Republished at the same version with different bytes.
        self._publish("c.m", "1.0.0", download_url="https://x/rebuilt/c.m.dll")
        mm._manifest_memo.clear()
        mm.install_mod(self.game, mm._fetch_manifest([self.BASE], "c.m", "1.0.0"), "pc",
                       lambda _m: None)
        mm.cache_store_mod(self.game, "c.m", "pc")
        version_dir = os.path.join(mm._cache_base(), "c.m", "1.0.0")
        self.assertEqual(sorted(os.listdir(version_dir)), sorted([
            self._sha_for(manifest_dict("c.m", "1.0.0")["artifacts"][0]["download_url"]),
            self._sha_for("https://x/rebuilt/c.m.dll")]))

    def test_restore_fills_the_target_folder_and_keeps_the_other_target(self):
        self._publish("c.m", "1.0.0")
        self._closure("c.m", target="pc")
        self._closure("c.m", target="server")
        mm.cache_store_mod(self.game, "c.m", "pc")
        with open(self._mod_file("c.m", "c.m.dll"), "wb") as f:
            f.write(b"eaten")
        server_entry = self._record("c.m")["installed"]["server"]

        self._restore("c.m", "1.0.0")
        mm._verify_memo.clear()
        self.assertIs(mm.verify_mod_files(self.game, "c.m", "pc"), True)
        self.assertEqual(self._record("c.m")["installed"]["server"], server_entry)

    def test_adopt_caches_only_the_target_asked_for(self):
        self._install("pc.m")
        self._publish("srv.m", "1.0.0")
        self._closure("srv.m", target="server")
        self.assertEqual(mm.adopt_installed_mods(self.game, "pc"), ["pc.m"])
        self.assertFalse(os.path.exists(os.path.join(mm._cache_base(), "srv.m")))


class ClientReadsItsOwnTarget(_FakeInstallFixture, unittest.TestCase):
    """The claim, the parity check and the build advisory all read pc."""

    def test_the_claim_lists_enabled_mods_installed_for_pc(self):
        for mod_id in ("a.pc", "b.srv", "c.off"):
            self._publish(mod_id, "1.0.0")
        self._closure("a.pc", target="pc")
        self._closure("b.srv", target="server")
        self._closure("c.off", target="pc")
        mm.disable_mod(self.game, "c.off")
        ns = types.SimpleNamespace(_print=lambda *a: None)
        claim = lw.ClientLauncher._mods_claim(ns, self.game)
        self.assertEqual(json.loads(claim), {"a.pc": "1.0.0"})

    def test_the_build_advisory_compares_pc_hashes(self):
        self._install("b.m")
        installed = lw._enabled_mods(self.game)
        local = installed[0]["installed"]["pc"]["sha256"]
        ns = types.SimpleNamespace()

        def warnings(shas):
            entry = dict(_srv("b.m", "1.0.0"), target_sha256=shas)
            return lw.ClientLauncher._build_divergence(ns, [entry], installed)
        self.assertEqual(warnings({"pc": local, "server": "f" * 64}), [])
        self.assertEqual(len(warnings({"pc": "f" * 64, "server": local})), 1)
        self.assertEqual(warnings({}), [])


if __name__ == "__main__":
    unittest.main()
