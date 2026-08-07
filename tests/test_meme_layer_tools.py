"""What /meme/apply-effect is allowed to run on a Meme Builder layer.

Run: venv-unified/bin/python -m unittest tests.test_meme_layer_tools

That endpoint hands its `effect` argument to _execute_command_inner, which is the dispatch for EVERY
command the app has — `post`, `mail`, `node`, `torrents`, the lot. The allowlist in front of it is
therefore a security boundary, not a UX nicety: whatever is in it, a caller with a valid self-proof
can invoke on this node. It exists so the answer is "effects, and nothing else".

Background removal is the first exception, because a cut-out is compositing and compositing is what
the builder is for. This pins the exception to a NAMED set instead of letting it drift into "well,
also the media tools" — which would quietly hand over `post`, `translate` and `ocr` with it.
"""
import unittest

from app.services.command_service import CommandService


# Commands that must NEVER be reachable through the Meme Builder. Deliberately a hand-written list:
# it is the point of the test, and it should fail loudly if one of these ends up in a set that the
# allowlist unions in.
FORBIDDEN = [
    "post",        # publishes to Nostr as the user
    "mail",        # reads/sends email
    "node",        # unrestricted RCE on a configured node
    "logs",        # runs the agentic system-health report
    "geni", "musicgeni", "videogeni",   # take the GPU for minutes at a time
    "torrents", "nyaa", "ytdl",
    "translate", "ocr", "flashcards", "bill", "remind",
    "compress", "clip", "convert", "extractaudio", "circlecrop",
]


class TestMemeLayerAllowlist(unittest.TestCase):
    def test_the_tools_are_real_commands(self):
        # A typo here would be a silently dead button, since the endpoint would 400 "unknown effect".
        for name in CommandService.MEME_LAYER_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, CommandService.COMMANDS,
                              "%r is in MEME_LAYER_TOOLS but is not a command" % name)

    def test_the_tools_take_file_bytes(self):
        # These operate ON the layer's image. One that only reads text extracted from an upload would
        # be handed the bytes and do nothing useful.
        for name in CommandService.MEME_LAYER_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, CommandService.MEDIA_TOOL_COMMANDS)
                self.assertTrue(CommandService.wants_attachments(name))

    def test_removebackground_and_its_aliases_are_allowed(self):
        allowed = CommandService.meme_layer_allowed()
        for typed in ("removebackground", "removebg", "rmbg", "nobg"):
            with self.subTest(typed=typed):
                # The endpoint resolves the alias BEFORE the allowlist check, so all four must land.
                resolved = CommandService.COMMAND_ALIASES.get(typed, typed)
                self.assertIn(resolved, allowed)

    def test_effects_are_still_allowed(self):
        allowed = CommandService.meme_layer_allowed()
        self.assertTrue(CommandService.MOTION_EFFECTS <= allowed)
        self.assertTrue(CommandService.ANIMATED_EFFECTS <= allowed)

    def test_nothing_dangerous_is_reachable(self):
        allowed = CommandService.meme_layer_allowed()
        leaked = [c for c in FORBIDDEN
                  if CommandService.COMMAND_ALIASES.get(c, c) in allowed]
        self.assertEqual(leaked, [],
                         "these are reachable through /meme/apply-effect and must not be: %s" % leaked)

    def test_the_allowlist_is_only_effects_plus_the_named_tools(self):
        # The general form of the test above: anything in the allowlist is either an effect or an
        # explicitly listed layer tool. Adding a set to meme_layer_allowed() fails here first.
        extra = (CommandService.meme_layer_allowed()
                 - CommandService.MOTION_EFFECTS
                 - CommandService.ANIMATED_EFFECTS
                 - CommandService.MEME_LAYER_TOOLS)
        self.assertEqual(extra, set())

    def test_the_router_uses_the_shared_allowlist(self):
        """A copy of the union inline in the router is how this drifts — see the effect-command
        coverage test for the same guard on the attachment lists."""
        import inspect
        from app.routers import client as client_router
        src = inspect.getsource(client_router.meme_apply_effect)
        self.assertIn("meme_layer_allowed()", src)


if __name__ == "__main__":
    unittest.main()


class TestPoseAliases(unittest.TestCase):
    """A character's NICKNAME must reach the same artwork its canonical name does.

    Every pose has them — `seinfeldjerry` for jerry, `brutananadilewski` for carl, `soyjak`,
    `oldman`, `rabbi`, `unclerukus` — and they live in one table, `_CHARACTERS`, which maps each to a
    PNG. The catalogue is keyed by the CANONICAL name, so `canonical_alpha_effect` used to hand an
    alias straight back and every check that compared it against the catalogue then failed on a name
    whose artwork was right there: `render_alpha_effect` raised "unknown effect: seinfeldjerry" and
    `_pose_art_path` returned "", which reads as "this character cannot be made to talk" for a
    character that can. Same shape as the effect-alias rule in CLAUDE.md: resolve BEFORE the
    allowlist check, because clients cache a catalogue and keep sending the old name.
    """

    def _poses(self):
        from app.services import meme_builder_service as mb
        return {e["name"] for e in mb.alpha_effect_catalog() if e.get("pose")}

    def test_every_nickname_of_a_pose_is_a_pose(self):
        from app.services import meme_builder_service as mb
        from app.services.effects_service import _common as _c
        poses = self._poses()
        art = getattr(_c, "_CHARACTERS", {})
        canon_files = {art[p] for p in poses if p in art}
        for alias, f in art.items():
            if f not in canon_files:
                continue                     # not a pose's artwork (lookingaway's two panels)
            with self.subTest(alias=alias):
                self.assertIn(mb.canonical_alpha_effect(alias), poses,
                              f"{alias} draws {f}, which IS a pose — it must resolve to one")

    def test_the_talk_resolver_accepts_an_alias(self):
        """_pose_art_path is what the mouth picker, its detection seed and the render all go
        through, so an alias failing here is the whole feature missing for that name."""
        from app.routers.client import _pose_art_path
        for alias in ("seinfeldjerry", "brutananadilewski", "soyjak", "oldman", "unclerukus"):
            with self.subTest(alias=alias):
                self.assertTrue(_pose_art_path(alias), f"{alias} has artwork but resolved to nothing")

    def test_an_audio_effect_is_still_not_a_pose(self):
        """`seinfeld` is the theme over your OWN untouched image — there is no character drawing to
        lip-sync, so it must keep failing the pose check rather than being swept in by the alias fix.
        `lookingaway` is the two-panel turn: an animation, however still each panel is."""
        from app.services import meme_builder_service as mb
        from app.routers.client import _pose_art_path
        poses = self._poses()
        for name in ("seinfeld", "lookingaway", "anyways", "lookaway"):
            with self.subTest(name=name):
                self.assertNotIn(mb.canonical_alpha_effect(name), poses)
                self.assertFalse(_pose_art_path(name))


class TestStagePreviewSurvivesAStaleStylesheet(unittest.TestCase):
    """The stage's .mb-fx wrapper must carry its own layout INLINE.

    It is a positioned box whose child media is sized `width:100%;height:100%`. If the JS that emits
    it can arrive without the CSS that lays it out — a service worker holding one file and not the
    other, or a desktop/APK bundle whose copy of client.css is a build behind — then `<i>` is an
    ordinary inline box with no size and EVERY image and clip on the stage collapses to nothing. The
    build looks empty, with no error in the console, and the only cure the user can find is a hard
    refresh. That happened in Firefox the first time this shipped.

    So this is not style policing: it pins the one property that makes the element independent of a
    second file landing at the same moment.
    """

    def _js(self):
        import os
        return open(os.path.join(os.path.dirname(__file__), "..", "static", "js",
                                 "client", "meme.js"), encoding="utf-8").read()

    def test_the_wrapper_styles_itself(self):
        import re
        js = self._js()
        m = re.search(r"function _rotCss\(l\)\{(.+?)\n  \}", js, re.S)
        self.assertIsNotNone(m, "_rotCss (the .mb-fx wrapper's style) went away")
        body = m.group(1)
        for prop in ("position:absolute", "inset:0", "display:block"):
            self.assertIn(prop, body,
                          f"the .mb-fx wrapper must set {prop} inline — a class alone is a promise "
                          f"that meme.js and client.css shipped together, which caches do not keep")

    def test_the_wrapper_is_actually_used(self):
        """…and that the stage still emits it, so the test above cannot pass on dead code."""
        js = self._js()
        self.assertIn('class="mb-fx', js)
        self.assertIn("_rotCss(l)", js)
