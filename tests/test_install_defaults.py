"""The add-ons that are meant to ship by DEFAULT actually run in the default install.

Run: venv-unified/bin/python -m pytest tests/test_install_defaults.py

"Built-in" is a promise that a fresh node has the feature without a second command, and every one of
these is invisible when it is absent rather than broken: Files simply has no 📝 button, the node
quietly searches through a public SearXNG instead of its own, streaming is a toggle that does
nothing. So a reordering that drops one from `main()` produces no error anywhere — it produces a
node that is missing a feature nobody notices for a month.

The second half matters as much: each must be NON-FATAL. These download from third-party mirrors,
and an install that dies on a slow CDN leaves a half-configured box, which is far worse than one
that finishes without an optional editor. The `|| print_warning …` is the promise that the rest of
the install completes.

Not asserted here: `install_nostr_only`, which deliberately skips every heavy add-on — it is the
"light, no GPU" path and takes none of MediaMTX, SearXNG, TURN or CODE.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")

# (setup function, the ./install.sh flag that installs it on its own)
DEFAULT_ADDONS = [
    ("setup_stream_server", "--stream"),
    ("setup_searxng", "--searxng"),
    ("setup_turn_server", "--turn"),
    ("setup_office_server", "--office"),
]


def _main_body(src):
    """`main()`'s body, by matching braces — never a fixed slice, which reports a function that
    merely grew as one that lost a call."""
    i = src.index("\nmain() {")
    depth, k = 0, src.index("{", i)
    start = k
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[start:k + 1]
        k += 1
    raise AssertionError("main() never closes — re-point this test")


class TheDefaultInstallShipsWhatItPromises(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(INSTALL, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.main = _main_body(cls.src)

    def test_each_default_addon_runs_in_the_default_install(self):
        for fn, flag in DEFAULT_ADDONS:
            with self.subTest(addon=fn):
                self.assertIn(fn, self.main,
                              f"{fn} is not called by main(), so a normal ./install.sh leaves the "
                              f"feature off and nothing says so (only ./install.sh {flag} would)")

    def test_each_default_addon_is_non_fatal(self):
        """`set -e` plus a third-party download is a half-installed box."""
        for fn, _flag in DEFAULT_ADDONS:
            with self.subTest(addon=fn):
                call = re.search(rf"^\s*{fn}\b(.*)$", self.main, re.M)
                self.assertTrue(call, f"{fn} not called in main()")
                self.assertIn("||", call.group(1),
                              f"{fn} can abort the whole install — it must degrade to a warning")

    def test_each_addon_still_has_its_own_flag(self):
        """The flag is how somebody retries the one that failed."""
        for fn, flag in DEFAULT_ADDONS:
            with self.subTest(addon=fn):
                self.assertRegex(self.src, rf'"\$1"\s*=\s*"{re.escape(flag)}"',
                                 f"{flag} is gone, so a failed {fn} cannot be retried on its own")

    def test_docker_starts_the_office_editor_too(self):
        """`--profile office` made it opt-in TWICE — you had to know the profile existed AND set
        POSTERCHANAI_OFFICE — and the 📝 button is hidden when the editor is absent, so a stack
        without it simply had no office and nothing said why."""
        import yaml
        with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
            compose = yaml.safe_load(fh)
        office = compose["services"]["office"]
        self.assertNotIn("profiles", office,
                         "the office service is profile-gated, so a normal `docker compose up` "
                         "brings up everything except the editor")
        env = "\n".join(compose["x-common"]["environment"])
        self.assertIn("POSTERCHANAI_OFFICE=${POSTERCHANAI_OFFICE:-1}", env,
                      "the container runs but the client is told the editor is off, so the 📝 "
                      "button is hidden and the editor is unreachable")

    def test_the_listen_address_is_a_choice_not_a_hardcode(self):
        """loopback is right when nginx and CODE share a box; on a split deployment the front end is
        another machine and loopback means every document 502s. Neither can be the silent default
        for the other, so it is named."""
        with open(os.path.join(ROOT, "scripts", "install", "office.sh"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("POSTERCHANAI_OFFICE_LISTEN", src)
        self.assertIn("--o:net.listen=$listen", src)
        # THE ExecStart LINE, not the file: the paragraph above it explains why `--port=9983` must
        # not be passed, and a bare substring search reads its own explanation as the offence.
        execs = [l for l in src.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual(len(execs), 1, f"expected one ExecStart, got {execs}")
        self.assertNotIn("--port=", execs[0],
                         "the AppImage's AppRun already passes --port, and coolwsd treats a repeat "
                         "as fatal while still exiting 0 — systemd reports success and the unit is "
                         "simply never up")

    def test_the_office_installer_is_sourced(self):
        """A call to a function from a file nobody sources is a command-not-found at install time."""
        self.assertIn('source "$INSTALL_DIR/office.sh"', self.src)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "scripts", "install", "office.sh")))


if __name__ == "__main__":
    unittest.main()
