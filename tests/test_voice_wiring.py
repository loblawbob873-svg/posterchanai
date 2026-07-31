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


class VoiceMemeBuilder(unittest.TestCase):
    def test_builder_borrows_the_studio_rather_than_copying_it(self):
        """The Meme Builder must reuse AI Chat's voice studio through the PC bridge. A second copy
        would mean two voice libraries, two recorders and two sets of mobile layout to keep in step —
        the same reason 'Generate one with AI' borrows the image studio."""
        meme = _read("static/js/client/meme.js")
        self.assertIn("PC.openVoiceStudio(", meme)
        self.assertIn("onTake", meme)
        self.assertNotIn("pcai:voices", meme, "the builder must not read the voice library itself")
        app = _read("static/js/client/app.js")
        self.assertIn("\n    openVoiceStudio,", app, "openVoiceStudio must be on the PC bridge")

    def test_entry_is_on_the_add_sheet(self):
        meme = _read("static/js/client/meme.js")
        self.assertIn('id="mba-voice"', meme)
        self.assertIn("go('mba-voice', pickClonedVoice)", meme)


class VoiceInstaller(unittest.TestCase):
    def test_installer_guards_the_watermarker(self):
        """perth exports PerthImplicitWatermarker as None when its own import fails, so `import perth`
        succeeding proves nothing. On a Python 3.12 node with setuptools>=81 (pkg_resources removed)
        that is exactly what happens, and the model dies at construction AFTER the 6GB download."""
        sh = _read("scripts/install/voice.sh")
        self.assertIn("setuptools<81", sh)
        self.assertIn("perth.PerthImplicitWatermarker is not None", sh)

    def test_docker_guards_it_too(self):
        df = _read("Dockerfile")
        self.assertIn("setuptools<81", df)
        self.assertIn("perth.PerthImplicitWatermarker is not None", df)

    def test_docker_can_actually_enable_voice(self):
        """The Dockerfile ARG is useless if compose never passes it: POSTERCHANAI_VOICE must both
        INSTALL the engine at build time and enable it at runtime, or you get an image with the
        feature switched on and no model — exactly the trap the INSTALL_MUSIC comment describes."""
        compose = _read("docker-compose.yml")
        self.assertEqual(compose.count("INSTALL_VOICE"), 4, "every build profile must pass it")
        self.assertIn("POSTERCHANAI_VOICE=${POSTERCHANAI_VOICE:-0}", compose)
        self.assertIn("INSTALL_VOICE", _read("Dockerfile"))
        self.assertIn('os.environ.get("POSTERCHANAI_VOICE", "0")', _read("app/main.py"))

    def test_unsupported_language_explains_itself(self):
        """Chatterbox imports per-language tokenizer helpers lazily; we don't ship them. A raw
        ModuleNotFoundError from inside a tokenizer is a terrible way to learn that."""
        src = _read("app/services/voice_local.py")
        self.assertIn("except ModuleNotFoundError", src)
        self.assertIn("can't speak that language", src)

    def test_installer_never_lets_pip_move_torch(self):
        """--no-deps + a constraints file is what stops chatterbox's torch==2.6.0 pin replacing the
        GPU torch and breaking image, music and video at once."""
        sh = _read("scripts/install/voice.sh")
        self.assertIn("--no-deps chatterbox-tts", sh)
        self.assertIn('-c "$CONSTRAINTS"', sh)


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

    def test_uses_the_unified_server_list(self):
        """One chat_server_urls drives chat, image, music, video AND voice. A per-feature list is a
        second thing to keep in step, and the node missing from it fails by never being asked."""
        src = _read("app/services/voice_factory.py")
        self.assertIn('s.get("chat_server_urls"', src)
        self.assertNotIn("voice_server_urls", src)
        from app.schemas import SettingsResponse
        self.assertNotIn("voice_server_urls", SettingsResponse.model_fields,
                         "voice must not have a server list of its own")
        self.assertNotIn("voice_server_urls", _read("templates/admin/tabs/voice_generation.html"))

    def test_parse_excludes_self(self):
        from app.services.voice_factory import other_nodes
        self.assertEqual(other_nodes(""), [])
        # exclude_self is what stops a node proxying to itself and deadlocking on its own GPU lock.
        self.assertNotIn("http://127.0.0.1:3051", other_nodes("http://127.0.0.1:3051"))

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

    def test_empty_library_is_not_treated_as_unreachable(self):
        """"No document yet" and "relay unreachable" are both an empty query result. Conflating them
        broke the FIRST save for every user — there is no doc to find until you have saved one — and
        the studio complained about relays instead. Reachability comes from Relay.ready(), not from
        whether the query found anything."""
        js = _read("static/js/client/app.js")
        block = js.split("---- Voice studio ---")[1].split("window.__openVoiceStudio")[0]
        self.assertIn("Relay.ready(", block,
                      "voicesRead must ask the CONNECTION whether it is live")
        self.assertIn("Three live reads, nothing there", block,
                      "an empty result must be RETRIED before being called empty — a live socket can "
                      "read empty for a moment (EOSE racing the event), and acting on that first look "
                      "replaces a real library with one entry")

    def test_library_write_refuses_on_a_failed_read(self):
        """kind-30078 is REPLACEABLE: writing a list built on an empty/failed read replaces the whole
        library. Same wipe that took out mutes, follows and a drive's file index."""
        js = _read("static/js/client/app.js")
        block = js.split("---- Voice studio ---")[1].split("window.__openVoiceStudio")[0]
        self.assertIn("if(cur === null) throw", block)


if __name__ == "__main__":
    unittest.main(verbosity=1)
