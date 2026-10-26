"""pc-eac-check answers Easy Anti-Cheat Linux v2's requirements from the machine, and never mistakes "could not
name the signer" for "unsigned" (Epic, 2026-10-14: signed kernel + modules, Secure Boot, TPM 2.0, measured boot,
IOMMU without passthrough, lockdown=integrity, kexec disabled, BPF LSM).

Runs the SHIPPED script against a fake /sys and /proc and a stub modinfo.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-eac-check"


def machine(tmp, *, sb=1, tpm="2", log=b"x" * 64, iommu=True, ident=False, lockdown="none", kexec="0", lsm="lockdown,bpf",
            cmdline="root=x", modules=(("good", "sig", "Gentoo"),), taint="0", unreadable_log=False, log_from=None):
    r = tmp / "root"
    (r / "sys/firmware/efi/efivars").mkdir(parents=True)
    (r / "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(bytes([6, 0, 0, 0, sb]))
    if tpm:
        (r / "sys/class/tpm/tpm0").mkdir(parents=True)
        (r / "sys/class/tpm/tpm0/tpm_version_major").write_text(tpm)
    (r / "sys/kernel/security/tpm0").mkdir(parents=True)
    if log is not False:
        (r / "sys/kernel/security/tpm0/binary_bios_measurements").write_bytes(log)
    if log_from:
        (r / "sys/kernel/security/tpm0/binary_bios_measurements").unlink()
        (r / "sys/kernel/security/tpm0/binary_bios_measurements").symlink_to(log_from)
    if unreadable_log:
        (r / "sys/kernel/security/tpm0/binary_bios_measurements").chmod(0)
    (r / "sys/class/iommu").mkdir(parents=True)
    if iommu:
        (r / "sys/class/iommu/dmar0").mkdir()
    (r / "sys/kernel/iommu_groups/0").mkdir(parents=True)
    (r / "sys/kernel/iommu_groups/0/type").write_text("identity" if ident else "DMA-FQ")
    (r / "sys/kernel/security/lockdown").write_text({"none": "[none] integrity confidentiality",
                                                     "integrity": "none [integrity] confidentiality"}[lockdown])
    (r / "sys/kernel/security/lsm").write_text(lsm)
    (r / "proc/sys/kernel").mkdir(parents=True)
    (r / "proc/sys/kernel/kexec_load_disabled").write_text(kexec)
    (r / "proc/sys/kernel/tainted").write_text(taint)
    (r / "proc/cmdline").write_text(cmdline)
    (r / "proc/modules").write_text("".join(f"{m} 1 0 - Live 0x0\n" for m, _, _ in modules))
    stub = tmp / "modinfo"
    lines = ["#!/bin/bash", 'f="$2"; m="$3"', 'case "$m:$f" in']
    for m, sig, signer in modules:
        lines.append(f'  {m}:sig_id) echo "{"PKCS#7" if sig else ""}";;')
        lines.append(f'  {m}:signer) echo "{signer}";;')
    lines += ["esac"]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    env = {**os.environ, "PC_ROOT": str(r), "PC_MODINFO": str(stub)}
    p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout


def test_a_machine_that_meets_every_check_passes(tmp_path):
    rc, out = machine(tmp_path, lockdown="integrity", kexec="1", cmdline="root=x lockdown=integrity")
    assert rc == 0, out
    assert "Module signers seen: Gentoo" in out and "Epic" in out   # and it says the key question is Epic's


def test_each_missing_requirement_is_named(tmp_path):
    rc, out = machine(tmp_path, sb=0, tpm="", log=False, iommu=False, lsm="lockdown,yama")
    assert rc == 1
    for words in ("Secure Boot is off", "No TPM", "Measured boot", "IOMMU is off", "lockdown is off", "kexec is allowed",
                  "BPF LSM not loaded"):
        assert words in out, (words, out)


def test_passthrough_is_caught_from_the_command_line_or_the_groups(tmp_path):
    _, out = machine(tmp_path / "a", cmdline="root=x iommu=pt")
    assert "passthrough" in out
    _, out = machine(tmp_path / "b", ident=True)
    assert "passthrough (1 identity group(s))" in out


def test_a_signer_it_cannot_name_is_not_called_unsigned(tmp_path):
    rc, out = machine(tmp_path, lockdown="integrity", kexec="1", modules=(("a", "sig", ""), ("b", "sig", "")))
    assert rc == 0 and "Every loaded module is signed" in out and "2 signed module(s)" in out, out


def test_an_unsigned_module_is_named_and_the_taint_flag_counts(tmp_path):
    rc, out = machine(tmp_path, lockdown="integrity", kexec="1", modules=(("nvidia", "", ""),), taint=str(8192))
    assert rc == 1 and "Unsigned modules loaded: nvidia" in out and "taint flag E" in out


def test_it_ships_with_the_desktop():
    assert "pc-eac-check" in (ROOT / "os/gentoo.sh").read_text()
    ebuild = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("*.ebuild")).read_text()
    assert "pc-eac-check" in ebuild


def test_a_real_event_log_is_found_though_securityfs_reports_size_zero(tmp_path, monkeypatch):
    """Measured on the Secure Boot VM (2026-10-26): binary_bios_measurements is there, root:tss 0440, and `stat` says
    0 bytes like every securityfs file -- the old `[ -s ]` answered "no TPM event log visible" on a machine that had
    one. A fake file cannot be size-0-with-content, so the stand-ins are: readable with bytes, and present but not
    readable (a normal user)."""
    assert os.stat("/proc/version").st_size == 0      # the securityfs shape: reports 0 bytes, reads as text
    _, out = machine(tmp_path / "a", log_from="/proc/version")
    assert "ok" in out and "Measured boot (the firmware's TPM event log is there)" in out, out
    if os.geteuid() == 0:
        return
    rc, out = machine(tmp_path / "b", lockdown="integrity", kexec="1", log=b"x" * 64, unreadable_log=True)
    assert rc == 0 and "readable by root and the tss group" in out, out


def test_an_empty_event_log_is_not_called_measured_boot(tmp_path):
    _, out = machine(tmp_path, log=b"")
    assert "no TPM event log visible" in out, out
