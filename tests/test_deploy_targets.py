"""Which units a deploy restarts (scripts/deploy_targets.py).

Run: venv-unified/bin/python -m unittest tests.test_deploy_targets

The role split only pays off if the deploy knows what it touched. Restarting everything on every push
gives back exactly the outage the split removed — dropped Nostr clients, live streams killed
mid-broadcast, active calls dropped, nine bots restarted into their startup race.

The asymmetry that shapes every case below: OVER-restarting costs an outage you can see;
UNDER-restarting ships code that is running nowhere, and presents as "the fix didn't work" with no
error in any log. So anything unrecognised must map to everything.
"""
import importlib.util
import os
import unittest
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "deploy_targets", os.path.join(REPO, "scripts", "deploy_targets.py"))
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)


class Mapping(unittest.TestCase):
    def test_ui_only_changes_restart_nothing(self):
        """The pre-existing rule this must not break: a static/client change is served from
        router.lan's own checkout, so it must never take the ~90s restart outage."""
        for p in ("static/js/client/app.js", "static/css/client.css", "docs/GIT.md",
                  "README.md", "tests/test_role_split.py", "scripts/check_client_icons.py"):
            self.assertEqual(dt.units_for([p]), [], p)

    def test_a_relay_change_leaves_the_app_and_streams_alone(self):
        for p in ("relay_main.py", "app/services/nostr_relay/server.py"):
            self.assertEqual(dt.units_for([p]), [dt.RELAY], p)

    def test_web_search_spares_the_relay(self):
        """search_service is the app's and the pollers' — MEASURED: relay_main, app.worker,
        tor_service, http_proxy_service, git_http_service and stream_service all leave it out of
        sys.modules. Unmapped it meant "everything", so shipping the Web Search screen dropped every
        connected Nostr client on both nodes for a file the relay never loads."""
        got = dt.units_for(["app/services/search_service.py"])
        self.assertIn(dt.APP, got)
        self.assertNotIn(dt.RELAY, got)
        self.assertNotIn(dt.MEDIA, got)
        self.assertEqual(dt.units_for(["app/routers/websearch.py"]), dt.units_for(["app/routers/client.py"]))

    def test_the_container_build_restarts_nothing(self):
        """A systemd node runs none of it: compose is the OTHER way to deploy this app, and
        docker/searxng/settings.yml is read by ./install.sh — a person running a command, not a
        service. Unmapped, adding a compose service restarted all seven units on both bare-metal
        nodes."""
        for p in ("docker-compose.yml", "Dockerfile", "docker/searxng/settings.yml",
                  "docker/proxy/nginx.conf"):
            self.assertEqual(dt.units_for([p]), [], p)

    def test_the_calendar_and_the_document_store_spare_the_relay(self):
        """MEASURED: relay_main, tor, proxy and git leave nostr_store out of sys.modules, and the
        CalDAV plugins run in the app's own process. Unmapped they meant "everything", so editing a
        document helper dropped every connected Nostr client."""
        for p in ("app/services/nostr_store.py", "app/services/caldav_store.py",
                  "app/services/caldav/storage.py"):
            got = dt.units_for([p])
            self.assertIn(dt.APP, got, p)
            self.assertNotIn(dt.RELAY, got, p)

    def test_nostr_store_restarts_media_because_media_imports_it_transitively(self):
        """The media role imports ``stream_service -> settings_store -> nostr_store``.

        This is intentionally different from a relay-store change: changing
        ``app/services/nostr_relay/store.py`` only restarts the relay, while changing the datastore
        client must also restart media or that long-running process keeps executing the old module.
        Keep this explicit because the two similarly named stores made a correct intelligent deploy
        look like an unrelated media restart during the folder-pagination repair.
        """
        self.assertEqual(dt.units_for(["app/services/nostr_relay/store.py"]), [dt.RELAY])
        got = dt.units_for(["app/services/nostr_store.py"])
        self.assertIn(dt.MEDIA, got)
        self.assertNotIn(dt.RELAY, got)

        stream = (Path(REPO) / "app/services/stream_service.py").read_text(encoding="utf-8")
        settings = (Path(REPO) / "app/services/settings_store.py").read_text(encoding="utf-8")
        self.assertIn("from app.services import settings_store", stream)
        self.assertIn("from app.services import nostr_store as store", settings)

    def test_a_bot_change_restarts_only_the_bots(self):
        self.assertEqual(dt.units_for(["botframework/main.py"]), [dt.BOTS])
        self.assertEqual(dt.units_for(["app/services/bot_manager_service.py"]), [dt.BOTS])

    def test_a_stream_change_restarts_only_media(self):
        self.assertEqual(dt.units_for(["app/services/stream_service.py"]), [dt.MEDIA])
        self.assertEqual(dt.units_for(["app/services/turn_service.py"]), [dt.MEDIA])

    def test_a_router_change_spares_the_relay_and_the_streams(self):
        """The common case, and the one the split is for: shipping a router fix must not drop every
        connected Nostr client or kill a live stream mid-broadcast.

        The BOTS are deliberately not on that list. They stay in the app process because Admin → Bots
        drives them through an in-process registry (split out, the UI showed every bot as stopped
        while they were running, and a button press would have spawned a second copy of each). So
        `BOTS is APP`, and a router change restarts them — the accepted cost of a working admin UI."""
        got = dt.units_for(["app/routers/client.py"])
        self.assertIn(dt.APP, got)
        for spared in (dt.RELAY, dt.MEDIA):
            self.assertNotIn(spared, got, f"a router change must not restart {spared}")

    def test_monero_changes_restart_api_and_output_scheduler_without_disconnecting_relays(self):
        """The worker imports pooled maintenance and shared RPC errors/amount helpers."""
        for path in ("app/services/monero_wallet_service.py",
                     "app/services/monero_user_wallets.py"):
            got = dt.units_for([path])
            self.assertEqual(set(got), {dt.APP, dt.WORKER}, path)
            self.assertNotIn(dt.RELAY, got)
            self.assertNotIn(dt.MEDIA, got)

    def test_bot_code_restarts_the_app_because_that_is_where_bots_run(self):
        """If BOTS ever stops aliasing APP without a bots unit existing, bot code changes would
        restart a unit that isn't there — i.e. ship bot code that never takes effect."""
        self.assertEqual(dt.BOTS, dt.APP)
        self.assertEqual(dt.units_for(["botframework/main.py"]), [dt.APP])

    def test_deploy_tooling_restarts_nothing(self):
        """sync.sh and install.sh are read fresh by whoever runs them and imported by no service.
        They were UNMAPPED, which means "could affect anything" and therefore every unit — so editing
        sync.sh restarted the relay and put every connected web client into "reconnecting". The
        tooling that exists to avoid downtime was causing it."""
        for p in ("sync.sh", "install.sh"):
            self.assertEqual(dt.units_for([p]), [], p)

    def test_the_native_app_projects_restart_nothing(self):
        """desktop/, mobile/ and extension/ are built and shipped by CI to a GitHub release, and no
        service on a node imports, reads or serves a byte of any of them — /apk, /desktop/* and
        /extension/* are 302s to those releases.

        mobile/ was UNMAPPED, which means "could affect anything" and therefore EVERY unit: a two-line
        comment fix in mobile/build-www.sh restarted all seven on both nodes — every connected Nostr
        client dropped, streams killed mid-broadcast, the bots bounced — on a commit that otherwise
        touched only static/ and templates/, i.e. one service. Exactly the outage the role split
        exists to remove, and the third time this same hole has been found in a different directory."""
        for p in ("desktop/main.js", "desktop/build-www.sh", "desktop/tor.js",
                  "mobile/build-www.sh", "mobile/package.json",
                  "mobile/android/app/src/main/AndroidManifest.xml",
                  "extension/manifest.json", "extension/background.js"):
            self.assertEqual(dt.units_for([p]), [], p)

    def test_a_native_app_change_does_not_dilute_a_real_change(self):
        """The other half: inert must mean "adds nothing", never "cancels something". A commit that
        touches the APK bundler AND a router still restarts the router's units."""
        # Compared as sets: units_for's ORDER is not part of its contract (sync.sh restarts whatever it
        # is handed), and pinning it here would fail on an unrelated reshuffle.
        self.assertEqual(set(dt.units_for(["mobile/build-www.sh", "app/routers/client.py"])),
                         {dt.APP, dt.WORKER})
        self.assertEqual(dt.units_for(["desktop/main.js", "relay_main.py"]), [dt.RELAY])

    def test_the_launchers_still_restart_everything(self):
        """run-*.sh IS each unit's ExecStart, so a change there genuinely needs every unit — the
        inert list must not grow to swallow them."""
        for p in ("run-intel.sh", "run-nvidia.sh"):
            self.assertEqual(sorted(dt.units_for([p])), sorted(dt.ALL), p)

    def test_shared_code_restarts_everything(self):
        """app/database.py, app/models.py, settings_store, run.py and the role plumbing itself are
        imported by every role — under-restarting these leaves stale code running somewhere."""
        for p in ("app/database.py", "app/models.py", "app/services/settings_store.py",
                  "run.py", "app/role.py", "app/role_runner.py", "requirements.txt"):
            self.assertEqual(sorted(dt.units_for([p])), sorted(dt.ALL), p)

    def test_an_unrecognised_path_restarts_everything(self):
        self.assertEqual(sorted(dt.units_for(["something/brand_new.py"])), sorted(dt.ALL))

    def test_mixed_changes_take_the_union(self):
        got = dt.units_for(["app/services/nostr_relay/server.py", "botframework/main.py"])
        self.assertEqual(sorted(got), sorted([dt.RELAY, dt.BOTS]))

    def test_one_shared_file_in_a_mixed_set_still_means_everything(self):
        got = dt.units_for(["app/services/nostr_relay/server.py", "app/models.py"])
        self.assertEqual(sorted(got), sorted(dt.ALL))

    def test_inert_files_do_not_dilute_a_real_change(self):
        got = dt.units_for(["docs/GIT.md", "app/services/stream_service.py"])
        self.assertEqual(got, [dt.MEDIA])

    def test_a_command_change_does_not_touch_the_relay(self):
        """The command layer is the web UI websocket + Telegram, both in the app. Measured: no role
        module outside the app imports command_service. Unmapped it meant "everything", so adding one
        command alias restarted the relay and dropped every connected Nostr client."""
        got = dt.units_for(["app/services/command_service/core.py"])
        self.assertEqual(sorted(got), sorted([dt.APP, dt.WORKER]))
        self.assertNotIn(dt.RELAY, got)
        self.assertNotIn(dt.MEDIA, got)

    def test_a_git_hook_change_restarts_nothing(self):
        """install_hooks writes a shim that `exec`s "<python> <checkout>/git_hooks/<f>.py", so
        git-receive-pack spawns a fresh process per push and reads the file off disk every time — a
        pull is all a hook change needs. Unmapped it meant "everything": editing one hook's log
        message restarted all seven units on BOTH nodes and dropped every connected Nostr client."""
        self.assertEqual(dt.units_for(["git_hooks/post_receive.py"]), [])
        self.assertEqual(dt.units_for(["git_hooks/pre_receive.py"]), [])

    def test_the_git_server_itself_restarts_only_the_git_unit(self):
        self.assertEqual(dt.units_for(["git_host_main.py"]), [dt.GIT])

    def test_a_hook_change_does_not_dilute_a_real_change(self):
        got = dt.units_for(["git_hooks/post_receive.py", "app/services/nostr_relay/server.py"])
        self.assertEqual(got, [dt.RELAY])

    def test_every_mapped_unit_is_a_real_unit_template(self):
        """A mapping naming a unit that does not exist would silently fail to restart at deploy."""
        # Against UNITS, not ALL: ALL is the "restart everything" set, and the shell keeper is
        # deliberately absent from it (restarting it destroys open shells). "Is this a real unit" and
        # "is this restarted by a conservative fallback" are different questions.
        for _prefix, units in dt._OWNED:
            for u in units:
                self.assertIn(u, dt.UNITS, u)
        for u in dt.UNITS:
            if u == dt.APP:
                name = "posterchanai.service"
            else:
                name = u
            self.assertTrue(os.path.exists(os.path.join(REPO, name)),
                            f"{name} template missing from the repo")


if __name__ == "__main__":
    unittest.main()
