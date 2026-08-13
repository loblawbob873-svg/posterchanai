"""Tests for the Admin → Download model button — app/services/model_download_service.py.

Two defects lived here, both invisible on the box they were written on:

- The music button fed `music_model` to `huggingface_hub.snapshot_download` as a repo id. It is a
  CHECKPOINT DIRECTORY NAME under <ACESTEP_ROOT>/checkpoints (music_local.DEFAULT_MODEL says so, and
  load_model passes it as `config_path`), so the button failed 100% of the time with a 401
  "Repository Not Found for .../models/acestep-v15-turbo" — which reads as an auth problem — on a
  node whose weights were on disk and generating songs fine. Upstream's handler fetches what's
  missing on first use, so loading IS the download.

- Every download ran its VRAM swap and model load with NO GPU lock. These functions run in a plain
  `threading.Thread`, where the async GPUResourceLock is unusable: `_gpu_lock_base` is bound to the
  MAIN event loop, so the one path that tried (`asyncio.run`) attached futures to the wrong loop and
  excluded nothing. Pressing Download mid-song therefore unloaded the running model and put a second
  multi-GB model on the same GPU. Hence GPUResourceLockSync, which takes the cross-process FILE lock
  that every async GPU task also takes.

No GPU, model, network or database needed — everything is stubbed.
"""
import importlib.util
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from app.services import locks
from app.services import model_download_service as mds


class _FakeSvc:
    def __init__(self):
        self.events = []

    def load_model(self, db):
        self.events.append("load")

    def unload_model(self):
        self.events.append("unload")


class _RecordingLock:
    """Stands in for GPUResourceLockSync so we can assert the load happened INSIDE it."""

    def __init__(self, outer):
        self.outer = outer

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        self.outer.events.append("lock")
        return self

    def __exit__(self, *a):
        self.outer.events.append("unlock")
        return False


class MusicDownloadTest(unittest.TestCase):
    """The native path: no Hub call, and the load is inside the GPU lock."""

    def setUp(self):
        self.events = []
        self.svc = _FakeSvc()
        self.svc.events = self.events

    def _run(self, native=True):
        lock = _RecordingLock(self)
        with mock.patch.object(mds, "_run_sync") as run_sync, \
             mock.patch("app.services.vram_manager._native_music_active", return_value=native), \
             mock.patch("app.services.vram_manager.prepare_for_music") as prep, \
             mock.patch("app.services.locks.GPUResourceLockSync", lock), \
             mock.patch("app.services.music_service.get_settings",
                        return_value={"device": "auto", "fmt": "mp3", "base_url": "http://x:8001"}), \
             mock.patch("app.services.music_service.build_request_body", return_value={}), \
             mock.patch("app.services.music_service.generate_once"), \
             mock.patch("app.services.music_local._get_settings", return_value={"model": "acestep-v15-turbo"}), \
             mock.patch("app.services.music_local.get_music_service", return_value=self.svc):
            self.prep, self.run_sync = prep, run_sync
            mds._download_music(db=object())
        return mds.status("music")

    @unittest.skipUnless(importlib.util.find_spec("huggingface_hub"),
                         "huggingface_hub is not installed here — `mock.patch` cannot resolve a "
                         "target in a module that will not import, so this cannot RUN. A skip that "
                         "says why, never a failure: on a machine without the AI extras this test "
                         "failing looked exactly like the download code being broken.")
    def test_does_not_treat_the_checkpoint_dir_as_a_hub_repo(self):
        """`music_model` is a local directory name; snapshot_download 401s on it, always."""
        with mock.patch("huggingface_hub.snapshot_download") as snap:
            st = self._run()
        snap.assert_not_called()
        self.assertEqual(st["state"], "done", st["message"])

    def test_load_and_unload_happen_inside_the_gpu_lock(self):
        self._run()
        self.assertEqual(self.events, ["lock", "load", "unload", "unlock"])

    def test_vram_swap_is_inside_the_lock_too(self):
        """prepare_for_music unloads OTHER models — outside the lock that races a live generation."""
        self._run()
        self.assertEqual(self.events[0], "lock")
        self.prep.assert_called_once()

    def test_unload_runs_even_when_the_load_fails(self):
        self.svc.load_model = mock.Mock(side_effect=RuntimeError("OOM on this GPU"))
        st = self._run()
        self.assertEqual(st["state"], "error")
        self.assertIn("OOM on this GPU", st["message"])
        self.assertEqual(self.events, ["lock", "unload", "unlock"])

    def test_external_server_path_does_not_use_asyncio_run(self):
        """The old spelling awaited a main-loop asyncio.Lock from a second loop, excluding nothing."""
        st = self._run(native=False)
        self.assertEqual(self.events, ["lock", "unlock"])
        self.run_sync.assert_called_once()
        self.assertEqual(st["state"], "done", st["message"])


class ImageDownloadTest(unittest.TestCase):
    def test_load_is_inside_the_gpu_lock(self):
        events = []

        class _Rec(_RecordingLock):
            pass

        outer = mock.Mock()
        outer.events = events
        lock = _Rec(outer)

        svc = mock.Mock()
        svc.anime_model_path = ""
        svc._ensure_model_loaded.side_effect = lambda *a, **kw: events.append("load")

        with mock.patch("app.services.locks.GPUResourceLockSync", lock), \
             mock.patch("app.services.vram_manager.prepare_for_image",
                        side_effect=lambda db: events.append("swap")), \
             mock.patch("app.services.diffusers_service.get_diffusers_service", return_value=svc):
            mds._download_image(db=object())

        self.assertEqual(events, ["lock", "swap", "load", "unlock"])
        self.assertEqual(mds.status("image")["state"], "done")


class SyncLockExclusionTest(unittest.TestCase):
    """GPUResourceLockSync must really exclude — it is the only thing standing between a download
    thread and a generation, and it has to work from a plain thread with no event loop."""

    def setUp(self):
        """Point the lock files at a temp dir. Alone among the tests here these take the REAL
        cross-process flock, so on the default path (/tmp/posterchanai_locks/gpu.lock) they contend
        with the LIVE service on this box: while a generation is running `test_second_holder_waits`
        blocks for the full GPU_LOCK_WAIT_TIMEOUT (630s) instead of asserting anything, and
        `gpu_busy()` truthfully reports that unrelated holder. The header promises these tests need
        no GPU; this is what makes that true."""
        self._tmp = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(locks, "GPU_LOCK_FILE", os.path.join(self._tmp, "gpu.lock")),
            mock.patch.object(locks, "CPU_LOCK_FILE", os.path.join(self._tmp, "cpu.lock")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_second_holder_waits(self):
        from app.services.locks import GPUResourceLockSync
        with GPUResourceLockSync("Music", "held"):
            with self.assertRaises(TimeoutError):
                with GPUResourceLockSync("Image", "blocked", timeout=1.0):
                    self.fail("acquired a lock that was already held")

    def test_released_on_exit_even_after_an_error(self):
        from app.services.locks import GPUResourceLockSync
        try:
            with GPUResourceLockSync("Music", "boom"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with GPUResourceLockSync("Image", "after", timeout=2.0):
            pass    # would raise TimeoutError if the failed hold leaked the fd

    def test_gpu_busy_reports_the_sync_holder(self):
        """The LB reads gpu_busy() to send work to an idle node instead of queueing behind us."""
        from app.services import locks
        self.assertFalse(locks.gpu_busy())
        with locks.GPUResourceLockSync("Music", "model download"):
            self.assertTrue(locks.gpu_busy())
        self.assertFalse(locks.gpu_busy())

    def test_works_from_a_plain_thread(self):
        from app.services.locks import GPUResourceLockSync
        seen = []

        def worker():
            with GPUResourceLockSync("Music", "in a thread", timeout=5.0):
                seen.append(time.time())

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
