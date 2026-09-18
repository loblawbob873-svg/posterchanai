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

# EVERY RENDERER A HEALTHY IMAGE CARRIES, as the session reads them: the Mesa driver module per card
# family plus the proprietary pair. Handed to the script as PC_GL_ROOT so the decision below is about
# the SHAPE of an image and not about whatever happens to be installed on the machine running the
# tests -- which has no NVIDIA userspace, and would otherwise make every NVIDIA case "unrenderable".
FULL_GL = ("dri/radeonsi_dri.so", "dri/iris_dri.so", "dri/crocus_dri.so", "dri/r600_dri.so",
           "dri/nouveau_dri.so", "dri/virtio_gpu_dri.so", "dri/swrast_dri.so",
           "libEGL_nvidia.so.0", "gbm/nvidia-drm_gbm.so")


def make_gl_root(work, files):
    """A fabricated /usr/lib64 holding exactly the renderers named."""
    root = Path(work) / "gl"
    for relative in files:
        target = root / "usr/lib64" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    (root / "usr/lib64").mkdir(parents=True, exist_ok=True)
    return root
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
        self.assertIn("PC_LIVE_GPU_CMDLINE='module_blacklist=nouveau nvidia_drm.modeset=1'", GENTOO,
                      "module_blacklist= is the kernel's own parameter and the only one that binds")

    def test_every_ordinary_live_entry_turns_nvidia_modesetting_on(self):
        """`modeset` DEFAULTS TO 0 on nvidia-drm — measured with modinfo on the shipped 580.173.02
        module: `1 = enable, 0 = disable (default)`. The only thing that turns it on is
        /etc/modprobe.d/nvidia.conf, which belongs to x11-drivers/nvidia-drivers; an image built
        without that package gets a DRM device with no CRTCs from udev, which reads as a healthy GPU
        to every check there is. A module parameter on the KERNEL line needs no file in the image."""
        for title, body in MENUENTRIES:
            if "open-source NVIDIA" in title:
                self.assertNotIn("nvidia_drm.modeset", body,
                                 "the entry that blacklists the nvidia modules must not configure "
                                 "them")
                continue
            self.assertIn("nvidia_drm.modeset=1", body,
                          f'"{title}" leaves NVIDIA modesetting at its disabled default:\n{body}')

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
        black screen by construction. The two facts belong together.

        THIS TEST USED TO BE THE WHOLE GUARANTEE AND IT WAS A STRING SEARCH, which is why it passed
        for every day the shipped ISO was unbootable on NVIDIA: the package was named in the source
        and absent from the image. Naming the package is now the first of three demands — the build
        host must actually have it, the modules must be built for the kernel the ISO ships, and the
        packed output must carry the userspace (ImagePayload below).
        """
        self.assertIn("x11-drivers/nvidia-drivers", GENTOO,
                      "nouveau is refused but nvidia-drivers is no longer installed")
        self.assertIn("portageq has_version / x11-drivers/nvidia-drivers", GENTOO,
                      "nothing checks whether the BUILD HOST has the driver it is about to not "
                      "ship; `gentoo.sh livecd` images the build machine, and that machine is AMD")
        self.assertIn("video/nvidia-drm.ko", GENTOO,
                      "a package database entry is not a loadable module: the modules have to exist "
                      "for the kernel this ISO will boot")
        self.assertIn("liveGpuPayloadProblems", GENTOO,
                      "the packed image is never asked whether the driver reached it")


KERNEL = "6.18.43-gentoo-dist-bin"

# EVERY GRAPHICS PATH IN THE IMAGE THAT SHIPPED ON 2026-09-18, read out of its own squashfs listing
# and written down here because it is the only description of the failure that cannot rot. Note what
# IS present: five proprietary .ko files for the running kernel, and Mesa built for the build host's
# hardware. Note what is not.
AS_SHIPPED = [
    f"usr/lib/modules/{KERNEL}/video/nvidia.ko",
    f"usr/lib/modules/{KERNEL}/video/nvidia-drm.ko",
    f"usr/lib/modules/{KERNEL}/video/nvidia-modeset.ko",
    f"usr/lib/modules/{KERNEL}/video/nvidia-uvm.ko",
    f"usr/lib/modules/{KERNEL}/kernel/drivers/gpu/drm/nouveau/nouveau.ko",
    "usr/lib64/dri/crocus_dri.so", "usr/lib64/dri/iris_dri.so", "usr/lib64/dri/r300_dri.so",
    "usr/lib64/dri/r600_dri.so", "usr/lib64/dri/radeonsi_dri.so",
    "usr/lib64/dri/virtio_gpu_dri.so", "usr/lib64/dri/swrast_dri.so",
    "usr/lib64/dri/kms_swrast_dri.so", "usr/lib64/libEGL_mesa.so.0", "usr/lib64/gbm/dri_gbm.so",
    "usr/share/glvnd/egl_vendor.d/50_mesa.json", "etc/modprobe.d/ppp.conf",
]
# ...and what a complete one adds. Every one of these was measured absent from the failing image.
THE_MISSING_HALF = [
    "usr/lib64/libEGL_nvidia.so.0", "usr/lib64/gbm/nvidia-drm_gbm.so",
    "usr/share/glvnd/egl_vendor.d/10_nvidia.json", "etc/modprobe.d/nvidia.conf",
    "usr/bin/nvidia-smi", "usr/lib64/dri/nouveau_dri.so",
]
ORDINARY_CMDLINE = ("linux /boot/vmlinuz root=live:CDLABEL=PCLIVE rd.live.image "
                    "module_blacklist=nouveau console=tty0")
NOUVEAU_CMDLINE = ("linux /boot/vmlinuz root=live:CDLABEL=PCLIVE rd.live.image "
                   "module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm pc.gpu=nouveau")


class ImagePayload(unittest.TestCase):
    """Does the PACKED IMAGE carry a renderer for every card its boot menu offers one for?

    RUNS THE SHIPPED GATE, not a copy of its idea: `liveGpuPayloadProblems` is lifted out of
    os/gentoo.sh and fed a listing. The reason this is a separate gate from everything else in
    liveCD() is that every other gate PASSED on the image that could not boot — they ask portageq
    about the build host, and portageq was not what went wrong.
    """

    @staticmethod
    def _gate(paths, cmdlines, kernel=KERNEL, nvver=""):
        body = re.search(r"^liveGpuPayloadProblems\(\) \{.*?^\}$", GENTOO, re.S | re.M)
        assert body, "liveGpuPayloadProblems is not in os/gentoo.sh"
        listing = "".join("squashfs-root/%s\n" % p for p in paths)
        proc = subprocess.run(
            ["/bin/bash", "-c", body[0] + '\nliveGpuPayloadProblems "$1" "$2" "$3"', "_",
             kernel, cmdlines, nvver],
            input=listing, capture_output=True, text=True, timeout=60)
        self_err = proc.stderr.strip()
        assert not self_err, self_err
        return proc.stdout.strip()

    def test_the_image_that_shipped_would_have_been_refused(self):
        """The whole point. This listing is the real one, and it passed every gate the build had."""
        problems = self._gate(AS_SHIPPED, ORDINARY_CMDLINE + " " + NOUVEAU_CMDLINE)
        for wanted in ("libEGL_nvidia", "nvidia-drm_gbm.so", "10_nvidia.json",
                       "/etc/modprobe.d/nvidia.conf", "nvidia-smi", "nouveau_dri.so"):
            self.assertIn(wanted, problems,
                          f"the gate did not notice {wanted} was missing: {problems!r}")

    def test_a_complete_image_is_not_refused(self):
        """A gate that fires on a good image is a gate that gets switched off."""
        self.assertEqual(self._gate(AS_SHIPPED + THE_MISSING_HALF,
                                    ORDINARY_CMDLINE + " " + NOUVEAU_CMDLINE), "")

    def test_each_demand_is_tied_to_the_entry_that_creates_it(self):
        """Refusing nouveau in the kernel is what makes the proprietary userspace mandatory, and
        offering a nouveau entry is what makes Mesa's nouveau driver mandatory. An image whose menu
        does neither must not be held to either — the rule is a pair, not a wish list."""
        complete = AS_SHIPPED + THE_MISSING_HALF
        without_nvidia = [p for p in complete if "nvidia" not in p]
        without_nouveau_gl = [p for p in complete if "nouveau_dri" not in p]
        plain = ("linux /boot/vmlinuz root=live:CDLABEL=PCLIVE rd.live.image console=tty0")
        self.assertEqual(self._gate(without_nvidia, plain), "",
                         "an image that does not blacklist nouveau was asked for NVIDIA userspace")
        self.assertEqual(self._gate(without_nouveau_gl, plain), "",
                         "an image with no nouveau entry was asked for Mesa's nouveau driver")
        self.assertIn("libEGL_nvidia", self._gate(without_nvidia, ORDINARY_CMDLINE))
        self.assertIn("nouveau_dri.so", self._gate(without_nouveau_gl, NOUVEAU_CMDLINE))

    def test_modules_for_a_kernel_the_image_does_not_boot_are_not_a_driver(self):
        """The other half of the same packaging accident, and the one a `var/db/pkg` check would miss
        entirely: a host that has just taken a dist-kernel update has the package installed and
        modules only for the kernel it has not booted yet."""
        problems = self._gate(AS_SHIPPED + THE_MISSING_HALF, ORDINARY_CMDLINE,
                              kernel="6.18.48-gentoo-dist-bin")
        self.assertIn("nvidia.ko", problems, problems)
        self.assertIn("6.18.48", problems, "the message must name the kernel it looked for")

    def test_the_software_path_is_required_of_every_image(self):
        """llvmpipe is what `pc.gpu=software` and the session's own retry mean. An image without
        swrast has no fallback for any card at all."""
        self.assertIn("swrast_dri.so",
                      self._gate([p for p in AS_SHIPPED + THE_MISSING_HALF
                                  if "swrast_dri" not in p], ORDINARY_CMDLINE))

    def test_userspace_of_a_different_release_than_the_module_is_refused(self):
        """MEASURED ON THE MACHINE THAT COULD NOT START: nvidia.ko 580.173.02 beside libnvidia-ml
        580.178.04. Both halves present, both looking right, and the driver refuses to work — what a
        person sees is six EGL/Vulkan errors and `Failed to create renderer`, none of which mentions
        a version. The build host ran 6.18.43 while portage had built the modules for 6.18.48."""
        complete = AS_SHIPPED + THE_MISSING_HALF + ["usr/lib64/libnvidia-ml.so.580.178.04"]
        self.assertIn("libnvidia-ml.so.580.173.02",
                      self._gate(complete, ORDINARY_CMDLINE, nvver="580.173.02"),
                      "an image whose NVIDIA halves are different releases was accepted")
        self.assertEqual(self._gate(complete, ORDINARY_CMDLINE, nvver="580.178.04"), "",
                         "a matched pair must not be refused")

    def test_it_can_be_run_against_a_real_listing(self):
        """`PC_LIVECD_LISTING=<unsquashfs -l output> pytest …` answers the question about an actual
        ISO. Skipped, not passed, when nobody supplied one."""
        source = os.environ.get("PC_LIVECD_LISTING", "")
        if not source or not Path(source).is_file():
            self.skipTest("set PC_LIVECD_LISTING to an `unsquashfs -l` listing to check a real ISO")
        paths = [l.strip().split("squashfs-root/", 1)[-1]
                 for l in Path(source).read_text(errors="replace").splitlines()
                 if "squashfs-root/" in l]
        kernel = os.environ.get("PC_LIVECD_KERNEL", KERNEL)
        problems = self._gate(paths, ORDINARY_CMDLINE + " " + NOUVEAU_CMDLINE, kernel=kernel)
        self.assertEqual(problems, "", "that image is missing: " + problems)


class MesaDrivers(unittest.TestCase):
    """ONE IMAGE BOOTS EVERY MACHINE, so the driver list is not allowed to be the build host's."""

    def test_the_driver_list_covers_the_cards_the_boot_menu_offers(self):
        cards = re.search(r'^VIDEO_CARDS="([^"]*)"', GENTOO, re.M)
        self.assertTrue(cards, "VIDEO_CARDS is not in os/gentoo.sh")
        listed = cards[1].split()
        self.assertIn("nouveau", listed,
                      "the boot menu has an 'open-source NVIDIA driver' entry and Mesa is built "
                      "without the driver it needs, so that entry is a blank screen")
        self.assertIn("virgl", listed, "every VM gate in this repository depends on it")

    def test_the_drivers_for_hardware_this_host_lacks_are_reconciled_on_every_build(self):
        """A stored VIDEO_CARDS from an older install never gains a new entry on its own, which is
        the fossil the virgl line already had to work around."""
        required = re.search(r'^PC_MESA_REQUIRED_CARDS="([^"]*)"', GENTOO, re.M)
        self.assertTrue(required, "PC_MESA_REQUIRED_CARDS is not in os/gentoo.sh")
        self.assertEqual(sorted(required[1].split()), ["nouveau", "virgl"])
        self.assertIn("media-libs/mesa[video_cards_$WANT_CARD]", GENTOO,
                      "nothing checks the installed Mesa for those drivers")


class GpuDiagnosis(unittest.TestCase):
    """The decision, driven over a fabricated /sys.

    There is no NVIDIA card on any machine that runs these tests and QEMU cannot emulate one, so the
    only way to check what the screen SAYS is to hand the script the shapes it will meet.
    """

    def _sys(self, *, nvidia_driver=None, card_driver=None, modules=(), gl=FULL_GL):
        work = Path(tempfile.mkdtemp(prefix="pc-gpu-"))
        make_gl_root(work, gl)
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
                   PC_PROC_CMDLINE=str(work / "cmdline"),
                   PC_GL_ROOT=str(work / "gl"))
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

    def _run(self, *, cards, nvidia=None, modprobe_works=False, wayfire_status=0, cmdline="",
             gl=FULL_GL, wayfire_mode="exit", card_vendor=None, extra_env=None,
             nvidia_module_version=None, connectors=None):
        work = Path(tempfile.mkdtemp(prefix="pc-session-gpu-"))
        make_gl_root(work, gl)
        binned = work / "bin"
        binned.mkdir()
        calls = work / "calls"
        (binned / "bash").write_text(f'#!/bin/sh\necho rescue >>"{calls}"\nexit 0\n')
        # Every launch records the renderer it was given, so "did it retry in software" is a fact
        # about the attempt and not about the script's text.
        # THREE COMPOSITORS, BECAUSE THE SESSION HAS TO TELL THEM APART.
        #
        #   exit        it refuses to start at all and says so with a status.
        #   linger      it STARTS, stays up, never writes a ready file, and exits CLEANLY when the
        #               session stops it -- what wlroots does on a SIGTERM it handles, and the shape
        #               of "the desktop came up and painted nothing". The bug was that this outcome
        #               took the `status == 0` branch, which means "the user logged out".
        #   ignore-term it is wedged: SIGTERM does nothing. The session must escalate and must not
        #               block in `wait` on it, which is the one line that could hang for ever.
        body = {
            "exit": f"exit {wayfire_status}\n",
            "linger": "trap 'exit 0' TERM\nsleep 30 &\nwait\nexit 0\n",
            "ignore-term": "trap '' TERM\nsleep 30 &\nwait\nexit 0\n",
        }[wayfire_mode]
        (binned / "wayfire").write_text(
            '#!/bin/sh\n[ "$1" = --version ] && exit 0\n'
            f'echo "wayfire sw=${{LIBGL_ALWAYS_SOFTWARE:-0}}'
            f'${{WLR_DRM_NO_ATOMIC:+ noatomic}}${{WLR_RENDERER:+ renderer=$WLR_RENDERER}}'
            f'${{WLR_DRM_DEVICES:+ devices=$WLR_DRM_DEVICES}}" >>"{calls}"\n' + body)
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
            if card_vendor:
                (dev / "vendor").write_text(card_vendor + "\n")
        # A hybrid machine is two cards with the screen on one of them, and which one is a fact under
        # every connector: `connectors={"card0": {...}, "card1": {"DP-5": "connected"}}`.
        for card, outputs in (connectors or {}).items():
            holder = work / "drm" / card / "device"
            holder.mkdir(parents=True, exist_ok=True)
            if not (holder / "driver").exists():
                (work / "drivers" / (cards or "i915")).mkdir(exist_ok=True)
                (holder / "driver").symlink_to(work / "drivers" / (cards or "i915"))
            for name, status in outputs.items():
                node = work / "drm" / (card + "-" + name)
                node.mkdir(parents=True, exist_ok=True)
                (node / "status").write_text(status + "\n")
        if nvidia_module_version is not None:
            (work / "nvidia-version").write_text(
                "NVRM version: NVIDIA UNIX x86_64 Kernel Module  %s  Tue Jun 23 08:38:17 UTC 2026\n"
                "GCC version:  gcc version 15.3.0 (Gentoo 15.3.0 p8)\n" % nvidia_module_version)
        if nvidia is not None:
            slot = work / "pci/0000:01:00.0"
            slot.mkdir(parents=True)
            (slot / "vendor").write_text("0x10de\n")
            (slot / "class").write_text("0x030000\n")
            (slot / "device").write_text("0x1cbb\n")
            # WHICH DRIVER HOLDS IT, which this harness never recorded — so "the NVIDIA driver has
            # the card and there is still no screen" could not be distinguished from "nothing has it"
            # in any test, which is exactly the two cases the rescue wording has to keep apart.
            if nvidia:
                (work / "drivers" / nvidia).mkdir(exist_ok=True)
                (slot / "driver").symlink_to(work / "drivers" / nvidia)
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
            PC_GL_ROOT=str(work / "gl"),
            # The launcher is never started here, so waiting out the real five-minute cap would make
            # every run of this class a minutes-long test. The rule being checked is which branch is
            # taken, not how patient it is.
            PC_READY_WAIT_TENTHS="5", PC_STOP_GRACE_TENTHS="5", PC_READY_TICK_TENTHS="2",
            PC_NVIDIA_PROC_VERSION=str(work / "nvidia-version"),
            PC_DRI_ROOT=str(work / "dri"),
            PATH=str(binned) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        )
        env.update(extra_env or {})
        env.pop("PC_SOFTWARE_RENDER", None)
        env.pop("PC_SOFTWARE_REASON", None)
        proc = subprocess.run(["/bin/sh", str(SESSION)], env=env, capture_output=True,
                              text=True, timeout=180)
        recorded = calls.read_text().splitlines() if calls.exists() else []
        return proc.stdout + proc.stderr, recorded

    def test_a_failed_attempt_is_retried_once_in_software_before_a_console(self):
        """A card whose GL stack will not come up is not a machine without a desktop; llvmpipe over
        the same DRM device draws. The old session answered the first failure with a text console."""
        out, calls = self._run(cards="nouveau", wayfire_status=3)
        launches = [c for c in calls if c.startswith("wayfire ")]
        self.assertEqual(launches,
                         ["wayfire sw=0", "wayfire sw=1", "wayfire sw=1 renderer=pixman"],
                         "the session must try the card, then llvmpipe, then pixman:\n"
                         + "\n".join(calls))
        self.assertEqual(calls[-1], "rescue")
        self.assertIn("software rendering", out, out)

    def test_the_ladder_has_a_bottom(self):
        """Three attempts, each giving up something the last one needed, and then a console. A
        fallback chain with no end is a login loop nobody can type into."""
        out, calls = self._run(cards="nouveau", wayfire_status=3)
        self.assertEqual(len([c for c in calls if c.startswith("wayfire ")]), 3, calls)
        self.assertEqual(calls[-1], "rescue", calls)

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

    def test_a_card_with_no_renderer_in_this_image_starts_in_software(self):
        """0x1af4 WAS THE ONLY VENDOR THE GUARD NAMED, so every other unrenderable card got an
        accelerated attempt that painted nothing.

        Measured on the 2026-09-18 image: nouveau bound the card and `dri/` had no nouveau_dri.so.
        The compositor came up and the desktop was empty, which is worse than slow.
        """
        gl = [f for f in FULL_GL if "nouveau" not in f]
        out, calls = self._run(cards="nouveau", gl=gl, wayfire_status=0)
        self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=1"],
                         "the first attempt must already be in software:\n" + "\n".join(calls))
        self.assertIn("no renderer for nouveau", out, out)

    def test_an_nvidia_card_with_no_nvidia_userspace_starts_in_software(self):
        """The failing image had the five .ko files and none of the userspace: no libEGL_nvidia, no
        gbm/nvidia-drm_gbm.so. wlroots allocates through one and Chromium renders through the other,
        so a kernel driver on its own is a screen nothing can paint on."""
        gl = [f for f in FULL_GL if "nvidia" not in f]
        out, calls = self._run(cards="nvidia", nvidia="nvidia", gl=gl, wayfire_status=0)
        self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=1"],
                         "\n".join(calls))
        self.assertIn("no renderer for nvidia", out, out)

    def test_half_the_nvidia_userspace_is_not_a_working_card(self):
        """Both halves come from the same package and neither is Mesa's; one without the other is
        the same blank screen, so the pair is required together."""
        for kept in ("libEGL_nvidia.so.0", "gbm/nvidia-drm_gbm.so"):
            gl = [f for f in FULL_GL if "nvidia" not in f] + [kept]
            out, calls = self._run(cards="nvidia", nvidia="nvidia", gl=gl, wayfire_status=0)
            self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=1"],
                             f"with only {kept}:\n" + "\n".join(calls))

    def test_a_working_card_is_not_demoted_to_llvmpipe(self):
        """The cost of this guard being wrong in the other direction is every AMD and Intel machine
        running its desktop on the CPU, which is why it is a named table and not a probe of hope."""
        for driver, module in (("amdgpu", "dri/radeonsi_dri.so"), ("i915", "dri/iris_dri.so"),
                               ("nouveau", "dri/nouveau_dri.so")):
            out, calls = self._run(cards=driver, wayfire_status=0)
            self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=0"],
                             f"{driver} has {module} in this image and was demoted anyway:\n"
                             + "\n".join(calls))

    def test_an_unrecognised_driver_is_left_accelerated(self):
        """IT FAILS OPEN. A driver this table has never heard of may be perfectly able to render,
        and the one-shot retry below is what covers it if it cannot — demoting on a guess would make
        every future GPU slow for no measured reason."""
        out, calls = self._run(cards="somefuturegpu", wayfire_status=0)
        self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=0"],
                         "\n".join(calls))

    def test_the_virtio_rule_keeps_its_own_measured_extra(self):
        """WLR_DRM_NO_ATOMIC is a virtio scanout fact, not a software-rendering fact. Generalising it
        to every software attempt would change modesetting on real hardware for no reason."""
        out, calls = self._run(cards="virtio_gpu", card_vendor="0x1af4", wayfire_status=0)
        self.assertEqual([c for c in calls if c.startswith("wayfire ")],
                         ["wayfire sw=1 noatomic"], "\n".join(calls))
        out, calls = self._run(cards="nouveau", gl=[f for f in FULL_GL if "nouveau" not in f],
                               wayfire_status=0)
        self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=1"],
                         "a physical card took the virtio-only modesetting workaround:\n"
                         + "\n".join(calls))

    def test_the_virtio_rule_still_reads_the_root_the_rest_of_the_script_reads(self):
        """It grepped /sys/class/drm literally, so no test could ever drive it and the branch that a
        real machine takes had never been exercised here at all."""
        self.assertNotIn("/sys/class/drm/card*/device/vendor", SESSION.read_text(),
                         "the virtio check names /sys directly again, which no test can reach")

    def test_a_desktop_that_painted_nothing_is_retried_in_software(self):
        """THE ONE CASE THE RETRY WAS WRITTEN FOR AND DID NOT COVER.

        An unrenderable card makes wlroots fall back to pixman: the compositor comes up, the launcher
        maps a window, the health gate photographs it, finds no painted marker and gives up — and
        this script then stops Wayfire, which exits CLEANLY from the signal, i.e. status 0. Status 0
        means "the user logged out", so the empty desktop was answered with a text console instead of
        the llvmpipe attempt that draws.
        """
        out, calls = self._run(cards="somefuturegpu", wayfire_mode="linger")
        launches = [c for c in calls if c.startswith("wayfire ")]
        self.assertEqual(launches[:2], ["wayfire sw=0", "wayfire sw=1"],
                         "a window that never painted must earn the software retry:\n"
                         + "\n".join(calls))
        self.assertIn("painted nothing", out, out)
        self.assertEqual(calls[-1], "rescue")

    def test_a_wedged_compositor_cannot_stop_the_session_for_ever(self):
        """`wait` CANNOT TIME OUT. A compositor stuck in a driver ioctl does not answer SIGTERM, and
        every path here reached a bare `wait` on it — a login that never says anything again, which
        is exactly the report this was written for. Escalate, then stop waiting."""
        out, calls = self._run(cards="somefuturegpu", wayfire_mode="ignore-term")
        self.assertIn("rescue", calls,
                      "the session never reached its own rescue screen:\n" + "\n".join(calls))
        self.assertIn("did not stop when asked", out, out)

    def test_the_session_says_what_it_is_doing_while_it_does_it(self):
        """A five-minute wait with nothing on the console is indistinguishable from a dead machine,
        and that is how this was reported: "hangs right after the login message"."""
        out, calls = self._run(cards="somefuturegpu", wayfire_mode="linger")
        self.assertIn("PosterChanOS: starting Wayfire on: card0 somefuturegpu", out, out)
        self.assertIn("waiting for the desktop to draw", out,
                      "nothing on the console while the longest wait in the session runs:\n" + out)

    def test_the_rescue_puts_the_console_back_before_it_prints(self):
        """wlroots leaves the VT in KD_GRAPHICS and the keyboard in K_OFF when it dies on a signal,
        so the rescue screen printed into a framebuffer nobody can see, on a keyboard that types
        nowhere. That is the same picture as "empty UI with no mouse movement"."""
        session = SESSION.read_text()
        self.assertIn("pc_restore_console", session)
        rescue_body = session.split("\nrescue() {", 1)[1].split("\n}", 1)[0]
        first = [l.strip() for l in rescue_body.splitlines()
                 if l.strip() and not l.strip().startswith("#")][0]
        self.assertEqual(first, "pc_restore_console",
                         "the console is restored after something has already been printed to it")
        self.assertIn("0x4B45", session, "KDSKBMODE/K_XLATE is what makes the shell typeable")

    def test_the_proprietary_driver_is_asked_for_before_the_open_one(self):
        """NOTHING AUTOLOADS nvidia.ko — it declares no PCI modalias (measured in the shipped
        image's modules.alias), and nvidia-drm, the modesetting half, is never pulled in as anybody's
        dependency. So on the DEFAULT entry, which refuses nouveau in the KERNEL, the only module
        this branch ever reached for was one that entry has made impossible to load."""
        out, calls = self._run(cards=None, nvidia="", modprobe_works=False,
                               cmdline="root=live:CDLABEL=PCLIVE module_blacklist=nouveau")
        self.assertIn("modprobe nvidia-drm modeset=1", calls,
                      "nothing tried the modesetting module before giving up:\n"
                      + "\n".join(calls))
        self.assertLess(calls.index("modprobe nvidia-drm modeset=1"),
                        calls.index("modprobe nouveau"),
                        "the open driver was tried first on an entry that forbids it:\n"
                        + "\n".join(calls))

    def test_the_nouveau_entry_is_not_handed_the_module_it_blacklisted(self):
        """`pc.gpu=nouveau` blacklists the nvidia modules in the kernel; asking for them there is a
        privileged action that cannot succeed."""
        out, calls = self._run(
            cards=None, nvidia="", modprobe_works=False,
            cmdline="root=live:CDLABEL=PCLIVE module_blacklist=nvidia,nvidia_drm,nvidia_modeset,"
                    "nvidia_uvm pc.gpu=nouveau")
        self.assertNotIn("modprobe nvidia-drm modeset=1", calls, "\n".join(calls))

    def test_a_card_the_nvidia_driver_holds_with_no_drm_node_gets_its_own_sentence(self):
        """Reported as the generic "no display driver claimed the GPU", which sends somebody hunting
        a driver that is loaded and working. nvidia.ko registers no DRM device on its own."""
        out, calls = self._run(cards=None, nvidia="nvidia", modprobe_works=False,
                               cmdline="root=live:CDLABEL=PCLIVE module_blacklist=nouveau")
        self.assertIn("nvidia-drm", out, out)
        self.assertIn("modesetting half", out, out)

    def test_two_halves_of_one_nvidia_driver_that_disagree_are_named_and_worked_around(self):
        """THE ACTUAL CAUSE OF THE REPORTED FAILURE, measured on the machine: module 580.173.02,
        userspace 580.178.04. What wayfire.log showed was six EGL and Vulkan errors ending in
        `Failed to create renderer`, and `status 1` was the whole of what the screen said."""
        gl = list(FULL_GL) + ["libnvidia-ml.so.580.178.04"]
        out, calls = self._run(cards="nvidia", nvidia="nvidia", gl=gl,
                               nvidia_module_version="580.173.02", wayfire_status=0,
                               connectors={"card0": {"DP-1": "connected"}})
        self.assertIn("580.173.02", out, out)
        self.assertIn("580.178.04", out, out)
        self.assertIn("renderer=pixman", "\n".join(calls),
                      "llvmpipe was MEASURED not to help here — the broken half is the GBM backend, "
                      "not the GL implementation:\n" + "\n".join(calls))

    def test_matched_nvidia_halves_are_left_alone(self):
        """A guard that fires on a correctly built image would make every NVIDIA machine draw on the
        CPU."""
        gl = list(FULL_GL) + ["libnvidia-ml.so.580.178.04"]
        out, calls = self._run(cards="nvidia", nvidia="nvidia", gl=gl,
                               nvidia_module_version="580.178.04", wayfire_status=0,
                               connectors={"card0": {"DP-1": "connected"}})
        self.assertEqual([c for c in calls if c.startswith("wayfire ")], ["wayfire sw=0"],
                         "\n".join(calls))

    def test_a_console_is_the_third_answer_and_not_the_second(self):
        """The software retry was a second copy of the first failure on that machine: LIBGL_ALWAYS_
        SOFTWARE picks a GL implementation and changes nothing about which GBM backend allocated the
        buffer. pixman uses dumb buffers and neither, so it is the one thing left to try."""
        out, calls = self._run(cards="somefuturegpu", wayfire_status=3)
        launches = [c for c in calls if c.startswith("wayfire ")]
        self.assertEqual(launches,
                         ["wayfire sw=0", "wayfire sw=1", "wayfire sw=1 renderer=pixman"],
                         "\n".join(calls))
        self.assertEqual(calls[-1], "rescue")

    def test_the_compositor_is_started_on_the_card_the_screen_is_plugged_into(self):
        """Measured on the reported hybrid laptop: Intel card0 with six DISCONNECTED connectors, and
        the panel on the NVIDIA card1's DP-5. wlroots picks its primary by boot_vga, which on the
        mirror-image machine is the card with nothing plugged into it."""
        out, calls = self._run(
            cards="i915", wayfire_status=0,
            connectors={"card0": {"DP-1": "disconnected", "HDMI-A-1": "disconnected"},
                        "card1": {"DP-5": "connected"}})
        launch = [c for c in calls if c.startswith("wayfire ")][0]
        self.assertIn("devices=", launch, "\n".join(calls))
        self.assertTrue(launch.split("devices=")[1].split(":")[0].endswith("/card1"),
                        "the compositor was pointed at a card with nothing plugged into it: "
                        + launch)

    def test_a_single_card_machine_is_told_nothing_about_devices(self):
        """Stating a device where there is no choice is a way to be wrong for no benefit."""
        out, calls = self._run(cards="amdgpu", wayfire_status=0,
                               connectors={"card0": {"DP-1": "connected"}})
        self.assertNotIn("devices=", "\n".join(calls), "\n".join(calls))

    def test_every_card_being_in_use_is_left_alone(self):
        """A docked laptop drives one screen from each GPU; naming one would lose the other."""
        out, calls = self._run(cards="i915", wayfire_status=0,
                               connectors={"card0": {"eDP-1": "connected"},
                                           "card1": {"DP-5": "connected"}})
        self.assertNotIn("devices=", "\n".join(calls), "\n".join(calls))

    def test_the_packaged_copy_has_not_diverged(self):
        self.assertEqual(SESSION.read_text(), PACKAGED.read_text(),
                         "os/bin/pc-compositor-session and the packaged copy have diverged")


if __name__ == "__main__":
    unittest.main()
