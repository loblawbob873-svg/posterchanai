"""update-posterchan gives a machine installed before Tor was part of PosterChanOS its Tor -- PosterFox's Tor tabs and
.onion sites go through SOCKS 9050.

"tried to go to onion site and it said proxy server refusing connections" (the laptop, 2026-10-29): gentoo.sh installs
net-vpn/tor, writes its torrc and enables tor.service on a FRESH install only, and the updater updates PosterChan's own
packages -- so the laptop had no tor at all. The updater's own _pc_tor, RUN under bash with `qlist`, `emerge` and
`systemctl` stubbed and the torrc in a temp directory.
"""
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPD = ROOT / "os/bin/update-posterchan"
GENTOO = ROOT / "os/gentoo.sh"


def _function():
    s = UPD.read_text()
    a = s.index('_pc_torrc="${PC_TORRC')
    return s[a:s.index("\n}\n", s.index("_pc_tor() {")) + 3]


def _run(tmp, installed, torrc=None, emerge_rc=0):
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "qlist").write_text("#!/bin/sh\n" + ("echo net-vpn/tor\n" if installed else "") + "exit 0\n")
    (bindir / "emerge").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/emerge.log\nexit {emerge_rc}\n')
    (bindir / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/systemctl.log\nexit 0\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    rc = tmp / "etc/tor/torrc"
    if torrc is not None:
        rc.parent.mkdir(parents=True)
        rc.write_text(torrc)
    out = subprocess.run([shutil.which("bash"), "-c", "CYA=; OFF=; YEL=\n" + _function() + "_pc_tor\necho AFTER\n"],
                         capture_output=True, text=True, timeout=20,
                         env={"PATH": f"{bindir}:/usr/bin:/bin", "PC_TORRC": str(rc)})
    read = lambda n: (tmp / n).read_text() if (tmp / n).exists() else ""
    return out, read("emerge.log"), read("systemctl.log"), (rc.read_text() if rc.exists() else None)


def test_a_machine_without_tor_gets_it_on_9050_and_running(tmp_path):
    out, emerge, systemctl, torrc = _run(tmp_path, installed=False)
    assert "net-vpn/tor" in emerge.split() and "--noreplace" in emerge.split(), emerge
    assert re.search(r"^SocksPort 9050$", torrc, re.M), torrc
    assert "enable --now tor" in systemctl, systemctl
    assert "AFTER" in out.stdout


def test_the_torrc_is_the_installers(tmp_path):
    _, _, _, torrc = _run(tmp_path, installed=False)
    g = GENTOO.read_text()
    body = g[g.index("torService() {"):]
    body = body[body.index("<<-'TORRC'") + len("<<-'TORRC'"):body.index("\tTORRC")]
    want = [l.strip() for l in body.strip("\n").splitlines()]
    assert [l.strip() for l in torrc.strip("\n").splitlines()] == want, "the updater writes another torrc than gentoo.sh"


def test_a_hand_made_torrc_is_left_alone(tmp_path):
    mine = "SocksPort 127.0.0.1:9150\nExitNodes {de}\n"
    _, emerge, systemctl, torrc = _run(tmp_path, installed=True, torrc=mine)
    assert emerge == "" and torrc == mine, (emerge, torrc)
    assert "enable --now tor" in systemctl


def test_a_failed_install_never_stops_the_update(tmp_path):
    out, _, systemctl, torrc = _run(tmp_path, installed=False, emerge_rc=1)
    assert "AFTER" in out.stdout and "Tor did not install" in out.stdout, out.stdout
    assert torrc is None and systemctl == "", "nothing is configured or started for a tor that is not there"


def test_it_runs_on_every_update():
    s = UPD.read_text()
    assert re.search(r"\n\t_pc_posterfox\n\t_pc_tor\n", s), "the update must call _pc_tor"
    assert UPD.read_bytes() == (ROOT / "os/overlay/app-misc/posterchanos-shell/files/update-posterchan").read_bytes()


def test_tor_enters_and_exits_in_the_us_by_default_and_never_fights_the_desktops_tor():
    """'Tor should use US entrance and exit nodes by default. Make sure it won't conflict with new installs where user
    chooses connect over Tor' (2026-10-29). The system tor (this torrc: SOCKS 9050, /var/lib/tor) is not the desktop's
    built-in tor that the first-run 'connect over Tor' step switches on (desktop/tor.js: an ephemeral SocksPort and
    its own DataDirectory, US by default on PosterChanOS) -- two processes that share no port and no files."""
    g = GENTOO.read_text()
    body = g[g.index("torService() {"):g.index("\n}\n", g.index("torService() {"))]
    for line in ("SocksPort 9050", "EntryNodes {us}", "ExitNodes {us}", "StrictNodes 1", "GeoIPFile /usr/share/tor/geoip"):
        assert line in body, line
    tor_js = (ROOT / "desktop/tor.js").read_text()
    assert "SocksPort 127.0.0.1:${state.socksPort}" in tor_js and "await freePort()" in tor_js, "the desktop's tor must not take 9050"
    assert "path.join(app.getPath('userData'), 'tor')" in tor_js, "the desktop's tor keeps its own data directory"
    main = (ROOT / "desktop/main.js").read_text()
    assert "SHELL_MODE ? { enabled: false, country: 'us' }" in main, "PosterChanOS's built-in tor defaults to the US"


def _reexec_script(tmp, new_differs, already=False):
    """The real fingerprint + re-run code, as an 'old' updater; a 'new' one sits on PATH as update-posterchan."""
    s = UPD.read_text()
    head = s[s.index('_pc_self="$(command -v'):s.index("\n", s.index('_pc_self_sum=')) + 1]
    blk = s[s.index('\tif [ -z "${PC_UPDATE_REEXEC:-}" ]'):s.index('\tafter="$(qlist')]
    old = tmp / "old-updater"
    old.write_text("#!/bin/bash\nCYA=; OFF=\nhave_pkgs='desktop-1 shell-1'\n" + head + blk + "echo OLD-FINISHED\n")
    old.chmod(0o755)
    bindir = tmp / "bin"
    bindir.mkdir()
    new = bindir / "update-posterchan"
    new.write_text(old.read_text() if not new_differs else
                   '#!/bin/bash\necho "NEW RAN reexec=$PC_UPDATE_REEXEC before=$PC_UPDATE_BEFORE"\n')
    new.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", **({"PC_UPDATE_REEXEC": "1"} if already else {})}
    return subprocess.run([str(old)], capture_output=True, text=True, timeout=20, env=env).stdout


def test_an_update_that_brings_a_newer_updater_runs_its_new_steps_now(tmp_path):
    """The laptop needed TWO updates to get Tor (2026-10-30): the shell package installs update-posterchan itself, and
    the run that installed the Tor step was the old script. A newer copy on disk is run once, reporting against what the
    first run started from; an identical copy is not; a re-run never re-runs again."""
    out = _reexec_script(tmp_path, new_differs=True)
    assert "NEW RAN reexec=1 before=desktop-1 shell-1" in out and "OLD-FINISHED" not in out, out


def test_no_newer_updater_means_no_second_run(tmp_path):
    out = _reexec_script(tmp_path, new_differs=False)
    assert "OLD-FINISHED" in out and "running it once more" not in out, out


def test_a_re_run_never_runs_again(tmp_path):
    out = _reexec_script(tmp_path, new_differs=True, already=True)
    assert "OLD-FINISHED" in out and "NEW RAN" not in out, out
