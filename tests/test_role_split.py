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

    def test_roles_compose(self):
        """The deployed layout is `app,bots`: the bot manager has to stay with the web app because
        Admin -> Bots reads its live in-process registry. Split out, the UI showed every bot as
        stopped while they were running, and reconcile_now() from a button press would have made the
        app spawn a SECOND copy of every bot."""
        with self._as("app,bots"):
            self.assertEqual(role.roles(), {"app", "bots"})
            self.assertTrue(role.owns("app"))
            self.assertTrue(role.owns("bots"))
            for c in ("relay", "worker", "media"):
                self.assertFalse(role.owns(c), c)

    def test_a_comma_list_with_one_bad_entry_falls_back_to_all(self):
        """Half-applying a typo'd list would be worse than ignoring it: 'app,botz' must not leave the
        node supervising only the app while nothing runs the bots."""
        with self._as("app,botz"):
            self.assertEqual(role.current(), "all")
            for c in COMPONENTS:
                self.assertTrue(role.owns(c), c)

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


class NoSpawningWhatYouDoNotOwn(unittest.TestCase):
    """The failure class the split keeps producing: app-side code driving a component whose state
    lives in ANOTHER process.

    Two real instances. The bots one shipped: Admin → Bots reads an in-process registry, so every bot
    showed as stopped while running, and reconcile_now() from a button would have made the app spawn
    a SECOND copy of each (fixed by keeping bots in the app — role 'app,bots'). The relay one is
    asserted here: restart_nostr_relay() is reached from an ordinary Settings save, and its tail is
    _spawn_relay() — from an app-role process that is a second relay on one Postgres, crash-looping
    on the bound :3052."""

    def test_restart_relay_does_not_spawn_when_this_process_is_not_the_owner(self):
        from app.services.nostr_relay import thread as t
        with mock.patch.dict(os.environ, {"POSTERCHANAI_ROLE": "app,bots"}), \
             mock.patch.object(t, "_spawn_relay") as spawn, \
             mock.patch.object(t, "_restart_relay_elsewhere", return_value={"ok": True}) as delegate:
            t.restart_nostr_relay()
        spawn.assert_not_called()
        delegate.assert_called_once()

    def test_restart_relay_still_spawns_locally_when_it_does_own_the_relay(self):
        """role 'all' (the default, and every un-split node) must behave exactly as before."""
        from app.services.nostr_relay import thread as t
        with mock.patch.dict(os.environ, {"POSTERCHANAI_ROLE": "all"}), \
             mock.patch.object(t, "_restart_relay_elsewhere") as delegate, \
             mock.patch.object(t, "_spawn_relay"), \
             mock.patch.object(t, "stop_nostr_relay"), \
             mock.patch.object(t, "_read_config", return_value={"enabled": True}):
            t.restart_nostr_relay()
        delegate.assert_not_called()

    def test_the_delegate_refuses_a_pid_that_is_not_our_relay(self):
        """It SIGTERMs a pid read from a FILE, so a stale/recycled pid must never be signalled. Uses a
        real status file and a real (absent) pid so the /proc identity check actually runs."""
        import json as _json
        import tempfile
        from app.services.nostr_relay import thread as t
        d = tempfile.mkdtemp()
        status = os.path.join(d, "s.json")
        with open(status, "w") as f:
            _json.dump({"pid": 4242424}, f)      # far above pid_max on any normal box
        with mock.patch.object(t, "_relay_paths", return_value={"status": status}), \
             mock.patch.object(t, "_relay_db_path", return_value="x"), \
             mock.patch("os.kill") as kill:
            res = t._restart_relay_elsewhere()
        kill.assert_not_called()
        self.assertFalse(res.get("ok"), res)

    def test_the_delegate_actually_signals_the_real_relay(self):
        """The POSITIVE case, and the one whose absence let a real bug through: the first cut built
        the repo path with three dirnames instead of four, landing on `app/` — a string that never
        appears in the relay's cmdline — so the identity check refused every time and the delegate
        was silently dead. Only refusal cases were covered, so everything still 'passed'.

        Uses THIS process's own pid, whose cmdline genuinely contains the repo path, and stands in for
        the relay by asserting the identity check accepts it once 'relay' is present."""
        import json as _json
        import tempfile
        from app.services.nostr_relay import thread as t
        d = tempfile.mkdtemp()
        status = os.path.join(d, "s.json")
        with open(status, "w") as f:
            _json.dump({"pid": os.getpid()}, f)
        real_open = open

        def fake_open(path, *a, **kw):
            # Our own cmdline is pytest's; substitute a relay-looking one for the identity check.
            if str(path).startswith("/proc/"):
                import io
                return io.BytesIO(f"{REPO}/venv/bin/python run.py --role relay".encode())
            return real_open(path, *a, **kw)

        with mock.patch.object(t, "_relay_paths", return_value={"status": status}), \
             mock.patch.object(t, "_relay_db_path", return_value="x"), \
             mock.patch("builtins.open", fake_open), \
             mock.patch("os.kill") as kill:
            res = t._restart_relay_elsewhere()
        self.assertTrue(res.get("ok"), res)
        kill.assert_called_once()

    def test_the_delegate_refuses_a_pid_whose_cmdline_is_not_ours(self):
        """The dangerous case: the pid EXISTS but belongs to something else entirely."""
        import json as _json
        import tempfile
        from app.services.nostr_relay import thread as t
        d = tempfile.mkdtemp()
        status = os.path.join(d, "s.json")
        with open(status, "w") as f:
            _json.dump({"pid": 1}, f)            # pid 1 is init — exists, definitely not our relay
        with mock.patch.object(t, "_relay_paths", return_value={"status": status}), \
             mock.patch.object(t, "_relay_db_path", return_value="x"), \
             mock.patch("os.kill") as kill:
            res = t._restart_relay_elsewhere()
        kill.assert_not_called()
        self.assertFalse(res.get("ok"), res)


class NewSplitsDoNotSpawnTwice(unittest.TestCase):
    """tor / proxy / git, split in the same pass. Each had a control path reachable from the WEB APP
    that called the component's own start_*() — from a non-owning process that is a second git host
    on the same port, or a tor toggle that silently did nothing."""

    def test_git_host_delegates_instead_of_spawning(self):
        """admin.py reconciles the git host on a Settings save (stop_git_http(); start_git_http()).
        Unguarded that puts a SECOND git host on :3053 as a child of the web app."""
        from app.services import git_http_service as g
        with mock.patch.dict(os.environ, {"POSTERCHANAI_ROLE": "app,bots"}), \
             mock.patch.object(g, "_spawn") as spawn, \
             mock.patch("app.role.restart_owner_process", return_value={"ok": True}) as delegate:
            g.start_git_http()
        spawn.assert_not_called()
        delegate.assert_called_once()

    def test_git_host_still_spawns_when_it_owns_it(self):
        from app.services import git_http_service as g
        with mock.patch.dict(os.environ, {"POSTERCHANAI_ROLE": "all"}), \
             mock.patch("app.role.restart_owner_process") as delegate, \
             mock.patch.object(g, "_read_config", return_value={"enabled": False}):
            g.start_git_http()
        delegate.assert_not_called()

    def test_set_onion_restarts_the_tor_unit_when_it_does_not_own_tor(self):
        """primary_service() is None off-owner, so the live SIGHUP reload silently did nothing and the
        admin toggle appeared to work while the .onion never changed."""
        from app.services import tor_service as t
        with mock.patch.dict(os.environ, {"POSTERCHANAI_ROLE": "app,bots"}), \
             mock.patch("app.role.restart_owner_by_cmdline", return_value={"ok": True}) as delegate, \
             mock.patch.object(t, "get_onion_address_global", return_value="abc.onion"), \
             mock.patch.object(t, "primary_service") as prim:
            res = t.set_onion(True, "127.0.0.1:3051", 3052)
        prim.assert_not_called()
        delegate.assert_called_once()
        self.assertEqual(res, "abc.onion")

    def test_owner_restart_refuses_a_cmdline_that_is_not_ours(self):
        from app import role
        with mock.patch("os.kill") as kill:
            res = role.restart_owner_by_cmdline("--role definitely-not-a-real-role-xyz")
        kill.assert_not_called()
        self.assertFalse(res.get("ok"))


class RunPyDispatch(unittest.TestCase):
    def test_every_role_has_a_dispatch_or_is_the_app(self):
        with open(os.path.join(REPO, "run.py"), encoding="utf-8") as f:
            src = f.read()
        for r in ("relay", "worker"):
            self.assertIn(f'role == "{r}"', src, f"run.py does not dispatch role {r}")
        # Dispatch is DERIVED from role_runner._ROLE_SERVICES, not a hardcoded tuple: a role added
        # to the runner but missed in run.py falls through to uvicorn and starts a second web server
        # on the app's port instead of the component.
        self.assertIn("if role in _ROLE_SERVICES:", src)
        # 'all' and 'app' fall through to uvicorn — they ARE the web server.
        # ROLES has ONE definition (app/role.py) which run.py imports, so the CLI's --role choices,
        # the unit files and the ownership predicate cannot disagree about what a valid role is. A
        # second literal here is exactly how the own-media-hosts list ended up in two places.
        # run.py must not carry its own list of valid roles: app.role.current() validates, and a
        # second copy here would let the CLI and the ownership predicate disagree.
        self.assertNotRegex(src, r"(?m)^ROLES\s*=\s*\(",
                            "run.py must not redefine ROLES — app/role.py owns it")
        self.assertEqual(set(role.ROLES),
                         {"all", "app", "relay", "worker", "media", "bots", "tor", "proxy", "git",
                          "shell"})

    def test_role_runner_covers_the_roles_run_py_delegates_to_it(self):
        from app import role_runner
        self.assertEqual(set(role_runner._ROLE_SERVICES),
                         {"media", "bots", "tor", "proxy", "git", "shell"})
        for svcs in role_runner._ROLE_SERVICES.values():
            for label, module, start_fn, stop_fn in svcs:
                mod = __import__(module, fromlist=["*"])
                self.assertTrue(hasattr(mod, start_fn), f"{module}.{start_fn} missing")
                self.assertTrue(hasattr(mod, stop_fn), f"{module}.{stop_fn} missing")

    def test_a_role_does_not_stay_up_with_unhydrated_off_by_default_services(self):
        from app import role_runner
        src = open(role_runner.__file__, encoding="utf-8").read()
        failed = src[src.index("except Exception as e:", src.index("_bootstrap_settings()")):]
        self.assertIn("return 1", failed[:900])
        self.assertNotIn("services may use defaults", failed[:900])

    def test_bootstrap_rejects_a_swallowed_database_hydrate_failure(self):
        """Postgres recovery is reported by hydrate as 0 + not-hydrated, not an exception."""
        from app import role_runner
        from app.services import settings_store

        fake_db = mock.Mock()
        with mock.patch.object(settings_store, "load_local"), \
             mock.patch.object(settings_store, "hydrate_from_db", return_value=0), \
             mock.patch.object(settings_store, "is_hydrated", return_value=False), \
             mock.patch("app.database.SessionLocal", return_value=fake_db):
            with self.assertRaisesRegex(RuntimeError, "not hydrated"):
                role_runner._bootstrap_settings()
        fake_db.close.assert_called_once()


class ProxyBootDefaults(unittest.TestCase):
    """The split Tor/proxy roles must survive the empty-cache window at early boot."""

    def test_proxy_missing_settings_uses_the_canonical_enabled_defaults(self):
        from app.services import http_proxy_service as proxy
        from app.services import settings_store

        def missing(_key, default=None):
            return default

        with mock.patch.object(settings_store, "get_bool", side_effect=missing), \
             mock.patch.object(settings_store, "get", side_effect=missing), \
             mock.patch.object(settings_store, "get_int", side_effect=missing), \
             mock.patch.object(proxy, "start_http_proxy_process") as start, \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTERCHANAI_PROXY_ENABLED", None)
            os.environ.pop("POSTERCHANAI_PROXY_SOCKS_HOST", None)
            self.assertTrue(proxy.start_from_settings())

        kwargs = start.call_args.kwargs
        self.assertEqual(kwargs["listen_port"], 8118)
        self.assertEqual(kwargs["socks_host"], "127.0.0.1")
        self.assertEqual(kwargs["socks_ports"], ["9052:us", "9062:ca"])

    def test_proxy_explicit_off_still_wins(self):
        from app.services import http_proxy_service as proxy
        from app.services import settings_store

        with mock.patch.object(settings_store, "get_bool", return_value=False), \
             mock.patch.object(proxy, "start_http_proxy_process") as start:
            self.assertFalse(proxy.start_from_settings())
        start.assert_not_called()

    def test_tor_missing_settings_uses_the_canonical_enabled_defaults(self):
        from app.services import settings_store
        from app.services import tor_service

        def missing(_key, default=None):
            return default

        with mock.patch.object(settings_store, "get_bool", side_effect=missing), \
             mock.patch.object(settings_store, "get", side_effect=missing), \
             mock.patch.object(settings_store, "get_int", side_effect=missing), \
             mock.patch.object(tor_service, "start_tor_service", return_value=object()) as start, \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTERCHANAI_TOR_ENABLED", None)
            os.environ.pop("POSTERCHANAI_TOR2_ENABLED", None)
            self.assertTrue(tor_service.start_from_settings())

        self.assertEqual(start.call_count, 2)
        self.assertEqual(start.call_args_list[0].kwargs["socks_port"], 9052)
        self.assertEqual(start.call_args_list[1].kwargs["socks_port"], 9062)

    def test_installed_proxy_unit_pulls_in_tor(self):
        root = os.path.dirname(os.path.dirname(__file__))
        template = open(os.path.join(root, "posterchanai-proxy.service"), encoding="utf-8").read()
        installer = open(os.path.join(root, "scripts", "install_services.sh"), encoding="utf-8").read()
        self.assertIn("Wants=network-online.target posterchanai-tor.service", template)
        self.assertIn("echo 'Wants=network-online.target posterchanai-tor.service'", installer)

    def test_proxy_waits_for_network_and_reports_only_listener_readiness(self):
        root = os.path.dirname(os.path.dirname(__file__))
        template = open(os.path.join(root, "posterchanai-proxy.service"), encoding="utf-8").read()
        installer = open(os.path.join(root, "scripts", "install_services.sh"), encoding="utf-8").read()
        self.assertIn("network-online.target", template)
        self.assertIn("Type=notify", template)
        self.assertIn("NotifyAccess=main", template)
        self.assertIn('after="network-online.target posterchanai-tor.service"', installer)
        self.assertIn('then echo notify', installer)
        self.assertIn("echo 'NotifyAccess=main'", installer)

    def test_role_notifies_systemd_after_service_start_readiness(self):
        from app import role_runner

        notify = mock.Mock()
        context = mock.Mock()
        context.__enter__ = mock.Mock(return_value=notify)
        context.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "@pc-proxy-ready"}), \
             mock.patch.object(role_runner.socket, "socket", return_value=context):
            role_runner._notify_ready()
        notify.connect.assert_called_once_with("\0pc-proxy-ready")
        notify.sendall.assert_called_once_with(b"READY=1")

    def test_proxy_readiness_rejects_a_child_that_exits_before_bind(self):
        from app.services import http_proxy_service as proxy

        child = mock.Mock()
        child.poll.return_value = 98
        with self.assertRaisesRegex(RuntimeError, "before readiness"):
            proxy._wait_proxy_listener(child, "127.0.0.1", 8118, timeout=0.1)

    def test_proxy_readiness_requires_the_configured_listener(self):
        from app.services import http_proxy_service as proxy

        child = mock.Mock()
        child.poll.return_value = None
        clock = iter((0.0, 0.0, 0.2, 0.2))
        with mock.patch.object(proxy.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(proxy.time, "sleep"), \
             mock.patch.object(proxy.socket, "create_connection",
                               side_effect=ConnectionRefusedError("not listening")):
            with self.assertRaisesRegex(RuntimeError, "did not listen"):
                proxy._wait_proxy_listener(child, "127.0.0.1", 8118, timeout=0.1)

    def test_failed_readiness_cleans_child_and_allows_systemd_retry(self):
        from app.services import http_proxy_service as proxy

        child = mock.Mock()
        child.poll.return_value = None
        with mock.patch.object(proxy.subprocess, "Popen", return_value=child), \
             mock.patch.object(proxy, "_wait_proxy_listener", side_effect=RuntimeError("bind failed")):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                proxy.start_http_proxy_process()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=2)
        self.assertIsNone(proxy._proxy_process)

    def test_role_runner_monitors_a_started_child_after_readiness(self):
        from app import role_runner

        src = open(role_runner.__file__, encoding="utf-8").read()
        self.assertIn('getattr(importlib.import_module(module), "role_healthy", None)', src)
        self.assertIn("return 1 if unhealthy else 0", src)
        from app.services import http_proxy_service as proxy
        child = mock.Mock()
        child.poll.side_effect = (None, 1)
        proxy._proxy_process = child
        try:
            self.assertTrue(proxy.role_healthy())
            self.assertFalse(proxy.role_healthy())
        finally:
            proxy._proxy_process = None


if __name__ == "__main__":
    unittest.main()
