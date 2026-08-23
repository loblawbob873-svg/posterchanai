"""`bootloader()` RUN against a fake target, because reading it proves nothing.

    venv-unified/bin/python -m pytest tests/test_gentoo_bootloader_entry.py

THE REPORT: "liveCD install does not configure systemd boot at all, missing entries, all i see is
reboot into firmware interface" — on a machine installed from an ISO that already carried the
kernel-layout fix, so the install ran the corrected script and still produced a disk that could not
name a kernel.

WHAT RUNNING IT SHOWED, and what no amount of reading it had: every write of the boot entry failed
with "No such file or directory", six times, because **/boot/loader/entries did not exist**. The
entry is written with `>`, which cannot create a directory. `bootctl install` normally makes it —
and in a chroot that is precisely the thing most likely not to have run, since it needs to identify
the ESP through udev and needs EFI variables, and its failure prints and exits nonzero while the
install carries on to report success. Meanwhile `rm -rf /boot/loader/entries/*` at the top has
already removed whatever a previous install left, which is why the menu is EMPTY rather than stale.

So the fix is three things, and this file checks all of them by executing the function: make the
directory regardless, say so when bootctl fails, and READ BACK the entry — systemd-boot silently
drops an entry whose kernel file is missing, so "the file exists" is not the same as "it boots".

The function is run with its absolute paths rewritten under a temporary root, and with stubs on PATH
for everything it shells out to (bootctl, dracut, blkid, btrfs, findmnt). That is not the same as
installing Gentoo, and does not pretend to be: what it covers is the part that was silently wrong —
which paths get written, and whether anything notices when they are not.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "os", "gentoo.sh")
KVER = "6.12.31-gentoo-dist"
MID = "deadbeefmachineid0000000000000"

STUBS = {
    "bootctl": ('#!/bin/sh\n'
                'echo "[stub] bootctl $*"\n'
                '[ "${PC_BOOTCTL_RC:-0}" = 0 ] || exit "$PC_BOOTCTL_RC"\n'
                'for x in "$@"; do case "$x" in --esp-path=*) esp=${x#*=};; esac; done\n'
                'mkdir -p "$esp/EFI/BOOT"\n'
                'printf EFI > "$esp/EFI/BOOT/BOOTX64.EFI"\n'),
    "dracut": '#!/bin/sh\nexit 0\n',
    "plymouth-set-default-theme": '#!/bin/sh\nexit 0\n',
    "depmod": '#!/bin/sh\nexit 0\n',
    "btrfs": '#!/bin/sh\necho 12345\n',
    "findmnt": '#!/bin/sh\necho 9999-8888\n',
    "blkid": ('#!/bin/sh\n'
              'if [ "$1" = "-s" ]; then echo "1111-2222-3333"; exit 0; fi\n'
              'echo "/dev/nvme0n1p1: TYPE=\\"vfat\\""\n'
              'echo "/dev/nvme0n1p2: TYPE=\\"crypto_LUKS\\""\n'),
}


def _fn(src, name):
    i = src.index(name + "() {")
    depth, k = 0, src.index("{", i)
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{name}: unbalanced braces")


@unittest.skipIf(not os.path.exists(SH), "no os/gentoo.sh here")
@unittest.skipIf(not shutil.which("bash"), "no bash")
class BootloaderWritesAnEntryThatNamesARealKernel(unittest.TestCase):

    def test_plymouth_helper_cannot_overwrite_recorded_selection(self):
        src = open(SH, encoding="utf-8").read()
        body = _fn(src, "_pc_select_plymouth_theme")
        self.assertGreater(body.index("_pc_record_plymouth_theme"),
                           body.index("plymouth-set-default-theme"))
        self.assertGreater(body.rindex("ln -sfn posterchanos/posterchanos.plymouth"),
                           body.index("plymouth-set-default-theme"))
    def _run(self, *, kernel=True, modules=True, bootctl_rc=0):
        src = open(SH, encoding="utf-8").read()
        body = (_fn(src, "partitionDetection") + "\n\n" +
                _fn(src, "_pc_record_plymouth_theme") + "\n\n" +
                _fn(src, "_pc_select_plymouth_theme") + "\n\n" +
                _fn(src, "bootloader"))
        for a, b in (("/boot", "$R/boot"), ("/etc/disk", "$R/etc/disk"),
                     ("/etc/machine-id", "$R/etc/machine-id"), ("/etc/crypttab", "$R/etc/crypttab"),
                     ("/etc/dracut.conf", "$R/etc/dracut.conf"),
                     ("/usr/lib/modules", "$R/usr/lib/modules"), ("/swap/swap", "$R/swap/swap"),
                     ("/sbin/blkid", "blkid"), ("/usr/bin/findmnt", "findmnt"),
                     ("/tmp/disk", "$R/tmp/disk")):
            body = body.replace(a, b)

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        target, stub = os.path.join(d, "t"), os.path.join(d, "stub")
        for sub in ("boot", "etc", "swap", "tmp", f"usr/lib/modules/{KVER}",
                    "usr/share/plymouth/themes/posterchanos"):
            os.makedirs(os.path.join(target, sub), exist_ok=True)
        open(os.path.join(target, "usr/share/plymouth/themes/posterchanos/posterchanos.plymouth"),
             "w").write("[Plymouth Theme]\nName=PosterChanOS\n")
        if not modules:
            shutil.rmtree(os.path.join(target, "usr/lib/modules", KVER))
        if kernel:
            open(os.path.join(target, "boot", "vmlinuz"), "w").write("fake kernel")
        open(os.path.join(target, "etc", "machine-id"), "w").write(MID + "\n")
        open(os.path.join(target, "etc", "disk"), "w").write("/dev/nvme0n1\nroot\nswapfile\n")
        open(os.path.join(target, "swap", "swap"), "w").write("")
        os.makedirs(stub, exist_ok=True)
        for name, text in STUBS.items():
            p = os.path.join(stub, name)
            open(p, "w").write(text)
            os.chmod(p, 0o755)

        script = os.path.join(d, "run.sh")
        open(script, "w").write("#!/bin/bash\nR=\"$1\"\nTARGET=\"$R\"\nAUTO_DECRYPT=False\n" + body + "\nbootloader\n")
        env = dict(os.environ, PATH=stub + os.pathsep + os.environ["PATH"],
                   PC_BOOTCTL_RC=str(bootctl_rc))
        r = subprocess.run(["bash", script, target], capture_output=True, text=True,
                           timeout=120, env=env)
        return target, (r.stdout or "") + (r.stderr or "")

    def _entries(self, target):
        d = os.path.join(target, "boot", "loader", "entries")
        return [os.path.join(d, f) for f in os.listdir(d)] if os.path.isdir(d) else []

    def test_an_entry_is_written_at_all(self):
        """The whole bug: `>` cannot create a directory, and nothing made
        /boot/loader/entries."""
        target, out = self._run()
        entries = self._entries(target)
        self.assertEqual(len(entries), 1,
                         "no boot entry was written — this is the empty sd-boot menu.\n" + out[-1200:])
        # NARROW ON PURPOSE. The harness's fake target has no /boot/EFI and no machine-id
        # directory until the repair makes one, so `chmod` and the version-deriving `ls` say so —
        # both are the function correctly reporting an empty target, not the bug. The bug is a
        # failed write to the LOADER FILE, and nothing else.
        for line in out.splitlines():
            if "No such file or directory" in line and "loader/" in line:
                self.fail("the boot entry could not be written:\n" + line)

    def test_the_entry_names_a_kernel_that_exists(self):
        """systemd-boot DROPS an entry whose `linux` file is missing, so an entry that exists is
        not the same as an entry that boots. That is how an empty menu looked like 'no entries'
        rather than 'a broken entry'."""
        target, _ = self._run()
        body = open(self._entries(target)[0], encoding="utf-8").read()
        m = re.search(r"^linux (\S+)$", body, re.M)
        self.assertTrue(m, "the entry does not name a kernel at all:\n" + body)
        self.assertNotIn("//", m.group(1), "the kernel path has an empty version in it")
        self.assertTrue(os.path.isfile(os.path.join(target, "boot", m.group(1).lstrip("/"))),
                        f"the entry names {m.group(1)}, which is not on the disk")

    def test_offline_install_writes_a_firmware_bootable_esp(self):
        target, out = self._run()
        fallback = os.path.join(target, "boot", "EFI", "BOOT", "BOOTX64.EFI")
        self.assertTrue(os.path.getsize(fallback), "fresh UEFI NVRAM has no fallback loader\n" + out)
        self.assertIn("--no-variables install", out)

    def test_bootctl_failure_is_an_install_failure(self):
        target, out = self._run(bootctl_rc=1)
        self.assertIn("could not install systemd-boot", out)
        self.assertEqual(self._entries(target), [],
                         "an installer without an EFI executable must not report a usable disk")

    def test_no_kernel_means_a_refusal_not_a_broken_entry(self):
        """With nothing to boot, writing an entry that names nothing is worse than writing none:
        it reports success and produces a menu that silently hides it."""
        target, out = self._run(kernel=False, modules=False)
        self.assertIn("No kernel to boot", out)
        self.assertEqual(self._entries(target), [])

    def test_posterchan_splash_is_selected_before_dracut(self):
        src = open(SH, encoding="utf-8").read()
        body = _fn(src, "bootloader")
        theme = body.index("_pc_select_plymouth_theme")
        dracut = body.index("dracut --force")
        self.assertLess(theme, dracut)
        self.assertNotIn("plymouth-set-default-theme solar", body)

    def test_splash_selection_writes_plymouths_real_inputs_directly(self):
        src = open(SH, encoding="utf-8").read()
        body = _fn(src, "_pc_select_plymouth_theme")
        self.assertIn("default.plymouth", body)
        self.assertIn("ln -sfn posterchanos/posterchanos.plymouth", body)
        self.assertIn("_pc_record_plymouth_theme", body)
        self.assertIn("grep -q '^Theme=posterchanos$'", body)


if __name__ == "__main__":
    unittest.main()
