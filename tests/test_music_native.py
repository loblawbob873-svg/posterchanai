"""Tests for native (in-process) music generation — app/services/music_local.py.

Run: venv-unified/bin/python -m unittest tests.test_music_native

Music moved from a per-node ACE-Step REST sidecar to loading upstream's `AceStepHandler` inside the
app process. These cover the three ways that switch failed SILENTLY — each looked fine locally and
would only bite on another node, on a fresh install, or under VRAM pressure:

- `is_available()` gates native-vs-sidecar. It probed diffusers for `AceStepPipeline`, which the load
  path never touches. Reported unavailable on a node with acestep but older diffusers, sending it to
  a sidecar that no longer exists — and on a `video_free_music` node that means
  `vram_manager._ensure_music_server` polls a dead port for 90s SYNCHRONOUSLY, per song, on the
  single uvicorn worker. It must probe `acestep`, and must not import torch to do it.
- the native path read a `music_duration` key no schema defines, so Admin → Music's
  `music_default_duration` was silently ignored and every song came out at the fallback length.
- unload called `.to("cpu")` on `AceStepHandler`, which is not an nn.Module and has no `.to()`. The
  AttributeError went into a bare except, so the swap that is supposed to free VRAM for the
  LLM/image/video model freed nothing.

No GPU, model, database or acestep install needed — the handler is stubbed.
"""
import unittest
from unittest import mock

from app.services import music_local


class _FakeHandler:
    """Stands in for AceStepHandler: a plain object with model attributes and NO `.to()`."""

    def __init__(self):
        self.model = object()
        self.vae = object()
        self.text_encoder = object()
        self.silence_latent = object()
        self.released = False

    def _release_system_memory(self):
        self.released = True


class _Settings:
    def __init__(self, **kw):
        self._d = kw

    def get(self, k, d=None):
        return self._d.get(k, d)


class IsAvailableProbeTest(unittest.TestCase):
    def setUp(self):
        music_local._available = None      # clear the memo between cases

    def tearDown(self):
        music_local._available = None

    def test_probes_acestep_not_diffusers(self):
        seen = []

        def fake_find_spec(name):
            seen.append(name)
            return object() if name == "acestep" else None

        with mock.patch("importlib.util.find_spec", side_effect=fake_find_spec):
            self.assertTrue(music_local.is_available())
        self.assertIn("acestep", seen)
        self.assertNotIn("diffusers", seen)

    def test_false_when_acestep_absent_even_if_diffusers_present(self):
        with mock.patch("importlib.util.find_spec",
                        side_effect=lambda n: object() if n == "diffusers" else None):
            self.assertFalse(music_local.is_available())

    def test_probes_top_level_only_so_it_does_not_import_torch(self):
        """`find_spec("acestep.handler")` would import the parent package to read its __path__,
        dragging torch in on a path vram_manager hits for every GPU swap."""
        with mock.patch("importlib.util.find_spec", side_effect=lambda n: object()) as f:
            music_local.is_available()
        for (name,), _ in f.call_args_list:
            self.assertNotIn(".", name, f"probe must be top-level, got {name!r}")

    def test_result_is_memoised(self):
        with mock.patch("importlib.util.find_spec", side_effect=lambda n: object()) as f:
            music_local.is_available()
            music_local.is_available()
            music_local.is_available()
        self.assertEqual(f.call_count, 1)

    def test_never_raises(self):
        with mock.patch("importlib.util.find_spec", side_effect=RuntimeError("boom")):
            self.assertFalse(music_local.is_available())


class SettingsKeyTest(unittest.TestCase):
    def test_duration_comes_from_the_admin_key(self):
        """music_default_duration is what Admin → Music writes and what the HTTP path reads."""
        with mock.patch.object(music_local, "settings_store",
                               _Settings(music_default_duration="240")):
            self.assertEqual(music_local._get_settings(None)["duration"], 240.0)

    def test_duration_ignores_the_bogus_private_key(self):
        with mock.patch.object(music_local, "settings_store",
                               _Settings(music_duration="12", music_default_duration="240")):
            self.assertEqual(music_local._get_settings(None)["duration"], 240.0)

    def test_duration_falls_back_to_the_schema_default(self):
        with mock.patch.object(music_local, "settings_store", _Settings()):
            self.assertEqual(music_local._get_settings(None)["duration"], 180.0)

    def test_blank_model_means_the_local_checkpoint_dir(self):
        with mock.patch.object(music_local, "settings_store", _Settings(music_model="  ")):
            self.assertEqual(music_local._get_settings(None)["model"], music_local.DEFAULT_MODEL)

    def test_garbage_values_do_not_explode(self):
        with mock.patch.object(music_local, "settings_store",
                               _Settings(music_default_duration="soon", music_default_steps="lots")):
            cfg = music_local._get_settings(None)
        self.assertEqual(cfg["duration"], 180.0)
        self.assertEqual(cfg["steps"], 8)


class UnloadReclaimsVramTest(unittest.TestCase):
    def _svc(self):
        with mock.patch.object(music_local, "_start_idle"):
            svc = music_local.MusicService()
        svc._pipe = _FakeHandler()
        svc._device = "cpu"
        return svc

    def test_unload_drops_the_model_references(self):
        svc = self._svc()
        pipe = svc._pipe
        with mock.patch("app.services.vram_manager.reset_vram_mode"):
            svc.unload_model()
        self.assertFalse(svc.is_loaded())
        for attr in ("model", "vae", "text_encoder", "silence_latent"):
            self.assertIsNone(getattr(pipe, attr),
                              f"{attr} still held — its VRAM is not reclaimed")
        self.assertTrue(pipe.released, "upstream's _release_system_memory was not called")

    def test_unload_does_not_call_to_on_a_non_module_handler(self):
        """A handler with no `.to()` must still unload; calling one raised into a bare except and
        silently freed nothing."""
        svc = self._svc()
        self.assertFalse(hasattr(svc._pipe, "to"))
        with mock.patch("app.services.vram_manager.reset_vram_mode"):
            svc.unload_model()          # must not raise
        self.assertIsNone(svc._pipe)

    def test_idle_unload_skipped_while_generating(self):
        svc = self._svc()
        svc._generating = 1
        with mock.patch("app.services.vram_manager.reset_vram_mode"):
            svc.unload_model(skip_if_generating=True)
        self.assertTrue(svc.is_loaded(), "unloaded the model out from under a running generation")

    def test_unload_is_idempotent(self):
        svc = self._svc()
        with mock.patch("app.services.vram_manager.reset_vram_mode"):
            svc.unload_model()
            svc.unload_model()          # must not raise on an already-empty handler
        self.assertIsNone(svc._pipe)


class NoDeadAudioConversionTest(unittest.TestCase):
    """ACE-Step encodes the file itself, so the WAV round-trip helpers are gone. If they come back,
    so does the question of which one actually runs."""

    def test_wav_and_transcode_helpers_are_gone(self):
        for name in ("_to_wav_bytes", "_transcode", "_CTYPE"):
            self.assertFalse(hasattr(music_local, name),
                             f"{name} is dead code — generate() returns ACE-Step's own bytes")

    def test_module_does_not_import_wave_or_io_for_audio(self):
        with open(music_local.__file__) as fh:
            src = fh.read()
        self.assertNotIn("\nimport wave", src)


if __name__ == "__main__":
    unittest.main()
