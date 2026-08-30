"""A browser check must not hardcode a port or share a profile directory.

Run: venv-unified/bin/python -m pytest tests/test_checks_can_run_concurrently.py

`checkall.py` runs the browser checks in parallel (`--jobs`, default cpus/2) and hands each one its
own Chrome debugging port (`PC_CHECK_PORT`, from `PORT_BASE + index`) and its own profile directory
(`PC_CHECK_PROFILE`). CLAUDE.md states both rules and gives the reason: four scripts once shared
9473, so two running at once attached to the SAME browser and drove each other's pages.

Nothing enforced them. Two checks were still binding literal ports — 9497 and 9503 — for an HTTP
server AND a CDP endpoint, so a run of both at once, or a run overlapping anything else on those
ports, fails in a way that reads as a broken feature rather than a port clash. Both are in the group
that runs with defaults, which is where a new check lands.

The profile rule is checked by INTENT rather than by the variable name: a per-run
`tempfile.TemporaryDirectory()` satisfies it just as well as reading `PC_CHECK_PROFILE`, and several
checks legitimately do that. What must not appear is a fixed `/tmp/<name>` path with no unique
component — two concurrent Chromes on one profile directory corrupt it and one of them dies on a
lock, intermittently. (`check_client_icon_themes.py` reads the env var and falls back to a literal;
that is correct and this must not flag it.)
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")

# A check that genuinely needs no CDP port: it renders with --dump-dom, one shot, no debugging
# endpoint at all. Listed rather than special-cased so a check that GROWS a port is caught.
NO_PORT_NEEDED = {
    "check_extension_popup.py":
        "renders with --dump-dom and never opens a CDP endpoint, so there is no port to collide",
}


def _browser_checks():
    out = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not (name.startswith("check_") and name.endswith(".py")):
            continue
        with open(os.path.join(SCRIPTS, name), encoding="utf-8", errors="ignore") as f:
            src = f.read()
        if "user-data-dir" in src or "remote-debugging-port" in src:
            out.append((name, src))
    return out


class BrowserChecksAreConcurrencySafe(unittest.TestCase):

    def test_the_scan_still_finds_the_browser_checks(self):
        """Guard on the guard: a rename would make every assertion below a loop over nothing."""
        found = _browser_checks()
        self.assertGreaterEqual(len(found), 30,
                                "only %d browser checks found — the scan has stopped seeing "
                                "scripts/" % len(found))

    def test_every_check_takes_its_debugging_port_from_the_runner(self):
        bad = []
        for name, src in _browser_checks():
            if "PC_CHECK_PORT" in src or name in NO_PORT_NEEDED:
                continue
            lits = sorted(set(re.findall(r"remote-debugging-port=\{?(\d{4,5})", src)
                              + re.findall(r"^PORT\s*=\s*(\d{4,5})", src, re.M)))
            bad.append("%s (%s)" % (name, ", ".join(lits) if lits else "no literal found"))
        self.assertEqual([], bad,
                         "these browser checks hardcode a port instead of reading PC_CHECK_PORT. "
                         "checkall runs them CONCURRENTLY, so two of them — or one of them and "
                         "anything else on the box — bind the same port and attach to the same "
                         "Chrome: %s" % "; ".join(bad))

    def test_no_check_uses_a_fixed_profile_directory(self):
        """Two Chromes on one profile dir corrupt it and one dies on a lock, intermittently — the
        worst shape, because it looks like a flaky feature rather than a flaky harness."""
        # Uniqueness is judged on the ASSIGNMENT that produces the profile path, not on the file.
        # Asking "does this file mention tempfile anywhere" passes any check that imports tempfile
        # for something else — which made the first version of this test unable to fail: pinning
        # check_client_icon_themes.py to a fixed literal left it green.
        UNIQUE = ("PC_CHECK_PROFILE", "TemporaryDirectory", "mkdtemp", "getpid", "uuid",
                  "td.name", "mktemp")
        bad = []
        for name, src in _browser_checks():
            # The expression handed to --user-data-dir: either a literal, or a variable name.
            for m in re.finditer(r"user-data-dir=\{?([A-Za-z_][A-Za-z0-9_.]*|['\"]?/tmp/[^'\"}\s]+)",
                                 src):
                expr = m.group(1).strip("'\"")
                if expr.startswith("/tmp/"):
                    bad.append("%s (literal %s)" % (name, expr))
                    continue
                var = expr.split(".")[0]
                # Every assignment to that variable must derive from something per-run.
                rhs = re.findall(r"^\s*%s\s*=\s*(.+)$" % re.escape(var), src, re.M)
                if rhs and not any(any(k in r for k in UNIQUE) for r in rhs):
                    lits = [r for r in rhs if "/tmp/" in r]
                    bad.append("%s (%s = %s)" % (name, var, (lits or rhs)[0].strip()[:60]))
        self.assertEqual([], bad,
                         "these browser checks point Chrome at a fixed profile directory with no "
                         "per-run component: %s" % "; ".join(bad))

    def test_the_exemption_list_is_not_stale(self):
        names = {n for n, _ in _browser_checks()}
        gone = sorted(n for n in NO_PORT_NEEDED if n not in names)
        self.assertEqual([], gone,
                         "NO_PORT_NEEDED names checks that no longer launch a browser: %s"
                         % ", ".join(gone))

    def test_every_exemption_gives_a_reason(self):
        for n, why in NO_PORT_NEEDED.items():
            self.assertGreater(len(why), 30, "%s is exempted without a real reason" % n)


class TheRunnerStillHandsThemOut(unittest.TestCase):
    """The other end of the contract. If checkall stops assigning them, every check falls back to
    its literal default and they all collide — with each check individually looking correct."""

    def test_checkall_assigns_a_unique_port_and_profile_per_job(self):
        src = open(os.path.join(ROOT, "scripts", "checkall.py"), encoding="utf-8").read()
        self.assertTrue("PC_CHECK_PORT" in src, "checkall no longer assigns PC_CHECK_PORT")
        self.assertTrue("PC_CHECK_PROFILE" in src, "checkall no longer assigns PC_CHECK_PROFILE")
        self.assertTrue("PORT_BASE" in src, "checkall no longer derives per-job ports")
        # The port must vary with the job index, not be one constant handed to everybody.
        self.assertTrue(re.search(r"PORT_BASE\s*\+\s*\w+", src),
                        "checkall hands out PORT_BASE itself rather than PORT_BASE + index, so "
                        "every concurrent check gets the same port")


if __name__ == "__main__":
    unittest.main()
