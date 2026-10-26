"""The "close the game first" check.

Mod work while the game is running fails on Windows file locks somewhere in the
middle of an rmtree or a replace, so the whole value of this guard is that it
runs BEFORE anything is written and that it stays silent the rest of the time.
Both halves are tested here: a launcher that has no idea whether the game is up
(the server one) must never see a dialog, and one whose game has exited must not
either -- a guard that nags on every click is one people learn to click through.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared import game_guard as gg


class FakeProc:
    """Enough of a subprocess.Popen for game_is_running."""

    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


class _AskRecorder:
    """Stands in for messagebox.askyesno, recording that it was reached."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, title, message, **kwargs):
        self.calls.append((title, message, kwargs))
        return self.answer


class GameIsRunning(unittest.TestCase):

    def test_no_process_at_all(self):
        self.assertFalse(gg.game_is_running(None))

    def test_a_live_process(self):
        self.assertTrue(gg.game_is_running(FakeProc(exit_code=None)))

    def test_a_process_that_has_exited(self):
        """The launcher keeps the handle after the game closes, so an exit code
        of 0 has to read as 'not running' rather than as 'we have a handle'."""
        self.assertFalse(gg.game_is_running(FakeProc(exit_code=0)))

    def test_a_process_that_crashed(self):
        self.assertFalse(gg.game_is_running(FakeProc(exit_code=1)))


class AnyGameRunning(unittest.TestCase):
    """The client launcher tracks every process it has launched, because the
    guard's prompt can be overridden and a second copy started - the guard
    must stay live until the LAST of them exits, not the newest."""

    def test_no_processes(self):
        self.assertFalse(gg.any_game_running([]))

    def test_one_live_process(self):
        self.assertTrue(gg.any_game_running([FakeProc(exit_code=None)]))

    def test_newest_exiting_does_not_blind_the_guard(self):
        """The overridden-prompt case: the second copy exits while the first
        still holds Mods/ open, and the guard has to keep saying so."""
        old, new = FakeProc(exit_code=None), FakeProc(exit_code=0)
        procs = [old, new]
        self.assertTrue(gg.any_game_running(procs))
        self.assertEqual(procs, [old])   # the dead handle is pruned in place

    def test_all_exited(self):
        procs = [FakeProc(exit_code=0), FakeProc(exit_code=1)]
        self.assertFalse(gg.any_game_running(procs))
        self.assertEqual(procs, [])


class ConfirmWhileGameRunning(unittest.TestCase):

    def setUp(self):
        self._real_ask = gg.messagebox.askyesno
        self.addCleanup(lambda: setattr(gg.messagebox, "askyesno", self._real_ask))

    def _ask(self, answer):
        recorder = _AskRecorder(answer)
        gg.messagebox.askyesno = recorder
        return recorder

    def test_no_check_supplied_never_asks(self):
        """The server launcher passes nothing: it has its own, different story
        about mods changing under a live session, and must keep it."""
        ask = self._ask(False)
        self.assertTrue(gg.confirm_while_game_running(None, None, "Installing X"))
        self.assertEqual(ask.calls, [])

    def test_game_not_running_never_asks(self):
        ask = self._ask(False)
        self.assertTrue(gg.confirm_while_game_running(None, lambda: False, "Installing X"))
        self.assertEqual(ask.calls, [])

    def test_game_running_asks_and_can_be_overridden(self):
        ask = self._ask(True)
        self.assertTrue(gg.confirm_while_game_running(None, lambda: True, "Installing X"))
        self.assertEqual(len(ask.calls), 1)

    def test_declining_stops_the_action(self):
        self._ask(False)
        self.assertFalse(gg.confirm_while_game_running(None, lambda: True, "Installing X"))

    def test_the_dialog_names_the_action_and_defaults_to_no(self):
        """Defaulting to yes would make the guard worse than nothing: the whole
        point is that the safe answer is the one you get by pressing enter."""
        ask = self._ask(False)
        gg.confirm_while_game_running(None, lambda: True, "Removing Better Anvils")
        _title, message, kwargs = ask.calls[0]
        self.assertIn("Removing Better Anvils while it's running", message)
        self.assertEqual(kwargs.get("default"), "no")


if __name__ == "__main__":
    unittest.main()
