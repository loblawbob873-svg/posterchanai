"""The live image must not hand an NVIDIA card to nouveau, and must say so when something does.

WHAT WAS MEASURED, because every part of this was already believed to be right.

The 2026-09-13 ISO reaches the desktop on an AMD machine and stops at a rescue console on an NVIDIA
one. Taking the shipped image apart (`unsquashfs -o <lba*2048>` straight out of the ISO):

  * all five proprietary modules are there, built for the image's own kernel —
    /usr/lib/modules/6.18.43-gentoo-dist-bin/video/{nvidia,nvidia-drm,nvidia-modeset,nvidia-uvm,
    nvidia-peermem}.ko, nvidia-drivers 580.173.02, `kernel-open` NOT in USE (so Pascal is supported)
  * the userspace is complete — libEGL_nvidia, /usr/lib64/gbm/nvidia-drm_gbm.so, the glvnd vendor
    json, the EGL external-platform json, libnvidia-egl-{gbm,wayland}
  * gp107 (Quadro P1000) nouveau firmware is present
  * `/etc/modprobe.d/nvidia.conf` carries `blacklist nouveau` AND `options nvidia-drm modeset=1`,
    in the squashfs AND inside the initramfs (dracut reads /usr/lib/dracut/dracut.conf.d even under
    `--conf /dev/null`, so nvidia-drivers' own install_items applied)

Nothing was missing, and the machine still came up on nouveau. Photographed off it:

    NVRM: GPU 0000:01:00.0 is already bound to nouveau.
    NVRM: The NVIDIA probe routine was not called for 1 device(s).
    NVRM: No NVIDIA devices probed.
    reason: Wayfire exited with status 1 after Wayfire exiting on its own

`blacklist` in modprobe.d is advice to modprobe about ALIASES. The kernel's `module_blacklist=` is
not advice. The initramfs shipped nouveau.ko; the installed path has omitted it since the LUKS work
and the live path never did. Both halves are fixed here, and the tests below fail without each.

`modprobe.blacklist=` is the spelling everyone reaches for and it would have done NOTHING: the
shipped vmlinuz contains the string `module_blacklist` and not `modprobe.blacklist`, and the shipped
initramfs has no handler for it either.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
SESSION = ROOT / "os/bin/pc-compositor-session"
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"

# The heredoc grub-mkrescue is handed. Read as text rather than executed: the value of the test is
# that it pins what ends up on the disc.
GRUB = GENTOO.split('cat >"$WORK/iso/boot/grub/grub.cfg" <<GRUB', 1)[-1].split("\nGRUB\n", 1)[0]

# The shell variables the heredoc interpolates, resolved once so the assertions below can be about
# THE COMMAND LINE THAT REACHES THE KERNEL rather than about which variable name was spelled where.
# (`PC_LIVE_SOFTWARE_CMDLINE` is itself built from `PC_LIVE_GPU_CMDLINE`; a text match would have
# read that entry as carrying no GPU rule at all.)
_VARS = dict(re.findall(r"""^(PC_LIVE_[A-Z_]+)=['"](.*)['"]$""", GENTOO, re.M))


def _expand(text):
    out = text
    for _ in range(4):  # the software entry's value is itself built from another one
        for name, value in sorted(_VARS.items(), key=lambda kv: -len(kv[0])):
            out = out.replace("$" + name, value)
    return out


# title -> the `linux` line as grub will see it, with every variable resolved.
MENUENTRIES = [(t, _expand(b)) for t, b in
               re.findall(r'menuentry "([^"]+)" \{(.*?)\n\}', GRUB, re.S)]


class LiveBootCmdline(unittest.TestCase):
    def test_every_ordinary_live_entry_refuses_nouveau_in_the_kernel(self):
        """The blacklist that matters is the one load_module() enforces, not modprobe's advice."""
        self.assertTrue(MENUENTRIES, "the live boot menu could not be read out of gentoo.sh")
        ordinary = [(t, b) for t, b in MENUENTRIES
                    if "open-source NVIDIA" not in t]
        self.assertGreaterEqual(len(ordinary), 3, [t for t, _ in MENUENTRIES])
        for title, body in ordinary:
            self.assertIn("module_blacklist=nouveau", body,
                          f'"{title}" boots without the nouveau refusal, so which entry you pick '
                          f"decides which driver gets the card:\n{body}")
        self.assertIn("PC_LIVE_GPU_CMDLINE='module_blacklist=nouveau'", GENTOO,
                      "module_blacklist= is the kernel's own parameter and the only one that binds")

    def test_the_spelling_is_the_one_this_kernel_actually_has(self):
        """`modprobe.blacklist=` is a distro convention, not a parameter of the shipped kernel.

        Measured against the ISO: `strings` on the decompressed vmlinuz has `module_blacklist` and
        not `modprobe.blacklist`, and the shipped initramfs carries no parser for it. Written on the
        command line it is silently ignored — the worst possible outcome for a boot parameter.
        """
        for title, body in MENUENTRIES:
            self.assertNotIn("modprobe.blacklist", body,
                             f'"{title}" carries a parameter this kernel ignores silently')
        code = "\n".join(l for l in GENTOO.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("modprobe.blacklist", code,
                         "that spelling does nothing on this kernel and reads exactly like a fix")

    def test_nothing_on_the_live_cmdline_was_dropped_to_make_room(self):
        """The serial console and the live-medium arguments are load-bearing; a GPU fix that loses
        `root=live:CDLABEL=` boots to a dracut shell, and one that loses console=ttyS0 takes every
        VM check offline."""
        for title, body in MENUENTRIES:
            self.assertIn("root=live:CDLABEL=$LABEL", body, title)
            self.assertIn("rd.live.image", body, title)
            self.assertIn("console=tty0 console=ttyS0,115200n8", body, title)
            self.assertIn("initrd /boot/initramfs.img", body, title)

    def test_there_is_a_way_out_for_a_card_the_proprietary_driver_cannot_drive(self):
        """A kernel-blacklisted module cannot be loaded later even deliberately.

        That is right for the default entry and would be a black screen with no escape for a
        pre-Maxwell card, which this profile deliberately does not support with nvidia-drivers. The
        menu offers the exact opposite instead — one line, not a conditional blacklist.
        """
        titles = [t for t, _ in MENUENTRIES]
        nouveau = [b for t, b in MENUENTRIES if "open-source NVIDIA" in t]
        self.assertEqual(len(nouveau), 1, titles)
        self.assertIn("module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm", nouveau[0])
        self.assertIn("pc.gpu=nouveau", nouveau[0])
        self.assertNotIn("module_blacklist=nouveau", nouveau[0],
                         "the escape hatch must not carry the blacklist it exists to lift")
        self.assertIn("PC_LIVE_NOUVEAU_CMDLINE='module_blacklist=nvidia,nvidia_drm,"
                      "nvidia_modeset,nvidia_uvm pc.gpu=nouveau'", GENTOO)

    def test_the_rescue_screen_names_menu_entries_that_exist(self):
        """Advice that names a boot entry by a title the menu does not have is worse than none."""
        session = SESSION.read_text(encoding="utf-8")
        titles = [t for t, _ in MENUENTRIES]
        for quoted in re.findall(r'\\"(PosterChan Live[^\\"]*)\\"', session):
            self.assertIn(quoted, titles,
                          f"the session tells the user to pick {quoted!r}, which is not in the "
                          f"boot menu: {titles}")


class LiveInitramfs(unittest.TestCase):
    def test_the_live_initramfs_omits_nouveau(self):
        """The shipped ISO's initrd contained nouveau.ko and a blacklist that is only advice.

        `--conf /dev/null --confdir <empty>` was read as "the live image needs no dracut policy",
        and it needs exactly one.
        """
        self.assertIn('printf \'%s\\n\' "$PC_DRACUT_OMIT_NOUVEAU" >"$WORK/dracut.conf.d/'
                      '10-posterchan-live.conf"', GENTOO,
                      "the live dracut build writes no nouveau omission")
        # ...and it must be written BEFORE dracut runs, not after.
        conf_at = GENTOO.index("10-posterchan-live.conf")
        dracut_at = GENTOO.index('dracut --force --no-hostonly --nolvmconf')
        self.assertLess(conf_at, dracut_at,
                        "the omission is written after the initramfs it is meant to change")

    def test_the_install_and_live_paths_share_one_rule(self):
        """Two copies of a boot rule is how the ISO ended up with an initrd the installed system
        would never have had: the install path has omitted nouveau since the LUKS work."""
        self.assertIn('PC_DRACUT_OMIT_NOUVEAU=\'omit_drivers+=" nouveau "\'', GENTOO)
        self.assertIn('echo "$PC_DRACUT_OMIT_NOUVEAU" >>/etc/dracut.conf', GENTOO)
        self.assertNotIn('omit_drivers+=\\" nouveau \\"', GENTOO,
                         "a second, hand-written copy of the rule is back")

    def test_nouveau_is_only_kept_off_a_card_this_image_can_drive_instead(self):
        """Blacklisting the open driver on an image that does not SHIP the proprietary one is a
        black screen by construction. The two facts belong together."""
        self.assertIn("x11-drivers/nvidia-drivers", GENTOO,
                      "nouveau is refused but nvidia-drivers is no longer installed")


class GpuDiagnosis(unittest.TestCase):
    """The decision, driven over a fabricated /sys.

    There is no NVIDIA card on any machine that runs these tests and QEMU cannot emulate one, so the
    only way to check what the screen SAYS is to hand the script the shapes it will meet.
    """

    def _sys(self, *, nvidia_driver=None, card_driver=None, modules=()):
        work = Path(tempfile.mkdtemp(prefix="pc-gpu-"))
        (work / "drivers").mkdir()
        (work / "drm").mkdir()
        (work / "pci").mkdir()
        if card_driver:
            dev = work / "drm/card0/device"
            dev.mkdir(parents=True)
            (work / "drivers" / card_driver).mkdir(exist_ok=True)
            (dev / "driver").symlink_to(work / "drivers" / card_driver)
            # A CONNECTOR lives in the same directory under the same glob.
            (work / "drm/card0-DP-1").mkdir()
        if nvidia_driver is not None:
            slot = work / "pci/0000:01:00.0"
            slot.mkdir(parents=True)
            (slot / "vendor").write_text("0x10de\n")
            (slot / "class").write_text("0x030000\n")
            (slot / "device").write_text("0x1cbb\n")
            if nvidia_driver:
                (work / "drivers" / nvidia_driver).mkdir(exist_ok=True)
                (slot / "driver").symlink_to(work / "drivers" / nvidia_driver)
            # the HDMI audio function of the same card: vendor 0x10de, class 0x0403, no driver
            audio = work / "pci/0000:01:00.1"
            audio.mkdir()
            (audio / "vendor").write_text("0x10de\n")
            (audio / "class").write_text("0x040300\n")
            (audio / "device").write_text("0x0fb9\n")
        (work / "modules").write_text("".join(f"{m} 1 0 - Live 0x0\n" for m in modules))
        (work / "cmdline").write_text("root=live:CDLABEL=PCLIVE\n")
        return work

    def _probe(self, work):
        env = dict(os.environ)
        env.update(PC_GPU_PROBE="1", HOME=str(work / "home"),
                   XDG_STATE_HOME=str(work / "state"),
                   PC_DRM_ROOT=str(work / "drm"), PC_PCI_ROOT=str(work / "pci"),
                   PC_PROC_MODULES=str(work / "modules"),
                   PC_PROC_CMDLINE=str(work / "cmdline"))
        proc = subprocess.run(["/bin/sh", str(SESSION)], env=env, capture_output=True,
                              text=True, timeout=60)
        return proc.stdout + proc.stderr

    def test_a_card_claimed_by_nouveau_is_named_as_that(self):
        out = self._probe(self._sys(nvidia_driver="nouveau", card_driver="nouveau",
                                    modules=("nouveau",)))
        self.assertIn("claimed by nouveau", out, out)
        self.assertIn("0000:01:00.0", out, "the sentence must name the card it is about:\n" + out)

    def test_no_driver_at_all_is_named_as_that_and_points_at_the_other_entry(self):
        out = self._probe(self._sys(nvidia_driver="", card_driver=None))
        self.assertIn("no display driver claimed the GPU", out, out)
        self.assertIn("open-source NVIDIA driver", out,
                      "a dead end must name the way out:\n" + out)

    def test_a_healthy_nvidia_machine_is_not_told_it_has_a_driver_problem(self):
        """The diagnosis is appended to whatever went wrong; on a working GPU it must be silent, or
        every unrelated crash reads as a graphics fault."""
        out = self._probe(self._sys(nvidia_driver="nvidia", card_driver="nvidia",
                                    modules=("nvidia", "nvidia_drm")))
        self.assertIn("diagnosis: \n", out, out)
        self.assertIn("card0 nvidia", out, out)

    def test_an_amd_machine_with_no_nvidia_card_says_nothing_about_nvidia(self):
        out = self._probe(self._sys(nvidia_driver=None, card_driver="amdgpu", modules=("amdgpu",)))
        self.assertIn("diagnosis: \n", out, out)
        self.assertNotIn("nvidia card:", out, out)

    def test_the_cards_audio_function_is_not_counted_as_an_unclaimed_gpu(self):
        """Every modern NVIDIA card carries an HDMI audio function at .1 with vendor 0x10de and no
        driver bound. Counted, it reports a driver problem on a perfectly healthy machine."""
        out = self._probe(self._sys(nvidia_driver="nvidia", card_driver="nvidia",
                                    modules=("nvidia",)))
        self.assertIn("diagnosis: \n", out, out)
        self.assertNotIn("0000:01:00.1", out, out)


class SessionFallback(unittest.TestCase):
    """The session must reach a desktop where one is still reachable, and a console only where it
    is not — running the shipped script, not reading it."""

    def _run(self, *, cards, nvidia=None, modprobe_works=False, wayfire_status=0, cmdline=""):
        work = Path(tempfile.mkdtemp(prefix="pc-session-gpu-"))
        binned = work / "bin"
        binned.mkdir()
        calls = work / "calls"
        (binned / "bash").write_text(f'#!/bin/sh\necho rescue >>"{calls}"\nexit 0\n')
        # Every launch records the renderer it was given, so "did it retry in software" is a fact
        # about the attempt and not about the script's text.
        (binned / "wayfire").write_text(
            '#!/bin/sh\n[ "$1" = --version ] && exit 0\n'
            f'echo "wayfire sw=${{LIBGL_ALWAYS_SOFTWARE:-0}}" >>"{calls}"\n'
            f"exit {wayfire_status}\n")
        (binned / "pc-wayfire-health").write_text("#!/bin/sh\nexit 0\n")
        (binned / "modprobe").write_text(
            f'#!/bin/sh\necho "modprobe $*" >>"{calls}"\nexit {0 if modprobe_works else 1}\n')
        # sudo must exist and must not be a way to smuggle a real modprobe into a test run.
        (binned / "sudo").write_text(f'#!/bin/sh\necho "sudo $*" >>"{calls}"\nexit 1\n')
        for child in binned.iterdir():
            child.chmod(0o755)
        (work / "drivers").mkdir()
        (work / "drm").mkdir()
        (work / "pci").mkdir()
        if cards:
            dev = work / "drm/card0/device"
            dev.mkdir(parents=True)
            (work / "drivers" / cards).mkdir(exist_ok=True)
            (dev / "driver").symlink_to(work / "drivers" / cards)
        if nvidia is not None:
            slot = work / "pci/0000:01:00.0"
            slot.mkdir(parents=True)
            (slot / "vendor").write_text("0x10de\n")
            (slot / "class").write_text("0x030000\n")
            (slot / "device").write_text("0x1cbb\n")
        (work / "modules").write_text("")
        (work / "cmdline").write_text(cmdline + "\n")
        home = work / "home"
        (home / ".config").mkdir(parents=True)
        (work / "run").mkdir()
        env = dict(os.environ)
        env.update(
            HOME=str(home), XDG_STATE_HOME=str(work / "state"),
            XDG_CONFIG_HOME=str(home / ".config"), XDG_RUNTIME_DIR=str(work / "run"),
            PC_RESCUE_SHELL=str(binned / "bash"),
            PC_WAYFIRE_HEALTH=str(binned / "pc-wayfire-health"),
            PC_DRM_ROOT=str(work / "drm"), PC_PCI_ROOT=str(work / "pci"),
            PC_PROC_MODULES=str(work / "modules"), PC_PROC_CMDLINE=str(work / "cmdline"),
            PATH=str(binned) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        )
        env.pop("PC_SOFTWARE_RENDER", None)
        proc = subprocess.run(["/bin/sh", str(SESSION)], env=env, capture_output=True,
                              text=True, timeout=180)
        recorded = calls.read_text().splitlines() if calls.exists() else []
        return proc.stdout + proc.stderr, recorded

    def test_a_failed_attempt_is_retried_once_in_software_before_a_console(self):
        """A card whose GL stack will not come up is not a machine without a desktop; llvmpipe over
        the same DRM device draws. The old session answered the first failure with a text console."""
        out, calls = self._run(cards="nouveau", wayfire_status=3)
        launches = [c for c in calls if c.startswith("wayfire ")]
        self.assertEqual(launches, ["wayfire sw=0", "wayfire sw=1"],
                         "the session must try once on the GPU and once in software:\n"
                         + "\n".join(calls))
        self.assertEqual(calls[-1], "rescue")
        self.assertIn("software rendering", out, out)

    def test_the_retry_happens_once_and_not_for_ever(self):
        out, calls = self._run(cards="nouveau", wayfire_status=3)
        self.assertEqual(len([c for c in calls if c.startswith("wayfire ")]), 2, calls)

    def test_a_session_the_script_stopped_is_not_retried(self):
        """Status 0 is this script stopping Wayfire; retrying it would double every clean logout."""
        out, calls = self._run(cards="i915", wayfire_status=0)
        self.assertEqual(len([c for c in calls if c.startswith("wayfire ")]), 1, calls)

    def test_no_drm_device_is_refused_by_name_before_wayfire_is_started(self):
        """With CONFIG_DRM_SIMPLEDRM unset there is no EFI-framebuffer device standing in for a GPU
        that did not come up, so `Found 0 GPUs` -> SIGSEGV -> `status 255` was the whole report."""
        out, calls = self._run(cards=None, nvidia="", modprobe_works=False)
        self.assertNotIn("wayfire sw=", "\n".join(calls),
                         "a reason known before the attempt must never be reported as an exit "
                         "status:\n" + "\n".join(calls))
        self.assertIn("no display driver claimed the GPU", out, out)
        self.assertNotIn("status 255", out, out)

    def test_the_last_resort_is_asked_for_by_name_because_the_blacklist_is_only_advice(self):
        """`blacklist nouveau` refuses an ALIAS; `modprobe nouveau` by name still loads it. That
        asymmetry is the entire fallback, so it has to be exercised rather than assumed."""
        out, calls = self._run(cards=None, nvidia="", modprobe_works=False)
        self.assertIn("modprobe nouveau", calls,
                      "nothing tried the open driver before giving up:\n" + "\n".join(calls))

    def test_a_machine_with_no_nvidia_card_is_not_handed_a_privileged_guess(self):
        """Reaching for nouveau on a box that has no NVIDIA card cannot produce a screen."""
        out, calls = self._run(cards=None, nvidia=None, modprobe_works=False)
        self.assertNotIn("modprobe nouveau", calls, "\n".join(calls))
        self.assertIn("no DRM device", out, out)

    def test_the_boot_menu_entry_for_nouveau_is_honoured(self):
        """`pc.gpu=nouveau` is what the open-source-driver entry passes; the session is what turns
        it into the explicit modprobe the packaged blacklist cannot refuse."""
        out, calls = self._run(cards="nouveau", modprobe_works=True, wayfire_status=0,
                               cmdline="root=live:CDLABEL=PCLIVE pc.gpu=nouveau")
        self.assertIn("modprobe nouveau", calls, "\n".join(calls))

    def test_the_software_boot_menu_entry_starts_in_software(self):
        out, calls = self._run(cards="i915", wayfire_status=0,
                               cmdline="root=live:CDLABEL=PCLIVE pc.gpu=software")
        self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=1"],
                         "\n".join(calls))

    def test_the_packaged_copy_has_not_diverged(self):
        self.assertEqual(SESSION.read_text(), PACKAGED.read_text(),
                         "os/bin/pc-compositor-session and the packaged copy have diverged")


if __name__ == "__main__":
    unittest.main()
