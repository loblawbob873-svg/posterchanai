"""THE INSTALLER MUST COPY THE CURRENT PLYMOUTH THEME, NOT THE INSTALL-DAY-STALE ONE.

Measured on a real TV, 2026-09-18: the boot splash was the OLD UI even though the ISO shipped the new
theme. The image carried TWO copies —

    /usr/share/plymouth/themes/posterchanos/posterchanos.script          181 lines (new, from the pkg)
    /usr/local/share/posterchanos/plymouth/posterchanos/posterchanos.script  80 lines (old)

`$PCOS_TREE` is /usr/local/share/posterchanos: install-day state on the build host, written once when
that host was installed and never updated. `plymouthTheme()` preferred it FIRST and did
`cp -f "$SRC"/* "$DEST"/`, so it copied the 80-line theme OVER the 181-line one the posterchanos-shell
package had already installed — and every fresh install booted the old splash.

The session helpers already learned this rule ("SYNCED OVERLAY FIRST. $PCOS_TREE is install-day
state"). plymouthTheme now follows it: the synced overlay and the package's own /usr/share theme come
first; $PCOS_TREE is the last resort.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")


def _fn(name):
    start = GENTOO.index(f"{name}() {{")
    return GENTOO[start:GENTOO.index("\n}\n", start) + 3]


class PlymouthSource(unittest.TestCase):
    def run_theme(self, present, *, script_lines):
        """RUN plymouthTheme()'s source-selection against fake dirs.

        `present` maps a candidate dir (relative role) to whether it exists; `script_lines` maps the
        same roles to the line count of their posterchanos.script, so the test can prove WHICH one
        was copied to DEST by reading DEST back.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            roles = {
                "overlay": tmp / "repo/app-misc/posterchanos-shell/files/plymouth",
                "usrshare": tmp / "usr/share/plymouth/themes/posterchanos",
                "pcostree": tmp / "pcostree/plymouth/posterchanos",
            }
            for role, d in roles.items():
                if present.get(role):
                    d.mkdir(parents=True)
                    (d / "posterchanos.plymouth").write_text("[Plymouth Theme]\n")
                    (d / "posterchanos.script").write_text("\n" * script_lines[role])
                    (d / "logo.png").write_text("x")
            dest = tmp / "target/usr/share/plymouth/themes/posterchanos"
            # Harness: stub the externals plymouthTheme touches, point its candidates at our dirs.
            fn = _fn("plymouthTheme")
            fn = fn.replace('portageq get_repo_path / posterchan 2>/dev/null',
                            f'echo {tmp}/repo')
            fn = fn.replace('/var/db/repos/posterchan/app-misc/posterchanos-shell/files/plymouth',
                            str(tmp / "nonexistent-vardb"))
            fn = fn.replace('/usr/share/plymouth/themes/posterchanos"',
                            f'{roles["usrshare"]}"', 1)  # first occurrence = the SRC candidate
            fn = fn.replace('$PCOS_TREE/plymouth/posterchanos', f'{roles["pcostree"]}')
            script = (
                "set -u\n"
                f'PCOS_TREE="{tmp}/pcostree"\n'
                f'TARGET="{tmp}/target"\n'
                'portageq(){ echo "' + str(tmp) + '/repo"; }\n'
                '_pc_select_plymouth_theme(){ return 0; }\n'
                + fn
                + "\nplymouthTheme >/dev/null 2>&1\n"
                + f'wc -l < "{dest}/posterchanos.script" 2>/dev/null || echo MISSING\n')
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            out = r.stdout.strip().splitlines()
            return out[-1] if out else "NORUN"

    def test_overlay_new_beats_pcostree_old(self):
        """THE REPORTED BUG: overlay/pkg has 181 lines, $PCOS_TREE has 80. The new one must win."""
        got = self.run_theme(
            {"overlay": True, "usrshare": True, "pcostree": True},
            script_lines={"overlay": 181, "usrshare": 181, "pcostree": 80})
        self.assertEqual("181", got,
                         "the installer copied the stale install-day theme over the fresh one")

    def test_usrshare_used_when_no_overlay(self):
        """No synced overlay (a machine off-LAN): the package's own /usr/share theme, not PCOS_TREE."""
        got = self.run_theme(
            {"overlay": False, "usrshare": True, "pcostree": True},
            script_lines={"usrshare": 181, "pcostree": 80})
        self.assertEqual("181", got)

    def test_pcostree_only_is_the_last_resort(self):
        """With nothing fresher present it still ships SOMETHING rather than the stock splash."""
        got = self.run_theme(
            {"overlay": False, "usrshare": False, "pcostree": True},
            script_lines={"pcostree": 80})
        self.assertEqual("80", got)


class TheOrderingIsStatedInSource(unittest.TestCase):
    def test_pcostree_is_not_the_first_candidate(self):
        fn = _fn("plymouthTheme")
        block = fn[fn.index("_pt_repo="):fn.index("if [ -z \"$SRC\" ]")]
        first = re.search(r'for _pt_cand in\s*\\\s*\n\s*"([^"]*)"', block)
        self.assertIsNotNone(first, block)
        self.assertNotIn("PCOS_TREE", first.group(1),
                         "$PCOS_TREE is still the first source tried — the stale-theme bug is back")


if __name__ == "__main__":
    unittest.main()
