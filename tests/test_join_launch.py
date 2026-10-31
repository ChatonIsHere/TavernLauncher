"""The client launcher's join path, from the auth handshake to the game's
command line, and the pre-join parity check that gates it.

Pinned here: the scene the server asks for during auth reaches the game's
launch args, and a required mod with no version fails parity, as in TavernLib's
ModParity.CheckJoin. The methods under test are bound onto a plain namespace.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import client.core.launcher_window as lw
from tavern_shared.mods.install import has_target
from tavern_shared.mods.manifest import parity_required, scene_from_arg
from tests._tk_fakes import FakeVar, FakeWidget, NoThread


class FakeProc:
    pid = 4242


def bind(ns, *names):
    for name in names:
        setattr(ns, name, types.MethodType(getattr(lw.ClientLauncher, name), ns))
    return ns


class SceneReachesTheLaunchArgs(unittest.TestCase):

    def _join(self, scene_arg, ip="", port="1757"):
        launched = []
        self.planned_scene = []
        self.resolved = []
        ns = types.SimpleNamespace(
            v_exe=FakeVar(r"C:\game\A Township Tale.exe"),
            v_username=FakeVar("Tester"),
            v_platform=FakeVar("fly"),
            v_ip=FakeVar(ip),
            v_port=FakeVar(port),
            _selected_kind="official",
            v_debug_helper=FakeVar(False),
            _check_status=FakeVar(""),
            _action_btn=FakeWidget(),
            _game_procs=[],
            _save=lambda: None,
            _print=lambda msg, tag="": None,
            _show_token_button=lambda: None,
            _popen_console_kwargs=lambda: {},
            # Mod reconciliation is its own flow; here it just lets the
            # launch go ahead, the way it does when there's nothing to sync.
            _reconcile_mods=lambda host, exe, go, scene=None: (
                self.planned_scene.append(scene), go(None)),
        )
        bind(ns, "_do_launch", "_launch_game", "_mods_claim", "_take_host")
        self.ns = ns
        fake_mods = types.SimpleNamespace(
            list_installed_mods=lambda game_dir: [],
            scene_from_arg=scene_from_arg)

        def popen(args, **kwargs):
            launched.append(args)
            return FakeProc()

        with mock.patch.object(lw.os.path, "isfile", lambda p: True), \
             mock.patch.object(lw, "_resolve_ip_for_game",
                               lambda h: (self.resolved.append(h), "127.0.0.1")[1]), \
             mock.patch.object(lw, "_any_token_files_exist", lambda: True), \
             mock.patch.object(lw, "_get_or_create_token", lambda h, u: ("tok", False)), \
             mock.patch.object(lw, "authenticate",
                               lambda host, user, token, password=None: (7, None, scene_arg)), \
             mock.patch.object(lw, "load_cfg", lambda: {}), \
             mock.patch.object(lw, "save_cfg", lambda cfg: None), \
             mock.patch.object(lw, "mods", fake_mods), \
             mock.patch.object(lw, "build_tokens", lambda *a: ("a", "r", "i")), \
             mock.patch.object(lw, "_delete_last_rejection_file", lambda: None), \
             mock.patch.object(lw.subprocess, "Popen", popen), \
             mock.patch.object(lw, "threading", types.SimpleNamespace(Thread=NoThread)):
            ns._do_launch(password=None)
        self.assertEqual(len(launched), 1, "the game was never launched")
        return launched[0]

    def test_a_tutorial_server_launches_into_the_tutorial_scene(self):
        self.assertIn("/tutorialScene", self._join("/tutorialScene"))
        # ...and the mod plan is made for that scene, so pins that don't work
        # there are left out (see plan_join's scene).
        self.assertEqual(self.planned_scene, ["tutorial"])

    def test_a_quest_server_launches_into_the_quest_scene(self):
        self.assertIn("/questScene", self._join("/questScene"))
        self.assertEqual(self.planned_scene, ["quest"])

    def test_a_normal_server_forces_no_scene(self):
        args = self._join(None)
        self.assertNotIn("/tutorialScene", args)
        self.assertNotIn("/questScene", args)
        self.assertEqual(self.planned_scene, ["normal"])

    def test_a_port_typed_with_the_address_moves_to_the_port_field(self):
        args = self._join(None, ip="100.66.236.126:1757", port="1758")
        self.assertEqual(self.resolved, ["100.66.236.126"])
        self.assertEqual(self.ns.v_ip.get(), "100.66.236.126")
        self.assertEqual(self.ns.v_port.get(), "1757")
        self.assertEqual(args[args.index("/dev_server_port") + 1], "1757")


class SplitHostPort(unittest.TestCase):
    """What the server fields take: a host, optionally with its game port."""

    def test_cases(self):
        cases = {
            "100.66.236.126:1757":  ("100.66.236.126", 1757),
            " play.example.com:1760 ": ("play.example.com", 1760),
            "100.66.236.126":       ("100.66.236.126", None),
            "":                     ("", None),
            "host:":                ("host", None),
            "host:99999":           ("host", None),
            "[::1]:1757":           ("::1", 1757),
            "[::1]":                ("::1", None),
            "::1":                  ("::1", None),
            "fe80::1":              ("fe80::1", None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(lw.split_host_port(text), expected)


class MainMenuLaunch(unittest.TestCase):
    """The Main Menu button (upstream 1.8.5): the game started with the
    offline identity a headless join uses, minus /join_local_server.
    Upstream's version called self._popen._console_kwargs(), which raised
    inside its own try, so every launch only ever logged a failure."""

    def _launch(self):
        launched, printed = [], []
        ns = types.SimpleNamespace(
            v_exe=FakeVar(r"C:\game\A Township Tale.exe"), v_username=FakeVar("Tester"),
            v_platform=FakeVar("fly"), v_debug_helper=FakeVar(False), _game_procs=[],
            _save=lambda: None, _print=lambda msg, tag="": printed.append((msg, tag)),
            _popen_console_kwargs=lambda: {})
        bind(ns, "_do_launch_menu", "_mods_claim")
        pc = {"installed": {"client_windows": {}}}
        fake_mods = types.SimpleNamespace(
            has_target=has_target,
            list_installed_mods=lambda d: [
                {"id": "a.mod", "version": "1.2.3", "enabled": True, **pc},
                {"id": "off.mod", "version": "2.0.0", "enabled": False, **pc},
                # The host's server build in the same folder: the game never
                # loads it, so it's no part of what this client claims.
                {"id": "srv.mod", "version": "3.0.0", "enabled": True,
                 "installed": {"server": {}}}])
        claims = []

        def popen(args, **kwargs):
            launched.append(args)
            return FakeProc()

        with mock.patch.object(lw.os.path, "isfile", lambda p: True), \
             mock.patch.object(lw, "mods", fake_mods), \
             mock.patch.object(lw, "build_tokens",
                               lambda uid, name, token="", claim="": claims.append(claim) or ("a", "r", "i")), \
             mock.patch.object(lw.subprocess, "Popen", popen):
            ns._do_launch_menu()
        return ns, launched, printed, claims

    def test_launches_into_the_menu_not_a_server(self):
        _, launched, printed, _ = self._launch()
        self.assertEqual(len(launched), 1, printed)
        self.assertIn("/fly", launched[0])
        self.assertNotIn("/join_local_server", launched[0])
        self.assertFalse(any(tag == "err" for _, tag in printed), printed)

    def test_the_game_is_tracked_like_a_joined_one(self):
        ns, _, _, _ = self._launch()
        self.assertEqual(len(ns._game_procs), 1)

    def test_it_carries_the_same_mods_claim_as_a_join(self):
        """Enabled mods installed for client_windows, and nothing else."""
        _, _, _, claims = self._launch()
        self.assertEqual(claims, ['{"a.mod": "1.2.3"}'])


class TavernEssentialsDetection(unittest.TestCase):
    """What shows the Main Menu button: an enabled TavernEssentials.dll,
    loose (upstream 1.8.5's install) or in a loaded mod folder's .client_windows/ build
    (ours)."""

    def setUp(self):
        self.game = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.game, ignore_errors=True)
        os.makedirs(os.path.join(self.game, "Mods"))

    def _file(self, *parts):
        path = os.path.join(self.game, "Mods", *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()

    def test_nothing_installed(self):
        self.assertFalse(lw._tavernessentials_installed(self.game))

    def test_loose_dll(self):
        self._file("TavernEssentials.dll")
        self.assertTrue(lw._tavernessentials_installed(self.game))

    def test_managed_folder(self):
        self._file("Nyx12012.Tavern-Essentials", "manifest.json")
        self._file("Nyx12012.Tavern-Essentials", ".client_windows", "TavernEssentials.dll")
        self.assertTrue(lw._tavernessentials_installed(self.game))

    def test_a_server_build_does_not_count(self):
        """Only .client_windows/ loads in the game a player launches."""
        self._file("Nyx12012.Tavern-Essentials", "manifest.json")
        self._file("Nyx12012.Tavern-Essentials", ".server", "TavernEssentials.dll")
        self.assertFalse(lw._tavernessentials_installed(self.game))

    def test_disabled_copies_do_not_count(self):
        self._file("TavernEssentials.dll.disabled")
        self._file("Nyx12012.Tavern-Essentials", "manifest.disabled.json")
        self._file("Nyx12012.Tavern-Essentials", ".client_windows", "TavernEssentials.dll")
        self.assertFalse(lw._tavernessentials_installed(self.game))

    def test_no_game_folder(self):
        self.assertFalse(lw._tavernessentials_installed(os.path.join(self.game, "missing")))


class VersionlessRequiredModFailsParity(unittest.TestCase):
    """Mirror of TavernLib's ModParity.CheckJoin rule."""

    def _verify(self, server_mods, installed):
        ns = types.SimpleNamespace()
        bind(ns, "_verify_mod_parity")
        fake_mods = types.SimpleNamespace(
            parity_required=parity_required)
        with mock.patch.object(lw, "mods", fake_mods):
            return ns._verify_mod_parity(server_mods, installed)

    def test_no_version_against_an_absent_mod_mismatches(self):
        server = [{"id": "a.mod", "version": None, "targets": ["client_windows", "server"],
                   "parity_required": True}]
        self.assertEqual(self._verify(server, []), [("a.mod", None, None)])

    def test_a_missing_version_key_mismatches_too(self):
        server = [{"id": "a.mod", "targets": ["client_windows"], "parity_required": True}]
        self.assertEqual(len(self._verify(server, [])), 1)

    def test_an_exact_match_still_passes(self):
        server = [{"id": "a.mod", "version": "1.2.3", "targets": ["client_windows", "server"],
                   "parity_required": True}]
        installed = [{"id": "a.mod", "version": "1.2.3"}]
        self.assertEqual(self._verify(server, installed), [])

    def test_a_mod_parity_does_not_require_is_still_ignored(self):
        server = [{"id": "a.mod", "version": None, "targets": ["client_windows", "server"],
                   "parity_required": False}]
        self.assertEqual(self._verify(server, []), [])

    def test_a_mod_without_a_pc_build_is_never_required_of_a_client(self):
        server = [{"id": "srv.mod", "version": "1.0.0", "targets": ["server", "client_android"],
                   "parity_required": True}]
        self.assertEqual(self._verify(server, []), [])


if __name__ == "__main__":
    unittest.main()
