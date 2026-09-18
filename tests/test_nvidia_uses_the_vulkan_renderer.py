"""ON NVIDIA, THE SESSION MUST PICK THE VULKAN RENDERER, NOT GLES2.

Measured for hours on a Quadro P1000 driving a 55" TV over DisplayPort (2026-09-18). wlroots defaults
to its GLES2 renderer, and on the NVIDIA proprietary driver GLES2 has no implicit buffer sync, so the
compositor scans out frames the GPU has not finished. The composited frame is PERFECT — screenshots,
the DOM and the compositor logs are all clean — but it reaches the panel with mouse trails, cursor
distortion and whole regions dropping out. Every GLES2-path lever was tried live and either did
nothing or made it worse:

    WLR_NO_HARDWARE_CURSORS  -> no change (then software-cursor trails)
    WLR_DRM_NO_ATOMIC        -> drmModePageFlip failed: Device or resource busy
    WLR_DRM_NO_MODIFIERS     -> Swapchain for output 'DP-5' failed test  (black screen)
    forcing power state off P8 -> no change

The one fix that stopped it while keeping GPU acceleration (so Steam still runs) is the Vulkan
renderer, which does explicit sync — the same thing KDE and Hyprland use on NVIDIA. pixman stops the
flicker too but composites on the CPU and cannot run a game, so it is the wrong default.

The rule is deliberately narrow, and each case below pins one edge: only with an NVIDIA card, only
when nothing already chose software, only when the Vulkan ICD and lib are installed, never over an
operator's own WLR_RENDERER, and never on Intel/AMD (whose GLES2 path is correct and flicker-free).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = (ROOT / "os/bin/pc-compositor-session").read_text(encoding="utf-8")
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"


def _slice() -> str:
    """The Vulkan-selection block, run in isolation with pc_drm_cards / pc_say stubbed."""
    start = SESSION.index("\tif [ -z \"${WLR_RENDERER:-}\" ]; then\n\t\tpc_has_nvidia=''")
    end = SESSION.index("\t# ---- ON A HYBRID LAPTOP", start)
    return SESSION[start:end]


class VulkanSelection(unittest.TestCase):
    def decide(self, cards, *, icd=True, libvulkan=True, preset_renderer=None):
        """Run the shipped block. `cards` is a list of (cardN, driver). Returns WLR_RENDERER after."""
        with tempfile.TemporaryDirectory() as tmp:
            icd_path = Path(tmp, "nvidia_icd.json")
            if icd:
                icd_path.write_text("{}")
            libdir = Path(tmp, "lib"); libdir.mkdir()
            if libvulkan:
                (libdir / "libvulkan.so").write_text("")
            cards_lines = " ".join("'%s %s'" % (c, d) for c, d in cards) or "''"
            pre = ("export WLR_RENDERER=%s\n" % preset_renderer) if preset_renderer else ""
            ldconfig_stub = ("ldconfig(){ printf '%s\\n' '\tlibvulkan.so.1 => /x/libvulkan.so.1'; }\n"
                             if libvulkan else "ldconfig(){ :; }\n")
            icd_arg = str(icd_path if icd else Path(tmp, "missing.json"))
            script = (
                "set -u\n"
                + pre
                + "pc_say(){ :; }\n"
                + "pc_drm_cards(){ printf '%s\\n' " + cards_lines + "; }\n"
                # exercise the ldconfig branch for the libvulkan probe (can't drop files in /usr here)
                + ldconfig_stub
                + ('export PC_VULKAN_ICD="' + icd_arg + '"\n')
                # neutralise the two absolute lib paths the block also accepts, so the ldconfig
                # branch is what decides in this harness
                + _slice().replace("[ -e /usr/lib64/libvulkan.so ]", "[ -e /nonexistent/a ]")
                          .replace("[ -e /usr/lib/libvulkan.so ]", "[ -e /nonexistent/b ]")
                + '\necho "RENDERER=${WLR_RENDERER:-unset}"\n'
            )
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                               env={**os.environ})
            self.assertEqual("", r.stderr.strip(), r.stderr)
            for line in r.stdout.splitlines():
                if line.startswith("RENDERER="):
                    return line.split("=", 1)[1]
            self.fail(r.stdout)

    def test_nvidia_gets_vulkan(self):
        """THE REPORTED MACHINE: Intel iGPU + NVIDIA dGPU, screen on the NVIDIA."""
        self.assertEqual("vulkan", self.decide([("card0", "i915"), ("card1", "nvidia")]))

    def test_nvidia_drm_spelling_also_counts(self):
        self.assertEqual("vulkan", self.decide([("card0", "nvidia-drm")]))

    def test_intel_only_keeps_gles2(self):
        """Intel's GLES2 path is correct and flicker-free — do not touch it."""
        self.assertEqual("unset", self.decide([("card0", "i915")]))

    def test_amd_only_keeps_gles2(self):
        self.assertEqual("unset", self.decide([("card0", "amdgpu")]))

    def test_an_operator_choice_is_never_overridden(self):
        self.assertEqual("pixman", self.decide([("card0", "nvidia")], preset_renderer="pixman"))

    def test_no_vulkan_icd_means_no_vulkan(self):
        """A machine whose NVIDIA userspace lacks Vulkan must not be sent to a renderer it can't run."""
        self.assertEqual("unset", self.decide([("card0", "nvidia")], icd=False))

    def test_no_libvulkan_means_no_vulkan(self):
        self.assertEqual("unset", self.decide([("card0", "nvidia")], libvulkan=False))


class ItIsWiredInTheRightPlace(unittest.TestCase):
    def test_it_runs_after_the_software_fallbacks(self):
        """Vulkan must not override a software renderer chosen for a broken NVIDIA userspace."""
        pixman = SESSION.index("PC_PIXMAN_RENDER=1")
        vulkan = SESSION.index('export WLR_RENDERER=vulkan')
        self.assertLess(pixman, vulkan,
                        "the Vulkan block runs before the pixman fallback; a mismatched NVIDIA "
                        "userspace would be sent to Vulkan instead of the safe software path")

    def test_it_only_acts_when_renderer_is_unset(self):
        block = _slice()
        self.assertTrue(block.startswith('\tif [ -z "${WLR_RENDERER:-}" ]'),
                        "the Vulkan block does not first check that no renderer was already chosen")

    def test_the_packaged_copy_matches(self):
        self.assertTrue(PACKAGED.exists())
        self.assertEqual(SESSION, PACKAGED.read_text(encoding="utf-8"),
                         "the ISO ships the packaged copy; a fix only in os/bin never boots")


if __name__ == "__main__":
    unittest.main()
