"""Pins the wiring that voice cloning silently depends on.

Every assertion here corresponds to a way this feature can break WITHOUT an error appearing anywhere:

  command coverage     `voice` needs the reference clip's raw BYTES. Miss it out of
                       MEDIA_TOOL_COMMANDS and it is handed attachments=None and can only answer
                       "attach a clip" — the exact defect that hit `lookingaway` and `goon`.
  telegram lists       Telegram does NOT use parse_command; it matches its own hardcoded list. A
                       command missing there falls through to the LLM, which cheerfully pretends.
  settings hydration   A key absent from SettingsResponse is dropped from the GET, so the field
                       loads blank forever and a CHECKBOX then posts false over the stored value on
                       the next Save — silently switching the feature off.
  vram eviction        The voice weights are ~6GB. If a prepare_for_* forgets to evict them, the next
                       LLM/image load OOMs on a shared card. And prepare_for_voice must NOT evict
                       them, or every request pays a reload.
  portability          The load path must force a CPU map_location. Without it the published
                       CUDA-tagged checkpoints refuse to load on Arc(XPU) and CPU — which is how this
                       feature failed first time round.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class VoiceCommandWiring(unittest.TestCase):
    def test_voice_is_a_command(self):
        from app.services.command_service import CommandService
        self.assertIn("voice", CommandService.COMMANDS)

    def test_voice_gets_raw_attachment_bytes(self):
        from app.services.command_service import CommandService
        self.assertTrue(
            CommandService.wants_attachments("voice"),
            "voice must be in MEDIA_TOOL_COMMANDS or it never receives the reference clip")

    def test_voice_is_in_the_telegram_lists(self):
        from app.routers.telegram import messages as tg
        self.assertIn("voice", tg._TG_COMMANDS,
                      "voice missing from the Telegram command list — it would hit the LLM instead")
        self.assertIn("voice", tg._TG_RAW_MEDIA_COMMANDS,
                      "voice must take raw media on Telegram, not OCR'd text")

    def test_voice_is_capability_gated(self):
        from app.services.command_service import CommandService
        self.assertIn("voice", CommandService._CAPABILITY_BY_COMMAND,
                      "cloning a voice is an impersonation surface — it must be gated like the "
                      "other GPU features, not open to every user")


class VoiceSettings(unittest.TestCase):
    def test_every_admin_field_is_declared(self):
        """A field in the tab that isn't in SettingsResponse never hydrates (and a checkbox then
        writes false over the stored value). Also pins id == name, which is what Save reads."""
        from app.schemas import SettingsResponse
        declared = set(SettingsResponse.model_fields)
        html = _read("templates/admin/tabs/voice_generation.html")
        for m in re.finditer(r'<(?:input|select|textarea)\b([^>]*)>', html):
            attrs = m.group(1)
            name = re.search(r'\bname="([^"]+)"', attrs)
            ident = re.search(r'\bid="([^"]+)"', attrs)
            if not name:
                continue
            self.assertIn(name.group(1), declared,
                          f"{name.group(1)} is in the Voice tab but not in SettingsResponse")
            self.assertTrue(ident and ident.group(1) == name.group(1),
                            f"{name.group(1)}: id must equal name (hydration reads id, Save reads name)")

    def test_tab_is_registered(self):
        admin = _read("templates/admin.html")
        self.assertIn('data-tab="voice"', admin)
        self.assertIn("admin/tabs/voice_generation.html", admin)

    def test_download_button_uses_a_real_kind(self):
        html = _read("templates/admin/tabs/voice_generation.html")
        self.assertIn("downloadModel('voice'", html)
        from app.services import model_download_service as mds
        self.assertIn("voice", mds._FNS, "the button posts kind=voice; the service must know it")

    def test_voice_is_off_by_default(self):
        from app.schemas import SettingsResponse
        self.assertEqual(SettingsResponse().voice_enabled, "false",
                         "6GB of weights and a GPU-blocking model must not default to on")


class VoiceVram(unittest.TestCase):
    def test_every_other_task_evicts_voice(self):
        src = _read("app/services/vram_manager.py")
        for fn in ("prepare_for_llm", "prepare_for_image", "prepare_for_music", "prepare_for_video"):
            body = src.split(f"def {fn}(")[1].split("\ndef ")[0]
            self.assertIn("_unload_native_voice(db)", body,
                          f"{fn} must free the ~6GB voice model or the next load OOMs")

    def test_prepare_for_voice_does_not_evict_itself(self):
        src = _read("app/services/vram_manager.py")
        body = src.split("def prepare_for_voice(")[1].split("\ndef ")[0]
        self.assertNotIn("_unload_native_voice(db)", body,
                         "prepare_for_voice must not drop the model it is about to use")


class VoicePortability(unittest.TestCase):
    def test_load_forces_cpu_map_location(self):
        """Upstream only forces a CPU map_location for cpu/mps, so Arc and ROCm inherit the
        checkpoint's CUDA storage tags and die on load. This is the fix; do not remove it."""
        src = _read("app/services/voice_local.py")
        self.assertIn('kw["map_location"] = "cpu"', src)
        self.assertIn("torch.load = _cpu_load", src)
        self.assertIn("torch.load = _orig_load", src,
                      "torch.load must be restored, or every OTHER model in the process "
                      "silently loads onto the CPU afterwards")

    def test_unload_drops_submodules_by_name(self):
        """ChatterboxTTS is a plain object with no .to()/.cpu() — the ACE-Step trap. Unload has to
        drop the attributes the weights hang off, or it frees nothing."""
        src = _read("app/services/voice_local.py")
        for attr in ("t3", "s3gen", "ve", "conds"):
            self.assertIn(f'"{attr}"', src)


class VoiceLoadBalancing(unittest.TestCase):
    def test_round_robin_is_not_modulo_list_length(self):
        """`_rr_index % len(candidates)` resets the rotation whenever the node list changes size,
        pinning every request to node 0."""
        src = _read("app/services/voice_factory.py")
        self.assertIn("_rr_index = (_rr_index + 1) % 1_000_000", src)

    def test_local_is_a_sentinel_not_a_url(self):
        """A node HTTPing its own /api/generate-voice would hold the GPU lock while waiting for a
        request queued behind that same lock."""
        src = _read("app/services/voice_factory.py")
        self.assertIn('candidates.append("local")', src)

    def test_parse_urls(self):
        from app.services.voice_factory import parse_voice_server_urls
        self.assertEqual(parse_voice_server_urls("http://a/, http://b\nhttp://c/"),
                         ["http://a", "http://b", "http://c"])
        self.assertEqual(parse_voice_server_urls(""), [])

    def test_node_endpoint_does_not_forward_again(self):
        """/api/generate-voice must call _generate_local, not the public entry point — otherwise a
        node can forward to a node that forwards to a node."""
        src = _read("app/routers/voice_api.py")
        self.assertIn("voice_factory._generate_local(", src)


class VoiceClientUI(unittest.TestCase):
    def test_studio_is_reachable_from_both_render_paths(self):
        """The splash cards and the mid-chat ✨ picker are two separate lists of the same actions —
        adding a feature to one and not the other ships it invisible in the other."""
        js = _read("static/js/client/app.js")
        self.assertIn('data-gen="voice"', js, "missing from the splash cards")
        self.assertIn("['voice','Clone a voice']", js, "missing from the mid-chat picker")
        # Both routes must DIVERT to the voice studio. openGenStudio drives a prompt sheet, and a
        # voice has no prompt — it is a saved clip — so falling through to it would open an empty
        # studio that generates nothing.
        self.assertIn("gc.dataset.gen==='voice'){ openVoiceStudio(); }", js,
                      "the splash card must divert to the voice studio")
        self.assertIn("if(b.dataset.gen==='voice') openVoiceStudio();", js,
                      "the mid-chat picker must divert to the voice studio")
        self.assertIn("voice: { cmd:'voice'", js,
                      "_GEN needs a voice entry or the picker throws on G.icon")

    def test_studio_uses_local_modal_helpers(self):
        """app.js PUBLISHES window.__PC — inside it, `PC.modal` is undefined."""
        js = _read("static/js/client/app.js")
        block = js.split("---- Voice studio ---")[1].split("window.__openVoiceStudio")[0]
        self.assertNotIn("PC.modal(", block)
        self.assertNotIn("PC.closeModal(", block)

    def test_library_write_refuses_on_a_failed_read(self):
        """kind-30078 is REPLACEABLE: writing a list built on an empty/failed read replaces the whole
        library. Same wipe that took out mutes, follows and a drive's file index."""
        js = _read("static/js/client/app.js")
        block = js.split("---- Voice studio ---")[1].split("window.__openVoiceStudio")[0]
        self.assertIn("if(cur === null) throw", block)


if __name__ == "__main__":
    unittest.main(verbosity=1)
