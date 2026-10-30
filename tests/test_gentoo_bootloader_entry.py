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
MID = "0123456789abcdef0123456789abcdef"
OLD_KVER = "6.12.20-gentoo-dist"
VM_TOKEN = "6ed1a9cb0331488fb15f91fb73891620"   # the ISO build VM's machine-id, inherited by an install

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
    # PC_NO_ROOT: nothing is mounted at / that findmnt can name (the last-resort answer is gone too).
    "findmnt": '#!/bin/sh\ncase "$*" in *" /") [ "${PC_NO_ROOT:-0}" = 1 ] && exit 1;; esac\necho 9999-8888\n',
    "blkid": ('#!/bin/sh\n'
              # PC_NO_MAPPER: the machine was started another way, so /dev/mapper/luks-<uuid> does not exist
              'if [ "$1" = "-s" ]; then case "$*" in */dev/mapper/*) [ "${PC_NO_MAPPER:-0}" = 1 ] && exit 2;; esac\n'
              '  echo "1111-2222-3333"; exit 0; fi\n'
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
    def _run(self, *, kernel=True, modules=True, bootctl_rc=0, no_mapper=False, no_root=False, old_entry=False, check_rc=None, tv=False):
        src = open(SH, encoding="utf-8").read()
        body = (_fn(src, "partitionDetection") + "\n\n" +
                _fn(src, "_pc_record_plymouth_theme") + "\n\n" +
                _fn(src, "_pc_select_plymouth_theme") + "\n\n" +
                _fn(src, "alignKernelEntryToken") + "\n\n" +
                _fn(src, "pc_prune_kernels") + "\n\n" +
                ((_fn(src, "pc_kernel_can_unlock") + "\n\n" + _fn(src, "pc_best_kernel_version") + "\n\n" +
                  (_fn(src, "pc_kernel_image") + "\n\n" if "pc_kernel_image() {" in src else "")) if tv else "") +
                _fn(src, "bootloader"))
        for a, b in (("/boot", "$R/boot"), ("/etc/disk", "$R/etc/disk"),
                     ("/etc/machine-id", "$R/etc/machine-id"), ("/etc/crypttab", "$R/etc/crypttab"),
                     ("/etc/dracut.conf", "$R/etc/dracut.conf"),
                     ("/etc/kernel", "$R/etc/kernel"),
                     ("/var/tmp", "$R/var/tmp"),
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
        if tv:   # the install's kernel under this machine's id, newer ones under the build VM's token (see the test)
            for ver, tok in ((OLD_KVER, MID), (KVER, VM_TOKEN)):
                os.makedirs(os.path.join(target, "boot", tok, ver), exist_ok=True)
                open(os.path.join(target, "boot", tok, ver, "linux"), "w").write("kernel " + ver)
                md = os.path.join(target, "usr/lib/modules", ver, "kernel/drivers/md")
                os.makedirs(md, exist_ok=True)
                open(os.path.join(md, "dm-crypt.ko"), "w").write("")
            os.makedirs(os.path.join(target, "etc/kernel"), exist_ok=True)
            open(os.path.join(target, "etc/kernel/entry-token"), "w").write(VM_TOKEN + "\n")
            os.makedirs(os.path.join(target, "boot/loader"), exist_ok=True)
            open(os.path.join(target, "boot/loader/loader.conf"), "w").write("default " + MID + "-*\ntimeout 1\n")
        if old_entry:
            os.makedirs(os.path.join(target, "boot/loader/entries"), exist_ok=True)
            open(os.path.join(target, "boot/loader/entries", "old.conf"), "w").write("title Working\n")
        os.makedirs(stub, exist_ok=True)
        for name, text in STUBS.items():
            p = os.path.join(stub, name)
            open(p, "w").write(text)
            os.chmod(p, 0o755)
        check = os.path.join(stub, "pc-boot-check")
        open(check, "w").write("#!/bin/sh\necho '[stub] pc-boot-check'\nexit %d\n" % (check_rc or 0))
        os.chmod(check, 0o755)

        script = os.path.join(d, "run.sh")
        open(script, "w").write("#!/bin/bash\nR=\"$1\"\nTARGET=\"$R\"\nAUTO_DECRYPT=False\n" + body + "\nbootloader\n")
        env = dict(os.environ, PATH=stub + os.pathsep + os.environ["PATH"],
                   PC_BOOTCTL_RC=str(bootctl_rc), PC_NO_MAPPER="1" if no_mapper else "0",
                   PC_NO_ROOT="1" if no_root else "0",
                   PC_BOOT_CHECK=check if check_rc is not None else os.path.join(stub, "absent"))
        r = subprocess.run(["bash", script, target], capture_output=True, text=True,
                           timeout=120, env=env)
        return target, (r.stdout or "") + (r.stderr or "")

    def _entries(self, target):
        d = os.path.join(target, "boot", "loader", "entries")
        return [os.path.join(d, f) for f in os.listdir(d)] if os.path.isdir(d) else []

    # THE LAPTOP, 2026-10-29: `gentoo.sh bootloader` run on a machine started through Secure Boot mode, where
    # the disk is open as /dev/mapper/root -- not the /dev/mapper/luks-<uuid> this script assumes. blkid printed
    # nothing, and the entry and the initramfs said `root=UUID=` with no value: the next start waited on
    # /dev/disk/by-uuid" for ever.


    def test_the_root_comes_from_what_is_mounted_when_the_mapper_name_is_wrong(self):
        target, out = self._run(no_mapper=True)
        entries = self._entries(target)
        self.assertTrue(entries, out)
        text = "".join(open(e).read() for e in entries) + open(os.path.join(target, "etc/dracut.conf")).read()
        self.assertNotRegex(text, r"root=UUID=(\s|$)", "an empty root= was written")
        self.assertIn("root=UUID=9999-8888 ", text, out)

    # "we need to prevent this" (2026-10-29): the new entry is CHECKED (pc-boot-check) before this says it is
    # done; a failed check puts back what started the machine before, and no old kernel is pruned.
    def test_an_entry_the_check_refuses_is_replaced_by_the_one_that_worked(self):
        target, out = self._run(old_entry=True, check_rc=1)
        self.assertIn("[stub] pc-boot-check", out)
        self.assertIn("WOULD NOT START THIS MACHINE", out)
        self.assertEqual([os.path.basename(e) for e in self._entries(target)], ["old.conf"], out)
        self.assertNotIn("Removing old kernels", out, "old kernels were pruned before the new boot was proven")

    def test_a_checked_entry_stays_and_only_then_are_old_kernels_pruned(self):
        target, out = self._run(old_entry=True, check_rc=0)
        names = [os.path.basename(e) for e in self._entries(target)]
        self.assertIn(f"{MID}-{KVER}.conf", names, out)
        self.assertLess(out.index("[stub] pc-boot-check"), out.index("Removing old kernels"), out)

    def test_with_no_root_to_name_nothing_is_removed(self):
        target, out = self._run(no_mapper=True, no_root=True, old_entry=True)
        self.assertIn("Could not tell the root file system's UUID", out)
        self.assertEqual([os.path.basename(e) for e in self._entries(target)], ["old.conf"],
                         "the working entries were deleted for an entry that could not be written")

    def test_package_kernel_entries_keep_the_installed_loader_default(self):
        target, out = self._run()
        with open(os.path.join(target, "etc/kernel/entry-token")) as f:
            self.assertEqual(f.read(), MID + "\n", out)
        with open(os.path.join(target, "boot/loader/loader.conf")) as f:
            self.assertIn("default " + MID + "-*", f.read())

    # THE TV, 2026-10-30: "i reinstalled bootloader to see new posterchan logo splash" -- and the desktop went to software
    # rendering. kernel-install had put every kernel for six weeks under /boot/<the build VM's machine-id> (the install
    # inherited that entry token), so bootloader() saw only the install's own kernel, booted it, and its NVIDIA module no
    # longer matched the userspace.
    def test_a_reinstall_boots_the_newest_kernel_even_under_another_entry_token(self):
        target, out = self._run(tv=True)
        names = [os.path.basename(e) for e in self._entries(target)]
        self.assertEqual(names, [f"{MID}-{KVER}.conf"], out[-1500:])
        body = open(self._entries(target)[0], encoding="utf-8").read()
        self.assertIn(f"linux /{MID}/{KVER}/linux", body)
        with open(os.path.join(target, "boot", MID, KVER, "linux")) as f:
            self.assertEqual(f.read(), "kernel " + KVER, "the newer kernel's image was not put where the entry points")
        with open(os.path.join(target, "etc/kernel/entry-token")) as f:
            self.assertEqual(f.read(), MID + "\n", "future kernels would still go to the build VM's directory")

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


PRUNE_STUBS = {
    # the running kernel
    "uname": '#!/bin/sh\necho 6.18.8-gentoo-dist\n',
    # who owns which module tree, and which versions of a package are installed
    "portageq": ('#!/bin/sh\n'
                 'case "$1" in\n'
                 '  owners) shift 2; for p in "$@"; do case "$p" in\n'
                 '    */6.18.40-gentoo-dist-bin) echo sys-kernel/gentoo-kernel-bin-6.18.40; echo "\t$p"; exit 0;;\n'
                 '    */6.18.54-gentoo-dist-bin) echo sys-kernel/gentoo-kernel-bin-6.18.54; echo "\t$p"; exit 0;;\n'
                 '    */6.19.1-gentoo-dist-bin) echo sys-kernel/gentoo-kernel-bin-6.19.1; echo "\t$p"; exit 0;;\n'
                 '  esac; done; exit 1;;\n'
                 '  match) printf "sys-kernel/gentoo-kernel-bin-6.18.40\\nsys-kernel/gentoo-kernel-bin-6.18.54\\nsys-kernel/gentoo-kernel-bin-6.19.1\\n";;\n'
                 'esac\n'),
    # an unmerge removes the package's module tree, as Portage would
    "emerge": ('#!/bin/sh\necho "$@" >> "$R/emerge.log"\n'
               'for a; do case "$a" in =*) v=${a##*-bin-}; rm -rf "$R/usr/lib/modules/$v-gentoo-dist-bin";; esac; done\n'),
}


@unittest.skipIf(not os.path.exists(SH), "no os/gentoo.sh here")
@unittest.skipIf(not shutil.which("bash"), "no bash")
class KernelPruning(unittest.TestCase):
    """"gentoo.sh bootloader causes issues with deleting the kernel modules": the cleanup rm'd every module
    tree but the chosen one -- the RUNNING kernel's, and trees Portage owns (server1: gentoo-kernel-bin-6.18.54
    installed, /lib/modules/6.18.54-gentoo-dist-bin gone). pc_prune_kernels, RUN against a fake root."""

    def _run(self, keep, trees):
        src = open(SH, encoding="utf-8").read()
        body = _fn(src, "pc_prune_kernels").replace("/usr/lib/modules", "$R/usr/lib/modules").replace("/boot", "$R/boot")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        root, stub = os.path.join(d, "r"), os.path.join(d, "stub")
        for v in trees:
            os.makedirs(os.path.join(root, "usr/lib/modules", v, "kernel"))
            os.makedirs(os.path.join(root, "boot", MID, v))
            open(os.path.join(root, "boot", MID, v, "linux"), "w").write("k")
        os.makedirs(stub)
        for name, text in PRUNE_STUBS.items():
            open(os.path.join(stub, name), "w").write(text)
            os.chmod(os.path.join(stub, name), 0o755)
        script = os.path.join(d, "run.sh")
        open(script, "w").write("#!/bin/bash\nR=\"$1\"\nexport R\n" + body + f"\npc_prune_kernels {MID} {keep}\n")
        r = subprocess.run(["bash", script, root], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PATH=stub + os.pathsep + os.environ["PATH"]))
        left = sorted(os.listdir(os.path.join(root, "usr/lib/modules")))
        esp = sorted(os.listdir(os.path.join(root, "boot", MID)))
        log = open(os.path.join(root, "emerge.log")).read() if os.path.exists(os.path.join(root, "emerge.log")) else ""
        return left, esp, log, r.stdout + r.stderr

    TREES = ["6.18.5-gentoo-dist", "6.18.8-gentoo-dist", "6.18.40-gentoo-dist-bin",
             "6.18.54-gentoo-dist-bin", "6.19.1-gentoo-dist-bin"]

    def test_only_unused_kernels_go_and_owned_ones_go_through_portage(self):
        left, esp, log, out = self._run("6.18.54-gentoo-dist-bin", self.TREES)
        self.assertIn("6.18.54-gentoo-dist-bin", left, "the kernel being booted lost its modules\n" + out)
        self.assertIn("6.18.8-gentoo-dist", left, "the RUNNING kernel lost its modules\n" + out)
        self.assertIn("6.19.1-gentoo-dist-bin", left, "the newest installed kernel package was removed\n" + out)
        self.assertNotIn("6.18.5-gentoo-dist", left, "an unused, unowned tree survived (substring of the kept one)\n" + out)
        self.assertNotIn("6.18.40-gentoo-dist-bin", left, out)
        self.assertEqual(log.split(), ["--unmerge", "--quiet", "=sys-kernel/gentoo-kernel-bin-6.18.40"],
                         ("an owned tree must be removed by unmerging its package, and only that one", log))
        self.assertEqual(esp, ["6.18.54-gentoo-dist-bin", "6.18.8-gentoo-dist", "6.19.1-gentoo-dist-bin"],
                         ("the ESP keeps exactly the kernels that are kept", esp))

    def test_nothing_is_removed_when_the_kept_kernel_has_no_modules(self):
        left, esp, log, out = self._run("6.99.0-missing", self.TREES)
        self.assertEqual(left, sorted(self.TREES), out)
        self.assertEqual(log, "")
        self.assertIn("Not removing any kernel", out)
