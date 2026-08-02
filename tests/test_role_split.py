"""Running one component per process (run.py --role / app/role.py).

Run: venv-unified/bin/python -m unittest tests.test_role_split

Historically the web app supervised the relay, the worker, mediamtx, pion-turn, tor and nine bots, so
restarting to ship a one-line router change dropped every connected Nostr client, killed live streams
MID-BROADCAST, dropped active calls, and restarted the bots — which is where their startup-race
crashes cluster. The least stable component supervised the most stable ones.

Two properties are load-bearing and both are asserted here:

  * 'all' is the DEFAULT and still owns everything. That is what makes the split safe to deploy: a
    node whose unit file has not been updated keeps working exactly as before, and rolling back is
    repointing the unit rather than reverting code.
  * every start_X gated on a component has its stop_X gated on the SAME one. Getting that pair out of
    step leaks a subprocess — the app starts mediamtx because it owns it, then declines to stop it.
    (Written because the first cut imported the predicate function-locally in the startup handler, so
    every gated call in the shutdown handler was a NameError and nothing was ever stopped.)
"""
import os
import re
import unittest
from unittest import mock

from app import role

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENTS = ("relay", "worker", "media", "bots", "app")


def _main_py():
    with open(os.path.join(REPO, "app", "main.py"), encoding="utf-8") as f:
        return f.read()


class RoleOwnership(unittest.TestCase):
    def _as(self, r):
        return mock.patch.dict(os.environ, {"POSTERCHANAI_ROLE": r})

    def test_default_is_all_and_owns_everything(self):
        """The compatibility guarantee. If this fails, deploying the split silently stops a node's
        relay/bots/streams from ever starting."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTERCHANAI_ROLE", None)
            self.assertEqual(role.current(), "all")
            for c in COMPONENTS:
                self.assertTrue(role.owns(c), c)

    def test_an_unset_or_junk_role_still_means_all(self):
        for val in ("", "   ", "nonsense"):
            with self._as(val):
                for c in COMPONENTS:
                    self.assertTrue(role.owns(c), f"{val!r}/{c}")

    def test_each_role_owns_exactly_its_own_component(self):
        for r in ("relay", "worker", "media", "bots", "app"):
            with self._as(r):
                for c in COMPONENTS:
                    self.assertEqual(role.owns(c), c == r, f"role={r} component={c}")

    def test_the_app_role_supervises_none_of_the_split_components(self):
        """The whole point: restarting the web app must not touch them."""
        with self._as("app"):
            for c in ("relay", "worker", "media", "bots"):
                self.assertFalse(role.owns(c), c)

    def test_an_unmapped_component_defaults_to_running_with_the_app(self):
        """A component added later without a mapping keeps the pre-split behaviour rather than
        silently never starting anywhere — a missing feature is easier to spot than a missing
        supervisor."""
        with self._as("app"):
            self.assertTrue(role.owns("something_added_later"))


class MainPyGating(unittest.TestCase):
    # start_X -> the component it must be gated on
    EXPECTED = {
        "start_worker_process": "worker", "stop_worker_process": "worker",
        "start_bot_manager": "bots",      "stop_bot_manager": "bots",
        "start_nostr_relay": "relay",
        "start_turn_server": "media",     "stop_turn_server": "media",
        "start_stream_server": "media",   "stop_stream_server": "media",
    }

    def test_every_supervised_call_is_gated_on_its_component(self):
        src = _main_py()
        for call, comp in self.EXPECTED.items():
            pat = re.compile(r"if _owns\('%s'\):\s*%s\(\)" % (re.escape(comp), re.escape(call)))
            self.assertRegex(src, pat, f"{call}() is not gated on _owns('{comp}')")

    def test_start_and_stop_agree(self):
        """A start gated on one component and its stop on another leaks the subprocess."""
        src = _main_py()
        pairs = {}
        for comp, verb, name in re.findall(r"if _owns\('(\w+)'\):\s*(start|stop)_(\w+)\(\)", src):
            pairs.setdefault(name, {})[verb] = comp
        for name, m in pairs.items():
            if "start" in m and "stop" in m:
                self.assertEqual(m["start"], m["stop"],
                                 f"start_{name} is gated on {m['start']} but stop_{name} on {m['stop']}")

    def test_no_stop_is_left_ungated_when_its_start_is_gated(self):
        """The asymmetry the pair-check above CANNOT see, and which was real: stop_nostr_relay() was
        ungated while start_nostr_relay() was gated, so an app running as role 'app' would reach in
        and stop the relay owned by posterchanai-relay.service — restarting the web app would take
        the relay down with it, which is the exact outage the split removes."""
        src = _main_py()
        gated_starts = set(re.findall(r"if _owns\('\w+'\):\s*start_(\w+)\(\)", src))
        # …plus the gated-on-its-own-line form used where the call sits inside a try/import block.
        gated_starts |= set(re.findall(r"if _owns\('\w+'\):\s*\n\s*from [\w.]+ import start_(\w+)", src))
        for name in gated_starts:
            stop = f"stop_{name}()"
            if stop not in src:
                continue          # no stop function at all — nothing to leak
            for m in re.finditer(re.escape(stop), src):
                line_start = src.rfind("\n", 0, m.start()) + 1
                window = src[max(0, line_start - 200):m.end()]
                self.assertIn("_owns(", window,
                              f"{stop} is not gated but start_{name}() is — an app-role process "
                              f"would stop a component another unit owns")

    def test_the_predicate_is_imported_at_module_scope(self):
        """It is used by BOTH the startup and the shutdown handler. A function-local import in
        startup makes every gated call in shutdown a NameError — nothing is ever stopped."""
        src = _main_py()
        self.assertRegex(src, r"(?m)^from app\.role import owns as _owns$",
                         "_owns must be imported at module scope, not inside a handler")


class RunPyDispatch(unittest.TestCase):
    def test_every_role_has_a_dispatch_or_is_the_app(self):
        with open(os.path.join(REPO, "run.py"), encoding="utf-8") as f:
            src = f.read()
        for r in ("relay", "worker"):
            self.assertIn(f'role == "{r}"', src, f"run.py does not dispatch role {r}")
        self.assertIn('role in ("media", "bots")', src)
        # 'all' and 'app' fall through to uvicorn — they ARE the web server.
        # ROLES has ONE definition (app/role.py) which run.py imports, so the CLI's --role choices,
        # the unit files and the ownership predicate cannot disagree about what a valid role is. A
        # second literal here is exactly how the own-media-hosts list ended up in two places.
        self.assertIn("from app.role import ROLES", src)
        self.assertNotRegex(src, r"(?m)^ROLES\s*=\s*\(",
                            "run.py must import ROLES, not redefine it")
        self.assertEqual(set(role.ROLES), {"all", "app", "relay", "worker", "media", "bots"})

    def test_role_runner_covers_the_roles_run_py_delegates_to_it(self):
        from app import role_runner
        self.assertEqual(set(role_runner._ROLE_SERVICES), {"media", "bots"})
        for svcs in role_runner._ROLE_SERVICES.values():
            for label, module, start_fn, stop_fn in svcs:
                mod = __import__(module, fromlist=["*"])
                self.assertTrue(hasattr(mod, start_fn), f"{module}.{start_fn} missing")
                self.assertTrue(hasattr(mod, stop_fn), f"{module}.{stop_fn} missing")


if __name__ == "__main__":
    unittest.main()
