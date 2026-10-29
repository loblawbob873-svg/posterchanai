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

import pytest
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


class TpmBox(Box):
    """A machine set up for Secure Boot: a root on LUKS, a key file, and (maybe) a TPM whose PCR 7 is `pcr`."""

    def __init__(self, tmp, sb="SecureBoot enabled", tpm=True, pcr="AA" * 32, enrolled=False, keyfile=True, enroll_rc=0,
                 tpm_support=True, unseal_ok=True):
        super().__init__(tmp, sb=sb)
        (self.root / "etc/posterchan/anticheat").write_text("2026-10-29")
        self.tpm = tmp / "tpm0"
        if tpm:
            (self.tpm / "pcr-sha256").mkdir(parents=True)
            (self.tpm / "pcr-sha256/7").write_text(pcr + "\n")
        self.keyfile = tmp / "keyfile.key"
        if keyfile:
            self.keyfile.write_bytes(b"k" * 64)
        self.slots = tmp / "slots"
        self.slots.write_text("tpm2\n" if enrolled else "")
        L = f'echo "$(basename "$0") $*" >> "{self.calls}"'
        _exe(self.bin / "findmnt", 'echo /dev/mapper/root')
        _exe(self.bin / "cryptsetup", f'{L}; [ "$1" = status ] && [ "$2" = root ] && echo "  device:  /dev/nvme0n1p2"; '
                                      f'case "$*" in *--test-passphrase*) exit {0 if unseal_ok else 1};; esac; true')
        _exe(self.bin / "systemd-cryptenroll",
             f'{L}; [ "$1" = --version ] && {{ echo "systemd 261 (261.2) +PAM {"+TPM2" if tpm_support else "-TPM2"} +CRYPTSETUP"; exit 0; }}; '
             f'if [ $# -eq 1 ]; then echo "SLOT TYPE"; echo "   0 password"; cat "{self.slots}" | sed "s/^/   1 /"; exit 0; fi; '
             f'[ {enroll_rc} = 0 ] && echo tpm2 > "{self.slots}"; exit {enroll_rc}')

    def tpm_run(self):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "PC_AC_STATE_DIR": str(self.state), "PC_AC_ROOT": str(self.root),
               "PC_GENTOO_SH": str(self.bin / "gentoo.sh"), "PC_AC_TPM": str(self.tpm), "PC_AC_KEYFILE": str(self.keyfile)}
        r = subprocess.run(["bash", str(HELPER), "tpm"], capture_output=True, text=True, timeout=60, env=env)
        calls = self.calls.read_text() if self.calls.exists() else ""
        st = json.loads((self.state / "anticheat-tpm.json").read_text())["state"] if (self.state / "anticheat-tpm.json").exists() else None
        return r, calls, st


def test_the_first_secure_boot_start_seals_the_disk_key_into_the_tpm_with_nothing_to_type(tmp_path):
    """"tpm unlock should be automatic" (2026-10-29): enrolled with the existing key file, sealed to PCR 7."""
    r, calls, st = TpmBox(tmp_path).tpm_run()
    assert r.returncode == 0, r.stderr
    assert f"systemd-cryptenroll /dev/nvme0n1p2 --unlock-key-file={tmp_path}/keyfile.key --wipe-slot=tpm2 --tpm2-device=auto --tpm2-pcrs=7 --tpm2-public-key=" in calls
    assert "cryptsetup open --test-passphrase --token-only --token-type systemd-tpm2 /dev/nvme0n1p2" in calls
    assert st == "on"


def test_a_seal_the_tpm_cannot_open_is_never_reported_on(tmp_path):
    """The laptop, 2026-10-29: enrolment succeeded, and the TPM refused the image's RSA-4096 key at every start."""
    r, calls, st = TpmBox(tmp_path, unseal_ok=False).tpm_run()
    assert st == "failed" and r.returncode != 0, (st, calls)


def test_a_seal_from_before_the_fix_is_redone_once(tmp_path):
    box = TpmBox(tmp_path, enrolled=True)
    box.state.mkdir(parents=True, exist_ok=True)
    (box.state / "anticheat-tpm.pcr7").write_text("AA" * 32 + "\n")        # v1: bound to the image key too
    r, calls, st = box.tpm_run()
    assert "--tpm2-public-key=" in calls and st == "on", calls
    r, calls, st = box.tpm_run()
    assert calls.count("--wipe-slot=tpm2 --tpm2-device") == 1, "a v2 seal was redone at every start"


def test_a_current_seal_is_left_alone_and_a_stale_one_is_redone(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    box = TpmBox(a)
    box.tpm_run()
    r, calls, st = box.tpm_run()                       # same PCR 7, slot present: nothing written
    assert calls.count("--unlock-key-file") == 1 and st == "on", calls
    (box.tpm / "pcr-sha256/7").write_text("BB" * 32 + "\n")   # a key enrolled / firmware changed
    r, calls, st = box.tpm_run()
    assert calls.count("--unlock-key-file") == 2 and st == "on", "a seal for the old Secure Boot state was kept"


def test_secure_boot_off_never_seals_the_wrong_state(tmp_path):
    r, calls, st = TpmBox(tmp_path, sb="SecureBoot disabled").tpm_run()
    assert "cryptenroll" not in calls and st == "waiting"


@pytest.mark.parametrize("kw,want", [({"tpm": False}, "absent"), ({"keyfile": False}, "nokey"), ({"enroll_rc": 1}, "failed")])
def test_what_it_cannot_do_it_says(tmp_path, kw, want):
    r, calls, st = TpmBox(tmp_path, **kw).tpm_run()
    assert st == want, (st, calls)


def test_a_machine_without_secure_boot_mode_is_untouched(tmp_path):
    box = TpmBox(tmp_path)
    (box.root / "etc/posterchan/anticheat").unlink()
    r, calls, st = box.tpm_run()
    assert r.returncode == 0 and calls == "" and st is None


def test_the_status_and_the_unit_carry_it():
    unit = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/posterchan-anticheat-tpm.service").read_text()
    assert "ExecStart=/usr/local/bin/pc-anticheat tpm" in unit and "ConditionPathExists=/etc/posterchan/anticheat" in unit
    from tests.overlay_paths import shell_ebuild
    eb = shell_ebuild().read_text()
    assert 'doins "${FILESDIR}/posterchan-anticheat-tpm.service"' in eb and "systemctl enable posterchan-anticheat-tpm.service" in eb
    assert 'out["tpm"]' in HELPER.read_text()


def test_a_systemd_without_tpm_support_is_rebuilt_once_in_the_background(tmp_path):
    """Gentoo's prebuilt systemd answers "TPM2 support is disabled" (the laptop, 2026-10-29): the step starts ONE
    background rebuild (gentoo.sh anticheat-tpm, which comes back to seal) and says so -- never a compile at boot."""
    box = TpmBox(tmp_path, tpm_support=False)
    r, calls, st = box.tpm_run()
    assert st == "building" and calls.count("systemd-run") == 1 and "anticheat-tpm" in calls, calls
    assert "--unlock-key-file" not in calls
    r, calls, st = box.tpm_run()
    assert calls.count("systemd-run") == 1, "a second rebuild was started while the first runs"
    g = (ROOT / "os/gentoo.sh").read_text()
    assert 'SPECIAL_PACKAGE_USE+=("sys-apps/systemd tpm")' in g and 'elif [ "$1" = "anticheat-tpm" ]; then' in g


# ---- TRAVEL MODE ("If you travel, you will want a password to unlock", 2026-10-29) -----------------------------------
# Off the TPM alone is not enough: the key file sits on the UNENCRYPTED boot partition and inside the startup images.
# So travel mode wipes the TPM slot, moves the key file into the encrypted root and rebuilds the images without it;
# off puts it all back and re-seals. The stubs record every call and keep the real files.

class TravelBox(TpmBox):
    def __init__(self, tmp, dracut_rc=0, check_rc=0, **kw):
        super().__init__(tmp, enrolled=True, **kw)
        # pc-boot-check, as a stub that records being asked and answers check_rc.
        _exe(self.bin / "pc-boot-check", f'echo "pc-boot-check $*" >> "{self.calls}"; exit {check_rc}')
        mid = "m" * 32
        self.esp = tmp / "esp"
        (self.root / "etc/machine-id").write_text(mid + "\n")
        (self.esp / "loader/entries").mkdir(parents=True)
        (self.esp / mid / "6.18.54-gentoo-dist").mkdir(parents=True)
        (self.esp / "loader/entries" / f"{mid}-6.18.54-gentoo-dist.conf").write_text(
            "title Current\nversion 6.18.54-gentoo-dist\noptions rd.luks.key=/boot/keyfile.key\n")
        self.initrd = self.esp / mid / "6.18.54-gentoo-dist/initrd"
        self.initrd.write_text("with key")
        (self.root / "usr/lib/modules/6.18.54-gentoo-dist").mkdir(parents=True)
        # An uninstalled kernel's directory: no entry, no modules, and the key still inside (the laptop, 6.18.43).
        self.orphan = self.esp / mid / "6.18.43-gentoo-dist/initrd"
        self.orphan.parent.mkdir(parents=True); self.orphan.write_text("with key")
        self.conf = self.root / "etc/dracut.conf"
        self.conf.write_text('add_dracutmodules+=" crypt systemd-cryptsetup dm rootfs-block "\n'
                             'install_items+=" /boot/unlock.sh /boot/keyfile.key "\n')
        L = f'echo "$(basename "$0") $*" >> "{self.calls}"'
        # The image is rebuilt from the dracut.conf as it stands: the key is in it only while install_items names it.
        _exe(self.bin / "dracut", f'{L}; [ {dracut_rc} = 0 ] || exit {dracut_rc}; out="${{@: -2:1}}"; '
                                  f'if grep -q "^install_items.*keyfile" "{self.conf}"; then echo "with key" > "$out"; else echo "no key" > "$out"; fi')
        _exe(self.bin / "mokutil", 'echo "SecureBoot enabled"')

    def travel(self, how):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "PC_AC_STATE_DIR": str(self.state), "PC_AC_ROOT": str(self.root),
               "PC_GENTOO_SH": str(self.bin / "gentoo.sh"), "PC_AC_TPM": str(self.tpm), "PC_AC_KEYFILE": str(self.keyfile),
               "PC_AC_ESP": str(self.esp), "PC_BOOT_CHECK": str(self.bin / "pc-boot-check")}
        r = subprocess.run(["bash", str(HELPER), "travel", how], capture_output=True, text=True, timeout=60, env=env)
        return r, self.calls.read_text() if self.calls.exists() else ""


def test_travel_mode_takes_the_key_out_of_every_place_a_start_could_find_it(tmp_path):
    box = TravelBox(tmp_path)
    r, calls = box.travel("on")
    assert r.returncode == 0, r.stderr
    kept = box.root / "etc/posterchan/travel/keyfile.key"
    assert kept.read_bytes() == b"k" * 64 and oct(kept.stat().st_mode & 0o777) == "0o400"
    assert oct((box.root / "etc/posterchan/travel").stat().st_mode & 0o777) == "0o700"
    assert not box.keyfile.exists(), "the key file is still on the unencrypted boot partition"
    assert f"--unlock-key-file={kept} --wipe-slot=tpm2" in calls, "the TPM still opens the disk by itself"
    assert box.initrd.read_text().strip() == "no key", "the startup image still carries the key"
    assert not box.orphan.exists(), "an old kernel's image kept the key on the boot partition"
    assert "# travel mode: install_items" in box.conf.read_text()
    assert (box.root / "etc/posterchan/travel/on").exists()
    # ...and the boot service leaves it that way.
    r, calls, st = box.tpm_run()
    assert st == "travel" and calls.count("--tpm2-device=auto") == 0, calls


def test_travel_mode_off_puts_the_key_back_and_seals_again_with_nothing_to_type(tmp_path):
    box = TravelBox(tmp_path)
    box.travel("on")
    box.slots.write_text("")
    r, calls = box.travel("off")
    assert r.returncode == 0, r.stderr
    assert box.keyfile.read_bytes() == b"k" * 64
    assert box.initrd.read_text().strip() == "with key"
    assert box.conf.read_text().count("\ninstall_items+=") == 1 and "travel mode" not in box.conf.read_text()
    assert not (box.root / "etc/posterchan/travel/on").exists()
    assert not (box.root / "etc/posterchan/travel/keyfile.key").exists(), "a second copy of the key was left behind"
    assert "--tpm2-device=auto --tpm2-pcrs=7" in calls, "the TPM was not sealed again"


def test_a_failed_image_rebuild_changes_nothing(tmp_path):
    box = TravelBox(tmp_path, dracut_rc=1)
    conf = box.conf.read_text()
    r, calls = box.travel("on")
    assert r.returncode != 0 and "nothing was changed" in r.stderr
    assert box.keyfile.read_bytes() == b"k" * 64 and box.conf.read_text() == conf
    assert not (box.root / "etc/posterchan/travel/on").exists()
    assert "--wipe-slot" not in calls and box.slots.read_text() == "tpm2\n", "the TPM unlock was removed anyway"


def test_a_tpm_slot_that_cannot_be_wiped_puts_the_images_back(tmp_path):
    box = TravelBox(tmp_path, enroll_rc=1)
    r, _ = box.travel("on")
    assert r.returncode != 0 and "nothing was changed" in r.stderr
    assert box.initrd.read_text().strip() == "with key" and box.keyfile.exists()
    assert "travel mode" not in box.conf.read_text()


def test_turning_secure_boot_mode_off_takes_the_tpm_slot_with_it(tmp_path):
    """The laptop, 2026-10-29: back to the plain key-file unlock, with the PCR 7 seal left behind for nothing."""
    box = TravelBox(tmp_path)
    (box.state / "anticheat-tpm.json").write_text('{"state": "on"}')
    env = {"PATH": f"{box.bin}:/usr/bin:/bin", "PC_AC_STATE_DIR": str(box.state), "PC_AC_ROOT": str(box.root),
           "PC_GENTOO_SH": str(box.bin / "gentoo.sh"), "PC_AC_TPM": str(box.tpm), "PC_AC_KEYFILE": str(box.keyfile),
           "PC_AC_ESP": str(box.esp), "PC_BOOT_CHECK": str(box.bin / "pc-boot-check")}
    _exe(box.bin / "gentoo.sh", "true")
    r = subprocess.run(["bash", str(HELPER), "run-off"], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    calls = box.calls.read_text()
    assert f"--unlock-key-file={box.keyfile} --wipe-slot=tpm2" in calls, calls
    assert not (box.state / "anticheat-tpm.json").exists()
    assert box.keyfile.exists(), "the key file that unlocks the disk must stay"



# ---- EVERY CHANGE TO HOW THE MACHINE STARTS IS CHECKED BEFORE IT IS REPORTED ("we need to prevent this", 10-29) ----

def test_travel_mode_is_checked_and_a_refused_result_is_put_back(tmp_path):
    box = TravelBox(tmp_path, check_rc=1)
    r, calls = box.travel("on")
    assert r.returncode != 0 and "boot check refused" in r.stderr, r.stderr
    assert "pc-boot-check --quiet" in calls
    assert box.keyfile.read_bytes() == b"k" * 64, "the key file was not put back"
    assert not (box.root / "etc/posterchan/travel/on").exists()
    assert box.initrd.read_text().strip() == "with key"


def test_travel_mode_off_is_never_reported_done_unchecked(tmp_path):
    box = TravelBox(tmp_path)
    assert box.travel("on")[0].returncode == 0
    _exe(box.bin / "pc-boot-check", "exit 1")
    r, _ = box.travel("off")
    assert r.returncode != 0 and "do not restart" in r.stderr, r.stderr
    assert (box.root / "etc/posterchan/travel/keyfile.key").exists(), "the only kept copy of the key was deleted"


def test_secure_boot_mode_off_fails_when_the_boot_check_does(tmp_path):
    box = TravelBox(tmp_path, check_rc=1)
    env = {"PATH": f"{box.bin}:/usr/bin:/bin", "PC_AC_STATE_DIR": str(box.state), "PC_AC_ROOT": str(box.root),
           "PC_GENTOO_SH": str(box.bin / "gentoo.sh"), "PC_AC_TPM": str(box.tpm), "PC_AC_KEYFILE": str(box.keyfile),
           "PC_AC_ESP": str(box.esp), "PC_BOOT_CHECK": str(box.bin / "pc-boot-check")}
    _exe(box.bin / "gentoo.sh", "true")
    r = subprocess.run(["bash", str(HELPER), "run-off"], capture_output=True, text=True, timeout=60, env=env)
    st = json.loads((box.state / "anticheat.json").read_text())["state"]
    assert r.returncode != 0 and st == "failed", (st, r.stderr)
