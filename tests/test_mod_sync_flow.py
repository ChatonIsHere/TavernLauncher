"""The client launcher's mod-sync flow, at the seams where the Sync button and
the join path have to agree with each other.

Three things are pinned here, all of them cases where the launcher used to say
one thing and do another:

  * a decline taken back has to be RE-PLANNED before it can be applied - the
    plan on screen never resolved that mod, so applying it as-is installs
    nothing while reporting success;
  * Sync and Join must key declines and the mods-list cache by the same host
    string, which is the resolved one, because that's what the join path uses;
  * "this server runs no mods" and "this server told us nothing about its mods"
    are different answers - only the second one has nothing to sync against.

The window is never built: ClientLauncher is a tk.Tk subclass and needs a
display, a game folder and a config file. The methods under test are bound onto
a plain namespace instead, so each test says exactly which of them it exercises
and which collaborators it stands in for.
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import client.core.launcher_window as lw
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.plan import JoinPlan, PlanEntry


def make_plan(entries=(), to_deactivate=(), declined=()):
    return JoinPlan(entries=list(entries), to_deactivate=list(to_deactivate),
                    libraries=[], missing=[], needs_repo=[], pin_conflicts=[],
                    declined=list(declined))


def entry(mod_id, version="1.0.0", reason="recommended"):
    return PlanEntry(mod_id=mod_id, version=version, source_repo="repo",
                     reason=reason, cached=True, active=False,
                     manifest=types.SimpleNamespace(id=mod_id, version=version,
                                                    name=mod_id))


class FakeWidget:
    def config(self, **kwargs):
        pass


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWindow:
    """The ModDiffWindow surface _apply_mod_plan actually touches."""

    def __init__(self, declined=(), pinned_keeps=()):
        self.declined = list(declined)
        self.pinned_keeps = list(pinned_keeps)
        self.retargeted = []
        self.finished = []

    def set_status(self, message):
        pass

    def set_step(self, item_id, state):
        pass

    def retarget(self, plan):
        self.retargeted.append(plan)

    def apply_finished(self, ok, message=""):
        self.finished.append((ok, message))


class InlineThread:
    """threading.Thread that runs on construction-plus-start, so a test sees
    the worker's effects without a join or a timeout."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def launcher(**attrs):
    ns = types.SimpleNamespace(**attrs)
    ns.after = lambda _delay, fn: fn()
    ns.printed = []
    ns._print = lambda msg, tag="": ns.printed.append((msg, tag))
    return ns


def bind(ns, *names):
    for name in names:
        setattr(ns, name, types.MethodType(getattr(lw.ClientLauncher, name), ns))
    return ns


class UnDecliningRePlans(unittest.TestCase):
    """FINDING: turning a declined recommendation back on used to be a no-op -
    the row said "Install", Apply reported success, and nothing was installed
    until the next join happened to re-plan."""

    def setUp(self):
        self.rendered = []
        self.saved_declines = []
        self.mods = types.SimpleNamespace(
            ModManagerError=ModManagerError,
            set_declined=lambda cfg, host, declined: self.saved_declines.append(
                (host, list(declined))),
            add_pin=lambda cfg, mod_id: None,
            render_active_set=lambda game_dir, plan, on_msg, on_step:
                self.rendered.append(plan),
        )

    def _run(self, ns, win, plan, **kwargs):
        with mock.patch.object(lw, "mods", self.mods), \
             mock.patch.object(lw, "load_cfg", lambda: {}), \
             mock.patch.object(lw, "threading",
                               types.SimpleNamespace(Thread=InlineThread)):
            ns._apply_mod_plan(win, r"C:\game", plan, ns._on_applied,
                               "10.0.0.5", **kwargs)

    def test_a_taken_back_decline_renders_a_freshly_resolved_plan(self):
        fresh = make_plan(entries=[entry("cool.mod")],
                          to_deactivate=["other.mod"])
        applied = []
        ns = launcher(_set_sync_busy=lambda busy: None,
                      _on_applied=lambda win: applied.append(win),
                      _build_mod_plan=lambda game_dir, host: ("plan", (fresh, [])))
        bind(ns, "_apply_mod_plan", "_replan_for_undeclined")
        win = FakeWindow(declined=[])
        # What the window handed back: it dropped the kept row from
        # to_deactivate itself, and cool.mod was never an entry to begin with.
        self._run(ns, win, make_plan(), was_declined={"cool.mod"},
                  was_deactivating=["other.mod"])

        self.assertEqual(self.rendered, [fresh])
        self.assertEqual(self.saved_declines, [("10.0.0.5", [])])
        self.assertEqual([e.mod_id for e in fresh.entries], ["cool.mod"])
        # A plain Keep lives only in the window, so the fresh plan has to be
        # re-filtered or it would deactivate a mod the user just kept.
        self.assertEqual(fresh.to_deactivate, [])
        self.assertEqual(win.retargeted, [fresh])
        self.assertEqual(len(applied), 1)

    def test_a_still_declined_mod_stays_out_of_the_fresh_plan(self):
        """The re-plan is driven by the declines just saved, but a failed save
        must not be able to install something the user said no to."""
        fresh = make_plan(entries=[entry("cool.mod"), entry("nope.mod")])
        ns = launcher(_set_sync_busy=lambda busy: None,
                      _on_applied=lambda win: None,
                      _build_mod_plan=lambda game_dir, host: ("plan", (fresh, [])))
        bind(ns, "_apply_mod_plan", "_replan_for_undeclined")
        self._run(ns, FakeWindow(declined=["nope.mod"]), make_plan(),
                  was_declined={"cool.mod", "nope.mod"}, was_deactivating=[])

        self.assertEqual([e.mod_id for e in self.rendered[0].entries], ["cool.mod"])

    def test_a_failed_re_plan_is_reported_not_swallowed(self):
        """The plan on screen is precisely the one that can't install what the
        row promised, so falling back to it would be the original bug."""
        ns = launcher(_set_sync_busy=lambda busy: None,
                      _on_applied=lambda win: None,
                      _build_mod_plan=lambda game_dir, host:
                          ("skip", "Couldn't reach the mod index: nope"))
        bind(ns, "_apply_mod_plan", "_replan_for_undeclined")
        win = FakeWindow(declined=[])
        self._run(ns, win, make_plan(), was_declined={"cool.mod"},
                  was_deactivating=[])

        self.assertEqual(self.rendered, [])
        self.assertEqual(len(win.finished), 1)
        ok, message = win.finished[0]
        self.assertFalse(ok)
        self.assertIn("cool.mod", message)

    def test_an_unavailable_recommendation_says_so(self):
        """Re-planning can't produce an entry for a mod no configured source
        carries, and a recommendation missing that way isn't blocking - so
        without this notice it would look like the no-op being fixed here."""
        ns = launcher(_set_sync_busy=lambda busy: None,
                      _on_applied=lambda win: None,
                      _build_mod_plan=lambda game_dir, host: ("plan", (make_plan(), [])))
        bind(ns, "_apply_mod_plan", "_replan_for_undeclined")
        self._run(ns, FakeWindow(declined=[]), make_plan(),
                  was_declined={"cool.mod"}, was_deactivating=[])

        self.assertTrue(any("cool.mod" in msg and tag == "warn"
                            for msg, tag in ns.printed), ns.printed)
        self.assertEqual(len(self.rendered), 1)

    def test_nothing_taken_back_renders_the_plan_that_was_shown(self):
        """The re-plan is only for the case the window can't express. Everything
        else must stay a single, already-resolved apply."""
        plan = make_plan(entries=[entry("a.mod")])
        ns = launcher(_set_sync_busy=lambda busy: None,
                      _on_applied=lambda win: None,
                      _build_mod_plan=lambda game_dir, host: self.fail(
                          "re-planned with nothing taken back"))
        bind(ns, "_apply_mod_plan", "_replan_for_undeclined")
        self._run(ns, FakeWindow(declined=["cool.mod"]), plan,
                  was_declined={"cool.mod"}, was_deactivating=[])

        self.assertEqual(self.rendered, [plan])


class CheckKeysBySameHostAsJoin(unittest.TestCase):
    """FINDING: Sync keyed declines and the mods-list cache by the raw field
    text while the join path keys everything by the resolved IP, so on a DNS
    name the two flows were reading different declines."""

    def _check(self, resp):
        ns = launcher(_check_status=FakeVar(), _check_label=FakeWidget(),
                      _check_btn=FakeWidget(), v_port=FakeVar(),
                      _show_sync_button=lambda show: shown.append(show))
        shown = []
        bind(ns, "_run_check", "_check_ok")
        with mock.patch.object(lw, "ping_server", lambda host: (resp, 7)), \
             mock.patch.object(lw, "_resolve_ip_for_game",
                               lambda host: "10.0.0.5"):
            ns._run_check("myserver.example")
        return ns, shown

    def test_checked_host_is_the_resolved_one(self):
        ns, _ = self._check({"status": "pong", "mods_hash": "h", "mods_count": 3})
        self.assertEqual(ns._checked_host, "10.0.0.5")

    def test_what_was_typed_is_kept_for_display(self):
        ns, _ = self._check({"status": "pong", "mods_hash": "h", "mods_count": 3})
        self.assertEqual(ns._checked_display, "myserver.example")

    def test_a_zero_mod_server_still_offers_sync(self):
        _, shown = self._check({"status": "pong", "mods_hash": "h", "mods_count": 0})
        self.assertEqual(shown, [True])

    def test_a_server_that_reports_nothing_does_not(self):
        _, shown = self._check({"status": "pong"})
        self.assertEqual(shown, [False])


class NoModsVersusNoModInfo(unittest.TestCase):
    """FINDING: a server running no mods reported an empty list, which the
    launcher treated as "nothing to sync" - so the deactivations the Sync button
    promises never happened, on either path."""

    def _get_server_mods(self, resp, fetched=None):
        ns = launcher(_mods_list_cache={}, _server_untracked=[])
        bind(ns, "_get_server_mods")
        fetch = (lambda host: fetched) if fetched is not None else None
        with mock.patch.object(lw, "ping_server", lambda host: (resp, 7)), \
             mock.patch.object(lw, "fetch_server_mods", fetch or (lambda host: ([], []))):
            return ns._get_server_mods("10.0.0.5")

    def test_an_empty_list_is_an_answer(self):
        self.assertEqual(self._get_server_mods({"mods_hash": "h"}), [])

    def test_no_hash_is_no_answer(self):
        self.assertIsNone(self._get_server_mods({}))

    def test_a_reported_list_comes_back(self):
        mods_list = [{"id": "a.mod", "version": "1.0.0", "client_side": True}]
        self.assertEqual(
            self._get_server_mods({"mods_hash": "h"}, fetched=(mods_list, [])),
            mods_list)

    def _build(self, server_mods):
        planned = []
        fake_mods = types.SimpleNamespace(
            ModManagerError=ModManagerError,
            adopt_installed_mods=lambda game_dir: [],
            list_repos=lambda cfg: ["https://repo"],
            fetch_indexes=lambda bases: [],
            list_pinned=lambda cfg: [],
            list_declined=lambda cfg, host: [],
            plan_join=lambda game_dir, sm, index, bases, pinned, declined:
                planned.append(sm) or make_plan(to_deactivate=["local.mod"]),
        )
        ns = launcher(_get_server_mods=lambda host: server_mods)
        bind(ns, "_build_mod_plan")
        with mock.patch.object(lw, "mods", fake_mods), \
             mock.patch.object(lw, "load_cfg", lambda: {}), \
             mock.patch.object(lw, "_melonloader_installed", lambda d: True), \
             mock.patch.object(lw, "_tavernlib_installed", lambda d: True):
            kind, payload = ns._build_mod_plan(r"C:\game", "10.0.0.5")
        return kind, payload, planned

    def test_a_zero_mod_server_is_planned_against(self):
        """Its empty set is exactly what deactivates whatever is active here,
        which is what the Sync button promises."""
        kind, payload, planned = self._build([])
        self.assertEqual(kind, "plan")
        self.assertEqual(planned, [[]])
        self.assertEqual(payload[0].to_deactivate, ["local.mod"])

    def test_a_server_with_no_mod_info_skips(self):
        kind, payload, planned = self._build(None)
        self.assertEqual(kind, "skip")
        self.assertEqual(planned, [])
        self.assertIn("didn't report", payload)


if __name__ == "__main__":
    unittest.main()
