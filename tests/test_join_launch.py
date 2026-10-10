"""The client launcher's join path, from the auth handshake to the game's
command line, and the pre-join parity check that gates it.

Two things are pinned here:

  * the scene the server asks for during auth (/tutorialScene, /questScene)
    reaches the game's launch args. The 1.8.5 merge left _do_launch passing a
    variable that no longer existed, so every authenticated join raised
    NameError - with no conflict marker to point at it;
  * a required mod with no version fails parity, as it does in TavernLib's
    ModParity.ValidateClient, rather than matching a client that lacks it.

As in test_mod_sync_flow, the window is never built: the methods under test
are bound onto a plain namespace, with each collaborator stood in for.
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import client.core.launcher_window as lw
from tavern_shared.mods.manifest import scene_from_arg


class FakeWidget:
    def config(self, **kwargs):
        pass


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class FakeProc:
    pid = 4242


class InlineThread:
    def __init__(self, target=None, args=(), daemon=None):
        pass

    def start(self):
        pass


def bind(ns, *names):
    for name in names:
        setattr(ns, name, types.MethodType(getattr(lw.ClientLauncher, name), ns))
    return ns


class SceneReachesTheLaunchArgs(unittest.TestCase):

    def _join(self, scene_arg):
        launched = []
        self.planned_scene = []
        ns = types.SimpleNamespace(
            v_exe=FakeVar(r"C:\game\A Township Tale.exe"),
            v_username=FakeVar("Tester"),
            v_platform=FakeVar("fly"),
            v_ip=FakeVar(""),
            v_port=FakeVar("1757"),
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
            _reconcile_mods_async=lambda host, exe, go, scene=None: (
                self.planned_scene.append(scene), go()),
        )
        bind(ns, "_do_launch", "_launch_game")
        fake_mods = types.SimpleNamespace(
            handshake_snapshot=lambda game_dir: ("", 0, [], []),
            scene_from_arg=scene_from_arg)

        def popen(args, **kwargs):
            launched.append(args)
            return FakeProc()

        with mock.patch.object(lw.os.path, "isfile", lambda p: True), \
             mock.patch.object(lw, "_resolve_ip_for_game", lambda h: "127.0.0.1"), \
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
             mock.patch.object(lw, "threading", types.SimpleNamespace(Thread=InlineThread)):
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


class VersionlessRequiredModFailsParity(unittest.TestCase):
    """Mirror of TavernLib's ModParity.ValidateClient rule."""

    def _verify(self, server_mods, installed):
        ns = types.SimpleNamespace()
        bind(ns, "_verify_mod_parity")
        fake_mods = types.SimpleNamespace(
            parity_required=lambda m: m.get("parity_required", True),
            handshake_snapshot=lambda game_dir: ("", 0, installed, []))
        with mock.patch.object(lw, "mods", fake_mods):
            return ns._verify_mod_parity(r"C:\game", server_mods)

    def test_no_version_against_an_absent_mod_mismatches(self):
        server = [{"id": "a.mod", "version": None, "client_side": True}]
        self.assertEqual(self._verify(server, []), [("a.mod", None, None)])

    def test_a_missing_version_key_mismatches_too(self):
        server = [{"id": "a.mod", "client_side": True}]
        self.assertEqual(len(self._verify(server, [])), 1)

    def test_an_exact_match_still_passes(self):
        server = [{"id": "a.mod", "version": "1.2.3", "client_side": True}]
        installed = [{"id": "a.mod", "version": "1.2.3"}]
        self.assertEqual(self._verify(server, installed), [])

    def test_a_mod_parity_does_not_require_is_still_ignored(self):
        server = [{"id": "a.mod", "version": None, "client_side": True,
                   "parity_required": False}]
        self.assertEqual(self._verify(server, []), [])


if __name__ == "__main__":
    unittest.main()
