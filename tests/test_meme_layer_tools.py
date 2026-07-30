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
