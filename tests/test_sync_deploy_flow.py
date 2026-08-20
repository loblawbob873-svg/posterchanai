"""What a deploy does to every node (sync.sh).

Run: venv-unified/bin/python -m unittest tests.test_sync_deploy_flow

Two rules, learned the hard way, that pull in opposite directions:

  PULL EVERYWHERE, ALWAYS.  A node left behind serves or runs OLD code with nothing in any log to
  say so, and the next bug hunt starts from source that isn't what's running. nas.lan sat three
  commits behind exactly this way -- a UI-only change was pulled to router.lan by hand because
  sync.sh restarts services, and nas was simply never touched. Invisible until someone asked
  whether the repos were in sync.

  RESTART ONLY WHAT CHANGED.  That is what the service split bought (scripts/deploy_targets.py) and
  it is the reason the hand-pulling started. A blanket restart drops every connected Nostr client,
  kills live streams mid-broadcast and drops active calls.

The resolution is that the two are separate decisions: the pull is free, only the restart costs an
outage. So sync.sh pulls unconditionally, restarts selectively, and VERIFIES -- because every pull
in it is best-effort, and a silent failure is the whole failure mode being guarded against.

These are source assertions rather than a live deploy: running the real thing pushes to production.
"""
import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC = os.path.join(REPO, "sync.sh")


def _src():
    with open(SYNC, encoding="utf-8") as fh:
        return fh.read()


class Syntax(unittest.TestCase):
    def test_sync_sh_parses(self):
        """A deploy script that does not parse fails AFTER committing and pushing, leaving the tree
        published and the nodes untouched -- the worst place to stop."""
        r = subprocess.run(["bash", "-n", SYNC], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class PullsEveryNode(unittest.TestCase):
    def test_router_lan_is_pulled(self):
        self.assertRegex(_src(), r"ssh router\.lan.*git reset --hard origin/master")

    def test_nas_lan_is_pulled(self):
        src = _src()
        self.assertIn("ssh nas.lan", src)
        self.assertIn("cd ~/posterchanai", src)

    def test_the_pulls_are_not_conditional_on_a_restart(self):
        """The regression that caused the drift: making the pull follow the restart decision. They
        are separate -- a UI-only deploy restarts nothing and must still reach every node."""
        src = _src()
        pull = src.index("ssh router.lan")
        targets = src.index('_TARGETS="$(')
        self.assertLess(pull, targets,
                        "router.lan must be pulled before the restart set is even computed")


class NothingTheServersDoNotRunCausesARestart(unittest.TestCase):
    """An unmapped path means "could affect anything", which means EVERY unit — so a file no service
    loads must be explicitly inert or it takes the whole cluster down to ship a comment. This has now
    happened for git hooks, for sync.sh itself, for the Dockerfiles and for the Electron app; the
    assertions are per-path because the failure is silent (a green deploy that dropped every client)."""

    def _units(self, *paths):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "deploy_targets", os.path.join(REPO, "scripts", "deploy_targets.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.units_for(list(paths))

    def test_the_desktop_app_restarts_nothing(self):
        # electron-builder output, shipped separately. The app is a window onto /client over HTTP, so
        # no unit imports, reads or serves anything under desktop/.
        self.assertEqual(self._units("desktop/main.js", "desktop/shell.html"), [])

    def test_client_assets_and_tooling_restart_nothing(self):
        self.assertEqual(self._units("static/js/client/sw.js", "static/offline.html",
                                     "tests/test_client_offline_shell.py",
                                     "scripts/deploy_targets.py"), [])

    def test_real_service_code_still_restarts_its_unit(self):
        # The guard above must not be so broad that it stops restarting what genuinely needs it.
        self.assertIn("posterchanai-relay.service", self._units("app/services/nostr_relay/x.py"))
        self.assertIn("posterchanai.service", self._units("templates/client.html"))


class RestartsOnlyWhatChanged(unittest.TestCase):
    def test_the_restart_set_comes_from_deploy_targets(self):
        self.assertIn("scripts/deploy_targets.py", _src())

    def test_an_unknown_failure_falls_back_to_restarting_the_app(self):
        """Under-restarting ships code running nowhere and presents as "the fix didn't work"."""
        self.assertIn("|| echo posterchanai.service", _src())

    def test_the_gpu_wait_is_skipped_when_nothing_restarts(self):
        """_wait_gpu_free blocks until any in-flight generation finishes -- minutes on a long video
        or music job. Paying that on a UI-only deploy that restarts NOTHING is pure dead time, and
        it is part of why running sync.sh for a client change felt too expensive to bother with."""
        src = _src()
        for guard in (r'if \[ -n "\$_TARGETS" \]; then _wait_gpu_free',
                      r'if \[ -n \\"\\\$_NAS_TARGETS\\" \]; then _wait_gpu_free'):
            self.assertRegex(src, guard)

    def test_the_gpu_wait_is_never_unguarded(self):
        """Every CALL must sit behind a targets check; only the two definitions are bare."""
        for line in _src().splitlines():
            s = line.strip()
            if "_wait_gpu_free" not in s or s.startswith("#"):
                continue
            if s.startswith("_wait_gpu_free() {") or s.startswith("_wait_gpu_free() {".replace(" ", "")):
                continue
            self.assertIn("_TARGETS", s, f"unguarded GPU wait: {s}")


class LocalRestartSetIsMeasuredFromWhatIsRunning(unittest.TestCase):
    """The local restart range must start at the commit THIS NODE'S SERVICES ARE ON, not at
    "HEAD before sync.sh's own commit". Those are the same only when sync.sh created the commit.

    That gap shipped. A commit made by hand before running sync.sh makes `git commit -a` a no-op, so
    HEAD-before == HEAD-after, the range is EMPTY, deploy_targets returns nothing, and server1 restarts
    NOTHING -- while nas restarts correctly, because nas computes its range from its own pre-pull HEAD.
    The deploy then reports "all nodes in sync", which is true of the checkouts and false of the
    processes: server1 ran two-hour-old code with nothing in any log to say so. Running sync.sh twice,
    or pulling a commit made on another machine, reproduces it exactly.
    """

    def test_the_base_comes_from_a_persisted_deploy_stamp(self):
        src = _src()
        self.assertIn("_STAMP=", src, "no stamp of what the local services are running")
        self.assertRegex(src, r'_PREV_HEAD="\$_s"',
                         "the stamp is read but never used as the restart range's base")

    def test_the_stamp_is_validated_before_it_is_trusted(self):
        """A rebased/gc'd sha would make the range bogus; falling back restarts more, which is safe."""
        self.assertIn('git rev-parse --verify --quiet "${_s}^{commit}"', _src())

    def test_the_stamp_lives_outside_the_working_tree(self):
        """In .git/, so it is never committed, never dirties `git status`, and survives checkouts."""
        m = re.search(r'_STAMP="([^"]+)"', _src())
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith(".git/"), m.group(1))

    def test_the_stamp_advances_even_when_nothing_restarted(self):
        """A range with no server-side change leaves the services on older code correctly; the next
        deploy must measure from here or it re-evaluates ground already covered forever."""
        src = _src()
        m = re.search(r'_restart_units "\$_TARGETS"(.*?)\n# server1 is cut over', src, re.S)
        self.assertIsNotNone(m, "could not find the post-restart block")
        self.assertIn("$_STAMP", m.group(1), "the stamp is never written after a local restart pass")

    def test_a_failed_restart_does_not_advance_the_stamp(self):
        """Otherwise the retry sees the work as already done and the unit stays on old code."""
        src = _src()
        self.assertIn("_RESTART_FAILED=1", src)
        self.assertRegex(src, r'(?s)if \[ "\$_RESTART_FAILED" = "0" \]; then\s*\n\s*git rev-parse HEAD > "\$_STAMP"')

    def test_a_failed_restart_is_reported(self):
        """It used to pass silently: the unit keeps running old code, the deploy still says success."""
        src = _src()
        self.assertRegex(src, r'if ! sudo systemctl restart "\$u"; then')
        self.assertIn("FAILED to restart", src)


class UnitExistenceCheckDoesNotNeedToReadTheUnitFile(unittest.TestCase):
    """`systemctl cat` READS THE UNIT FILE as the invoking user. posterchanai.service was mode 600
    while every sibling unit was 644, so `cat` returned "Permission denied" -- and the restart loop,
    which treated a failed `cat` as "this unit does not exist", silently skipped THE MAIN APP on
    every deploy. The worker restarted, the app did not, and the deploy reported success.

    LoadState asks systemd rather than the filesystem, so it is immune to the unit file's mode.
    """

    def test_the_guard_uses_loadstate(self):
        src = _src()
        self.assertIn('systemctl show -p LoadState --value "$u"', src)

    def test_the_guard_does_not_read_the_unit_file(self):
        m = re.search(r"_restart_units\(\) \{.*?\n\}", _src(), re.S)
        self.assertIsNotNone(m)
        # Comments are allowed to NAME the trap (they explain it); only executable lines must be free
        # of it, so strip them before asserting.
        code = "\n".join(ln for ln in m.group(0).splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("systemctl cat", code,
                         "reading the unit file makes the check fail on a root-only unit")

    def test_a_skipped_unit_is_reported(self):
        """Silence is what let the mode-600 bug hide behind a green deploy."""
        m = re.search(r"_restart_units\(\) \{.*?\n\}", _src(), re.S)
        self.assertIn("SKIPPED", m.group(0), "a unit that is not restarted must say so")


class VerifiesTheDeployLanded(unittest.TestCase):
    def test_every_target_is_verified(self):
        src = _src()
        for target in ("nas.lan", "router.lan", "origin:master", "github:main"):
            self.assertIn(target, src, target)
        self.assertIn("_verify_node", src)

    def test_drift_fails_the_deploy(self):
        """Silent success on an incomplete deploy is the exact failure being fixed, so it must exit
        non-zero -- a WARN scrolls past and gets believed."""
        src = _src()
        self.assertIn("_DRIFT=1", src)
        self.assertRegex(src, r'(?s)if \[ "\$_DRIFT" != "0" \]; then.*?exit 1')

    def test_an_unreachable_node_counts_as_drift(self):
        """It is still serving whatever it last had, however friendly the ssh error looked."""
        src = _src()
        self.assertIn("UNREACHABLE", src)
        m = re.search(r'elif \[ -z "\$got" \]; then(.*?)\n    else', src, re.S)
        self.assertIsNotNone(m, "no explicit unreachable branch")
        self.assertIn("_DRIFT=1", m.group(1))

    def test_verification_runs_after_the_nodes_are_pulled(self):
        """Checking before the pulls would pass on a deploy that never landed."""
        src = _src()
        self.assertLess(src.index("ssh nas.lan"), src.index("_verify_node()"))


if __name__ == "__main__":
    unittest.main()


def test_store_metadata_and_templates_restart_nothing():
    """Editing an app-store DESCRIPTION restarted all seven units on both nodes.

    zapstore.yaml is fetched from the repo by the Zapstore relay and read by the android.yml publish
    step; no running service loads it. Unmapped, it fell through to the fail-safe "could affect
    anything" branch — so a wording change to the store listing dropped every connected Nostr client
    and bounced the relay mid-stream. The same shape as desktop/, mobile/, extension/ and git_hooks/
    before it, which is why this now has a test rather than another comment.

    `.example` files are templates by definition: a service reading one would be reading a sample.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dt", os.path.join(REPO, "scripts", "deploy_targets.py"))
    dt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dt)

    for f in ("zapstore.yaml", "posterchanai-cuda.service.example", "sync.sh", "docker-compose.yml"):
        assert dt.units_for([f]) == [], f"{f} restarts services it cannot affect"

    # The duplicate-definition trap: _INERT_FILES was declared twice while this was being fixed, and
    # the second assignment silently discarded the first — so the new entry did nothing and, in the
    # other order, would have un-marked sync.sh and the Docker files. One definition, all entries.
    with open(os.path.join(REPO, "scripts", "deploy_targets.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("\n_INERT_FILES = ") == 1, "_INERT_FILES is defined more than once"
    for f in ("sync.sh", "install.sh", "docker-compose.yml", "zapstore.yaml"):
        assert f in dt._INERT_FILES, f"{f} fell out of _INERT_FILES"

    # And the fail-safe default must survive: anything that IS runtime still restarts.
    assert dt.units_for(["app/services/nostr_relay/outbox.py"]) == ["posterchanai-relay.service"]
    assert len(dt.units_for(["run-intel.sh"])) >= 5, "a launcher must still restart everything"


def test_a_service_the_routers_import_always_restarts_the_app():
    """A module the ROUTERS import must include the app in its targets — including when the import
    sits inside a function.

    Every mapping in _OWNED was measured by importing each role's modules and reading sys.modules,
    and that measurement cannot see a LAZY import: `app/routers/admin.py` does
    `from app.services.stats_bot_service import build_stats` inside the endpoint, so the module is
    absent from the app's sys.modules at startup and the file was mapped to the worker alone. It
    lands there the first time an admin presses the button and stays for the life of the process —
    so a chart fix reached the worker's nightly cron while the button an admin actually looks at went
    on rendering the old code, with the deploy reporting every node in sync. "Why does stats look the
    same."

    Under-restarting is the dangerous direction: over-restarting costs an outage somebody notices,
    while this ships code that runs nowhere and looks deployed.
    """
    import importlib.util
    import re
    spec = importlib.util.spec_from_file_location(
        "dt", os.path.join(REPO, "scripts", "deploy_targets.py"))
    dt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dt)

    routers = os.path.join(REPO, "app", "routers")
    wanted = set()
    for root, _dirs, files in os.walk(routers):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(root, fn), encoding="utf-8").read()
            for m in re.finditer(r"from\s+app\.services\.([\w.]+)\s+import|import\s+app\.services\.([\w.]+)", src):
                wanted.add(m.group(1) or m.group(2))

    assert wanted, "found no app.services imports in the routers — has the tree moved?"
    missing = []
    for mod in sorted(wanted):
        rel = "app/services/" + mod.replace(".", "/") + ".py"
        if not os.path.exists(os.path.join(REPO, rel)):
            rel = "app/services/" + mod.replace(".", "/") + "/"     # a package
            if not os.path.isdir(os.path.join(REPO, rel)):
                continue
        units = dt.units_for([rel])
        if "posterchanai.service" not in units:
            missing.append((rel, units))
    assert not missing, (
        "these are imported by app/routers/ but do not restart the app: %s. A router import means the "
        "APP runs this code; leaving it off the targets deploys a fix to a process that never serves "
        "it." % missing)


def test_the_overlay_is_published_when_its_inputs_change():
    """The overlay is how an INSTALLED machine gets a newer desktop — `emerge -u` against
    gentoo.poster.place. An overlay that only updates when somebody remembers to run a script is
    permanently a few versions behind the code it packages, and nothing about that is visible from
    either end: the deploy succeeds, the machines are simply old."""
    src = open(SYNC, encoding="utf-8").read()
    assert "publish_overlay.sh" in src, "sync.sh never publishes the overlay"
    i = src.index("publish_overlay.sh")
    guard = src[max(0, i - 400):i]
    assert "os/(overlay|bin|plymouth)" in guard, (
        "the overlay is published on every deploy — a forced push each time makes every installed "
        "machine re-sync a repo whose contents are identical")
    assert "WARN" in src[i:i + 200], (
        "a failed publish is silent; the deploy would report success with the overlay stale")


def test_the_overlay_ebuild_tracks_the_desktop_build():
    """SRC_URI points at a ROLLING url — `desktop-latest` — so it always fetches the newest
    AppImage. If the ebuild's version never changes, portage sees the version it already has
    installed and `emerge -u` reports nothing to do, for ever: a package manager pointed at a moving
    target, reporting success. The version is read from the same update feed the desktop app uses."""
    pub = open(os.path.join(REPO, "scripts", "publish_overlay.sh"), encoding="utf-8").read()
    assert "latest.yml" in pub, "the ebuild version is never bumped — updates can never be seen"
    assert "posterchan-desktop-${LIVE}.ebuild" in pub
    assert "could not read the desktop version" in pub, (
        "a failed version read is silent; it would publish a stale version number as though current")
