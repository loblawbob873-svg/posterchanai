"""pc-anticheat: the root helper behind System Settings -> Secure Boot and the installer's checkbox.

Asked 2026-10-27 ("i hope there is some secure boot thing in the gui installer" -> "yes", build it in). Runs the
SHIPPED helper with stand-ins for systemctl, systemd-run, mokutil and gentoo.sh: the request starts the service, the
service's run makes a one-time password and hands it to `gentoo.sh anticheat` as PC_MOK_PASSWORD, the status says
what to do next (and only shows the password while it is needed), a failure carries its log, and undo runs
anticheat-off in the background.
"""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-anticheat"


def _exe(p, body):
    p.write_text("#!/bin/bash\n" + body + "\n")
    os.chmod(p, 0o755)


class Box:
    def __init__(self, tmp, gentoo_rc=0, sb="SecureBoot disabled"):
        self.tmp, self.bin, self.state, self.root = tmp, tmp / "bin", tmp / "state", tmp / "root"
        for d in (self.bin, self.state, self.root / "etc/posterchan"):
            d.mkdir(parents=True, exist_ok=True)
        self.calls = tmp / "calls"
        L = f'echo "$(basename "$0") $*" >> "{self.calls}"'
        _exe(self.bin / "systemctl", L)
        _exe(self.bin / "systemd-run", L)
        _exe(self.bin / "mokutil", f'{L}; echo "{sb}"')
        _exe(self.bin / "gentoo.sh", f'{L}; echo "PW=$PC_MOK_PASSWORD"; echo "emerging shim..."; exit {gentoo_rc}')

    def run(self, *args):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "PC_AC_STATE_DIR": str(self.state), "PC_AC_ROOT": str(self.root),
               "PC_GENTOO_SH": str(self.bin / "gentoo.sh")}
        return subprocess.run(["bash", str(HELPER), *args], capture_output=True, text=True, timeout=60, env=env)

    def status(self):
        r = self.run("status")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def log(self):
        return self.calls.read_text() if self.calls.exists() else ""


def test_setup_asks_the_service_and_the_run_hands_over_a_one_time_password(tmp_path):
    b = Box(tmp_path)
    assert b.status()["state"] == "off" and not b.status()["requested"]
    r = b.run("setup")
    assert r.returncode == 0, r.stderr
    assert "systemctl start --no-block posterchan-anticheat.service" in b.log()
    assert b.status()["requested"] and b.status()["state"] == "requested"
    r = b.run("run")
    assert r.returncode == 0, r.stderr
    assert "gentoo.sh anticheat" in b.log()
    st = b.status()
    assert st["state"] == "enrol", st
    pw = st["password"]
    assert len(pw) == 8 and pw.isdigit(), ("a one-time password of eight digits", pw)
    assert f"PW={pw}" in (b.state / "anticheat.log").read_text(), "gentoo.sh was not handed the password shown"
    assert not st["requested"], "the request outlived a successful set-up"


def test_once_secure_boot_is_on_the_password_is_no_longer_shown(tmp_path):
    b = Box(tmp_path, sb="SecureBoot enabled")
    b.run("setup"); b.run("run")
    st = b.status()
    assert st["state"] == "enabled" and "password" not in st, st


def test_a_failed_setup_says_so_with_its_log_and_keeps_the_request(tmp_path):
    b = Box(tmp_path, gentoo_rc=1)
    b.run("setup")
    r = b.run("run")
    assert r.returncode == 1
    st = b.status()
    assert st["state"] == "failed" and "emerging shim" in st.get("log", ""), st
    assert "password" not in st
    assert st["requested"], "a failed set-up dropped the request, so it can never be retried"


def test_run_without_a_request_does_nothing(tmp_path):
    b = Box(tmp_path)
    assert b.run("run").returncode == 0
    assert "gentoo.sh" not in b.log()


def test_undo_runs_anticheat_off_in_the_background(tmp_path):
    b = Box(tmp_path)
    r = b.run("undo")
    assert r.returncode == 0, r.stderr
    assert "systemd-run --quiet --no-block --unit=posterchan-anticheat-off --collect" in b.log()
    assert b.status()["state"] == "undoing"
    assert b.run("run-off").returncode == 0
    assert "gentoo.sh anticheat-off" in b.log() and b.status()["state"] == "off"


def test_anything_else_is_refused():
    r = subprocess.run(["bash", str(HELPER), "rm", "-rf", "/"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "usage" in r.stderr


def test_the_package_installs_it_its_service_and_a_read_only_grant():
    eb = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("posterchanos-shell-*.ebuild")).read_text()
    assert " pc-anticheat " in eb and 'doins "${FILESDIR}/posterchan-anticheat.service"' in eb
    assert 'newins "${FILESDIR}/posterchan-anticheat.sudoers" posterchan-anticheat' in eb
    rule = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/posterchan-anticheat.sudoers").read_text()
    grants = [l for l in rule.splitlines() if l.strip() and not l.startswith("#")]
    assert grants == ["%posterchan ALL=(root) NOPASSWD: /usr/local/bin/pc-anticheat status"], \
        ("only STATUS may be run without being the administrator", grants)
    unit = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/posterchan-anticheat.service").read_text()
    assert "ConditionPathExists=/etc/posterchan/anticheat-requested" in unit
    assert "ExecStart=/usr/local/bin/pc-anticheat run" in unit
    # A oneshot in multi-user.target holds the boot (and so the desktop) until a ~600 MB set-up has finished.
    types = [l.split("=", 1)[1].strip() for l in unit.splitlines() if l.startswith("Type=")]
    assert types == ["exec"], ("the first boot must not wait for the set-up", types)


def _install_hook():
    sh = (ROOT / "os/gentoo.sh").read_text()
    i = sh.index('\tif [ "${PC_INSTALL_SECUREBOOT:-}" = 1 ]; then')
    return sh[i:sh.index("\n\tfi\n", i) + 4]


def _run_hook(tmp, on):
    target = tmp / "target"
    target.mkdir()
    bin_ = tmp / "hbin"
    bin_.mkdir()
    _exe(bin_ / "sudo", '"$@"')       # the install runs as root; the hook's sudo is a pass-through here
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "TARGET": str(target)}
    if on:
        env["PC_INSTALL_SECUREBOOT"] = "1"
    r = subprocess.run(["bash", "-c", _install_hook()], capture_output=True, text=True, timeout=30, env=env)
    return r, target


def test_the_installers_checkbox_leaves_the_first_boot_request_and_enables_the_service(tmp_path):
    r, t = _run_hook(tmp_path, True)
    assert r.returncode == 0, r.stderr
    assert (t / "etc/posterchan/anticheat-requested").exists()
    link = t / "etc/systemd/system/multi-user.target.wants/posterchan-anticheat.service"
    assert os.readlink(link) == "/usr/lib/systemd/system/posterchan-anticheat.service"
    assert "first boot" in r.stdout


def test_an_unticked_box_leaves_the_installed_system_alone(tmp_path):
    r, t = _run_hook(tmp_path, False)
    assert r.returncode == 0 and not (t / "etc").exists()


def test_the_unit_path_the_hook_links_is_where_the_package_puts_it():
    eb = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("posterchanos-shell-*.ebuild")).read_text()
    i = eb.index('doins "${FILESDIR}/posterchan-anticheat.service"')
    assert "insinto /usr/lib/systemd/system" in eb[max(0, eb.rfind("insinto", 0, i) - 1):i], \
        "the installer links /usr/lib/systemd/system/posterchan-anticheat.service; the package must install it there"
