"""update-posterchan keeps PosterFox current -- from its signed binary only, never by compiling Firefox.

"make sure all the apps get updated". PosterFox is published as a binary package (posterfox/tools/release.sh); the
updater must pull a newer one when there is one, must never fall back to building Firefox from source (hours, on the
user's machine), and must not let a missing binary stop the PosterChan update. The updater's own lines, RUN under bash
with `qlist` and `emerge` stubbed.
"""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPD = ROOT / "os/bin/update-posterchan"


def _snippet():
    s = UPD.read_text()
    a = s.index("if [ -n \"$(qlist -I www-client/posterfox")
    return s[a:s.index("\nfi\n", a) + 4]


def _run(tmp, installed, emerge_rc=0):
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "qlist").write_text("#!/bin/sh\n" + ("echo www-client/posterfox\n" if installed else "") + "exit 0\n")
    (bindir / "emerge").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/emerge.log\nexit {emerge_rc}\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    script = "CYA=; OFF=; YEL=\n" + _snippet() + "echo AFTER\n"
    out = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=20,
                         env={"PATH": f"{bindir}:/usr/bin:/bin"})
    log = (tmp / "emerge.log").read_text() if (tmp / "emerge.log").exists() else ""
    return out, log


def test_an_installed_posterfox_is_updated_from_its_binary_only(tmp_path):
    out, log = _run(tmp_path, installed=True)
    args = log.split()
    assert "www-client/posterfox" in args and "-u" in args, log
    assert "--usepkgonly" in args, ("the update could compile Firefox from source", log)
    assert "AFTER" in out.stdout


def test_no_binary_yet_keeps_the_browser_and_carries_on(tmp_path):
    out, log = _run(tmp_path, installed=True, emerge_rc=1)
    assert "keeping the one installed" in out.stdout
    assert "AFTER" in out.stdout, "a missing PosterFox binary stopped the PosterChan update"


def test_a_machine_without_posterfox_is_not_given_one(tmp_path):
    out, log = _run(tmp_path, installed=False)
    assert log == "", log


def test_it_runs_before_any_already_up_to_date_exit():
    s = UPD.read_text()
    assert s.index("qlist -I www-client/posterfox") < s.index("Already up to date.${OFF}"), \
        "an update that found nothing new for PosterChan would never check the browser"
