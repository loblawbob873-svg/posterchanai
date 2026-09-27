"""An install copies the kernel image that belongs to the version its initramfs is built for.

Run: venv-unified/bin/python -m pytest tests/test_installed_kernel_matches_its_initramfs.py

Found by the ISO gates on 2026-09-27: the build VM had upgraded 6.18.48 -> 6.18.50 and not rebooted,
so the live medium booted 6.18.48 while the newest module tree on it was 6.18.50. The installer
copied the LIVE kernel (/boot/vmlinuz) into /boot/<machine-id>/6.18.50/linux and built a 6.18.50
initramfs beside it -- every install then stopped at boot with `crypt: unknown target type`, both
install gates and the GUI and server gates with them. `pc_target_kernel_image` is run here for real
against a fake target shaped exactly like that medium.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text()
LIVE, NEW = "6.18.48-gentoo-dist-bin", "6.18.50-gentoo-dist-bin"


def _fn(name):
    start = GENTOO.index(f"{name}() {{")
    return GENTOO[start:GENTOO.index("\n}\n", start) + 3]


def pick(tmp_path, version, *, per_version_images=(), trees=(LIVE, NEW), live_image=True, symlink=False):
    root = tmp_path / "target"
    for v in trees:
        (root / "lib/modules" / v).mkdir(parents=True)
    for v in per_version_images:
        img = root / "lib/modules" / v / "vmlinuz"
        if symlink:                        # how gentoo-kernel-bin ships it: a link into /usr/src
            src = root / "usr/src" / f"linux-{v}" / "arch/x86/boot/bzImage"
            src.parent.mkdir(parents=True)
            src.write_text(v)
            img.symlink_to(f"../../../usr/src/linux-{v}/arch/x86/boot/bzImage")
        else:
            img.write_text(v)
    if live_image:
        (root / "boot").mkdir(parents=True)
        (root / "boot/vmlinuz").write_text(LIVE)
    script = (f'uname() {{ echo "{LIVE}"; }}\n' + _fn("pc_target_kernel_image")
              + f'\npc_target_kernel_image "{root}" "{version}" || echo NONE\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    if out == "NONE":
        return None
    ver, img = out.split(" ", 1)
    return ver, Path(img).read_text()      # what the copied image actually IS


def test_the_reported_medium_installs_the_new_kernel_with_its_own_image(tmp_path):
    ver, image = pick(tmp_path, NEW, per_version_images=[NEW], symlink=True)
    assert (ver, image) == (NEW, NEW), f"installed {image} under {ver}"


def test_with_no_image_for_the_newest_tree_the_live_kernel_keeps_its_own_version(tmp_path):
    """The old behaviour's exact failure: the live kernel filed under the newest version."""
    ver, image = pick(tmp_path, NEW)
    assert image == LIVE and ver == LIVE, f"a {image} kernel was installed as {ver} -- it cannot boot"


def test_nothing_to_boot_is_said_rather_than_guessed(tmp_path):
    assert pick(tmp_path / "a", NEW, live_image=False) is None
    assert pick(tmp_path / "b", NEW, trees=(NEW,)) is None, "the live kernel was paired with modules not its own"


def test_the_installer_copies_what_the_helper_chose():
    """The copy into /boot/<machine-id>/<version>/linux takes the paired image and version, never the
    live /boot/vmlinuz directly."""
    block = GENTOO[GENTOO.index('KVER="$(ls $TARGET/lib/modules'):GENTOO.index("kernel and initramfs are in")]
    copies = re.findall(r'cp -fL? (\S+) "\$TARGET/boot/\$MID/\$KVER/linux"', block)
    assert copies == ['"$KIMG"'], copies
    assert block.index("pc_target_kernel_image") < block.index('cp -fL "$KIMG"')
    assert block.index('KVER="${_pair%% *}"') < block.index("dracut --force"), \
        "the initramfs must be built for the version of the image that was copied"
