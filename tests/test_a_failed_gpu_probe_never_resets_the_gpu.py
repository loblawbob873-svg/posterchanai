""""Could not ask" is not a reading — and here the difference is a GPU reset.

Run: venv-unified/bin/python -m unittest tests.test_a_failed_gpu_probe_never_resets_the_gpu

`app/services/health_check.py` was on a coverage audit's list of modules no test mentioned anywhere.
It is the only thing in the app that can RESET A GPU and reload the model underneath whatever is
running on it, and it decides that from the output of an external command — `nvidia-smi`, or i915's
debugfs, or a sudo helper script. That is the same shape as the /logs board, which spent a while
reporting a drive as failed, swap that did not exist and a mount that was never there, all parsed
from tools that had not actually answered.

The rules that matter, none of which were covered:

  a number means a number      a probe that could not run returns None, not 0 and not 100. Zero
                               reads as "plenty free" and nothing ever reloads; a hundred reads as
                               "full" and it resets the GPU on a timer.
  None never acts              `check_gpu_memory_and_reload` returns False on None. A GPU reset
                               takes the card away from whatever is generating on it, so doing it
                               because a command was missing is worse than doing nothing at all.
  used, not free               Intel's debugfs reports `visible_avail` (FREE). Reading it as used
                               inverts the decision: it resets an idle card and leaves a full one.

Every number below is parsed by the shipped function from real tool output, with the subprocess and
the filesystem stubbed — this node's own GPU is an Intel Arc and the answers must not depend on what
it happens to be doing.
"""
import builtins
import io
import subprocess
import unittest
from types import SimpleNamespace

from app.services import health_check as hc


class _Ran:
    """A stand-in for subprocess.run that answers whatever the test wants."""
    def __init__(self, stdout="", returncode=0, stderr="", boom=None):
        self.stdout, self.returncode, self.stderr, self.boom = stdout, returncode, stderr, boom
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(cmd)
        if self.boom:
            raise self.boom
        return SimpleNamespace(stdout=self.stdout, returncode=self.returncode, stderr=self.stderr)


class AFailedGpuProbeIsNotAReading(unittest.TestCase):

    def setUp(self):
        self._run, self._open = subprocess.run, builtins.open
        self.addCleanup(lambda: (setattr(subprocess, "run", self._run),
                                 setattr(builtins, "open", self._open)))

    def nvidia(self, **kw):
        subprocess.run = _Ran(**kw)
        return hc.get_gpu_memory_usage("nvidia")

    # ------------------------------------------------------------------ nvidia

    def test_it_reads_used_over_total_as_a_percentage(self):
        self.assertAlmostEqual(self.nvidia(stdout="8192, 16384\n"), 50.0, places=3)
        self.assertAlmostEqual(self.nvidia(stdout="15360, 16384\n"), 93.75, places=2)

    def test_the_first_gpu_is_the_one_reported(self):
        self.assertAlmostEqual(self.nvidia(stdout="4096, 16384\n12288, 16384\n"), 25.0, places=3)

    def test_a_probe_that_failed_is_not_a_reading(self):
        """Not 0 (which reads as plenty free and never reloads) and not 100 (which resets on a
        timer). The only honest answer is that it could not be asked."""
        self.assertIsNone(self.nvidia(returncode=9, stderr="command not found"))
        self.assertIsNone(self.nvidia(boom=FileNotFoundError("nvidia-smi")))
        self.assertIsNone(self.nvidia(boom=subprocess.TimeoutExpired("nvidia-smi", 10)))

    def test_output_that_is_not_a_reading_is_not_read_as_one(self):
        for junk in ("", "\n", "N/A, N/A\n", "no devices were found\n", "8192\n"):
            self.assertIsNone(self.nvidia(stdout=junk),
                              f"{junk!r} was turned into a GPU memory percentage")

    # ------------------------------------------------------------------ intel

    def test_intel_reports_what_is_USED_not_what_is_free(self):
        """`visible_avail` is FREE memory. Reading it as used inverts every decision made from it."""
        content = ("i915_gem_objects:\n  visible_size: 16384MiB\n  visible_avail: 4096MiB\n")
        builtins.open = lambda *a, **k: io.StringIO(content)
        self.assertAlmostEqual(hc.get_gpu_memory_usage("intel"), 75.0, places=3)

    def test_intel_with_no_debugfs_and_no_helper_is_not_a_reading(self):
        def _no_file(*a, **k):
            raise FileNotFoundError("i915_gem_objects")
        builtins.open = _no_file
        subprocess.run = _Ran(returncode=1)
        self.assertIsNone(hc.get_gpu_memory_usage("intel"))


class AFailedProbeNeverResetsTheGpu(unittest.TestCase):
    """The decision on top of the reading. A reset takes the card away from whatever is generating
    on it, so it has to be driven by a measurement and never by the absence of one."""

    SETTINGS = {"gpu_memory_check_enabled": True, "gpu_type": "nvidia",
                "gpu_memory_threshold": 90, "nvidia_reset_before_reload": True}

    def setUp(self):
        self.reloads, self.resets = [], []
        self._real = (hc.get_gpu_memory_usage, hc.reload_native_model, hc._run_nvidia_reset_sync)
        hc.reload_native_model = lambda db: self.reloads.append(True) or True
        hc._run_nvidia_reset_sync = lambda: self.resets.append(True) or True
        self.addCleanup(lambda: setattr_all(hc, self._real))

    def answer(self, usage, **over):
        hc.get_gpu_memory_usage = lambda gpu_type="nvidia": usage
        return hc.check_gpu_memory_and_reload(None, {**self.SETTINGS, **over})

    def test_a_probe_that_could_not_answer_changes_nothing(self):
        self.assertFalse(self.answer(None),
                         "a GPU reset was triggered by a probe that never ran — that takes the card "
                         "away from whatever is generating on it")
        self.assertEqual((self.reloads, self.resets), ([], []))

    def test_below_the_threshold_nothing_happens(self):
        self.assertFalse(self.answer(89.9))
        self.assertEqual((self.reloads, self.resets), ([], []))

    def test_at_or_above_the_threshold_it_reloads(self):
        self.assertTrue(self.answer(90.0))
        self.assertEqual(len(self.reloads), 1)
        self.assertEqual(len(self.resets), 1, "nvidia_reset_before_reload was ignored")

    def test_the_reset_is_skipped_when_it_is_switched_off(self):
        self.assertTrue(self.answer(99.0, nvidia_reset_before_reload=False))
        self.assertEqual(len(self.reloads), 1)
        self.assertEqual(self.resets, [], "the GPU was reset with the switch off")

    def test_the_whole_check_can_be_switched_off(self):
        self.assertFalse(self.answer(100.0, gpu_memory_check_enabled=False))
        self.assertEqual((self.reloads, self.resets), ([], []))


def setattr_all(module, values):
    module.get_gpu_memory_usage, module.reload_native_model, module._run_nvidia_reset_sync = values


if __name__ == "__main__":
    unittest.main()
