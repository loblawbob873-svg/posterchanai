"""NVIDIA support is a licence, a branch pin and a package — and the pin is not optional.

The Quadro P1000 is Pascal (GP107). 580 is the last branch supporting Maxwell/Pascal/Volta,
which also makes it the widest branch for a product where one image boots every machine.

nvidia-drivers HAS a safety net for picking the right branch: it reads the installed card's
device id out of supported-gpus.json and tells you to mask anything newer. It cannot fire here.
It loops over `grep -l 0x10de /sys/bus/pci/devices/*/vendor`, and the host that builds this
image has no NVIDIA card, so it finds nothing and sets no NV_LEGACY_MASK. We write the pin
because the mechanism that would otherwise write it is blind on a build host.

These RUN the installer's own function against a temporary /etc/portage.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")


def _run_unmask(tmp_path):
    """Execute unmaskPackages() with /etc/portage redirected under tmp_path."""
    body = SRC[SRC.index("unmaskPackages() {"):]
    body = body[:body.index("\n}\n") + 3]
    etc = tmp_path / "etc/portage"
    etc.mkdir(parents=True)
    decls = "\n".join(l for l in SRC.splitlines()
                      if l.startswith(("LICENSED_PACKAGES=", "PINNED_PACKAGES=",
                                       "MASKED_PACKAGES=", "SPECIAL_PACKAGE_USE=")))
    script = (decls + "\n" + body.replace("/etc/portage", str(etc))
              + f'\nSPECIAL_PACKAGE_USE=()\nunmaskPackages\n')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return etc


def test_the_licence_is_accepted_or_portage_refuses_the_driver(tmp_path):
    etc = _run_unmask(tmp_path)
    lic = (etc / "package.license/posterchan-managed").read_text()
    assert "x11-drivers/nvidia-drivers" in lic and "NVIDIA-r2" in lic, lic


def test_the_branch_is_pinned_because_the_ebuilds_own_check_is_blind_here(tmp_path):
    etc = _run_unmask(tmp_path)
    mask = (etc / "package.mask/posterchan-managed").read_text()
    assert ">=x11-drivers/nvidia-drivers-581" in mask, mask


def test_an_operators_own_entries_survive(tmp_path):
    """A plain file from an older install is migrated to 00-local, never deleted."""
    etc = tmp_path / "etc/portage"
    etc.mkdir(parents=True)
    (etc / "package.mask").write_text("sys-apps/theirs\n")
    body = SRC[SRC.index("unmaskPackages() {"):]
    body = body[:body.index("\n}\n") + 3]
    decls = "\n".join(l for l in SRC.splitlines()
                      if l.startswith(("LICENSED_PACKAGES=", "PINNED_PACKAGES=", "MASKED_PACKAGES=")))
    script = decls + "\n" + body.replace("/etc/portage", str(etc)) + "\nSPECIAL_PACKAGE_USE=()\nunmaskPackages\n"
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert (etc / "package.mask/00-local").read_text() == "sys-apps/theirs\n"
    assert ">=x11-drivers/nvidia-drivers-581" in (etc / "package.mask/posterchan-managed").read_text()


def test_the_driver_is_in_the_package_set():
    pkgs = re.search(r'POSTERCHANOS_PACKAGES="(.*?)"', SRC, re.S).group(1)
    assert "x11-drivers/nvidia-drivers" in pkgs


def test_we_do_not_write_a_second_copy_of_what_the_package_already_owns():
    """nvidia.conf ships `blacklist nouveau` and `options nvidia-drm modeset=1`.

    Duplicating either here would be two copies of one rule, which is how the getty override and
    its gate drifted apart and stopped every ISO build for a day.
    """
    code = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
    assert "blacklist nouveau" not in code, "the package's own modprobe policy already does this"
    assert "nvidia-drm.modeset" not in code, \
        "modeset is set by /etc/modprobe.d/nvidia.conf, not by our kernel command line"
