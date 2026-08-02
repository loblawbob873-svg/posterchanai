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

    def test_a_bot_change_restarts_only_the_bots(self):
        self.assertEqual(dt.units_for(["botframework/main.py"]), [dt.BOTS])
        self.assertEqual(dt.units_for(["app/services/bot_manager_service.py"]), [dt.BOTS])

    def test_a_stream_change_restarts_only_media(self):
        self.assertEqual(dt.units_for(["app/services/stream_service.py"]), [dt.MEDIA])
        self.assertEqual(dt.units_for(["app/services/turn_service.py"]), [dt.MEDIA])

    def test_a_router_change_spares_relay_media_and_bots(self):
        """The common case, and the one the whole split is for: shipping a router fix must not drop
        every connected Nostr client or kill a live stream."""
        got = dt.units_for(["app/routers/client.py"])
        self.assertIn(dt.APP, got)
        for spared in (dt.RELAY, dt.MEDIA, dt.BOTS):
            self.assertNotIn(spared, got, f"a router change must not restart {spared}")

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

    def test_every_mapped_unit_is_a_real_unit_template(self):
        """A mapping naming a unit that does not exist would silently fail to restart at deploy."""
        for _prefix, units in dt._OWNED:
            for u in units:
                self.assertIn(u, dt.ALL, u)
        for u in dt.ALL:
            if u == dt.APP:
                name = "posterchanai.service"
            else:
                name = u
            self.assertTrue(os.path.exists(os.path.join(REPO, name)),
                            f"{name} template missing from the repo")


if __name__ == "__main__":
    unittest.main()
