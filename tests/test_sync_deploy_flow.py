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
