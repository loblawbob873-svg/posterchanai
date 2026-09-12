"""A bot in a room is the SAME bot that is on the timeline — and it still only speaks when named.

Reported as "we need to make sure most/all of the bot features work in Concord", right after the
listener was found joining a room, reading it, matching a mention and then saying nothing: its
generator imported `generate_message` from `bot_commands`, a name that has never existed in that
package, and the failure was silent end to end (the except logged at WARNING to a logger the bot
configures no handler for, `generate` returned "", and the caller read that as "nothing to say").

So there is now ONE dispatcher with two transports — `nostrListener._dispatch`, which the room
listener calls with its own `reply` — and these tests pin the three rules that seam has to keep:
the bot answers only when addressed, a command reaches the same code the timeline uses, and the
Nostr path is byte-for-byte the behaviour it always had.
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "botframework"))

import concordListener as cl          # noqa: E402


class AddressAndCommands(unittest.TestCase):
    NAMES = ["PosterChan AI"]
    NPUB = "npub1examplebot"

    def _body(self, text):
        return cl._strip_address(text, self.NPUB, self.NAMES)

    def test_the_address_comes_off_or_no_command_ever_matches(self):
        """Every command in `_dispatch` matches from the START of the string. A room message names
        the bot first, so without stripping, `geni` is prose about a cat rather than a picture."""
        self.assertEqual(self._body("@PosterChan AI geni a cat"), "geni a cat")
        self.assertEqual(self._body("PosterChan AI: help"), "help")
        # The trailing punctuation of the address goes with it.
        self.assertEqual(self._body("hey posterchan ai, news bbc"), "hey news bbc")

    def test_a_command_buried_in_a_sentence_is_conversation(self):
        """Deliberate: the whitelist matches from the START, so "hey @bot, news bbc" falls through
        to the ordinary generator and gets a conversational answer. Matching mid-sentence would mean
        anyone saying "I like the news" to the bot gets a headline dump."""
        self.assertFalse(cl._is_command(self._body("hey posterchan ai, news bbc")))
        self.assertTrue(cl._is_command(self._body("@PosterChan AI news bbc")))
        self.assertEqual(self._body("nostr:%s search cats" % self.NPUB), "search cats")

    def test_conversation_is_not_a_command(self):
        """The whitelist must never swallow ordinary talk — that turns chatter into tool calls."""
        for line in ("what do you think about films?", "how are you", "that was funny",
                     "I like the news", "", "   "):
            self.assertFalse(cl._is_command(self._body(line)), line)

    def test_the_features_are_reachable(self):
        for line in ("geni a cat", "help", "search cats", "images cats", "news bbc",
                     "ytdl https://x/y", "screenshot https://x", "/narrate hi", "?"):
            self.assertTrue(cl._is_command(line), line)

    def test_the_effect_list_is_read_never_copied(self):
        """~84 effect names; a hand-typed second copy is how the Telegram lists drifted."""
        from bot_commands import MEDIA_COMMANDS
        self.assertEqual(list(cl._media_commands()), list(MEDIA_COMMANDS))
        self.assertTrue(cl._is_command(MEDIA_COMMANDS[0]))


class TheHandlePeopleActuallyType(unittest.TestCase):
    """A display name has a SPACE in it and an @-mention cannot.

    Measured in a real room: a request addressed to `@PosterChan_AI` — the only way a person can
    @-mention a bot called "PosterChan AI" — was not a mention at all, so the bot ignored it in
    silence. Matched literally the name could only be hit by `@PosterChan AI`, which no client
    offers and nobody types.
    """
    NAMES = ["PosterChan AI"]
    PK = "ab" * 32

    def _m(self, text):
        return cl.mentions(text, "npub1examplebot", self.PK, self.NAMES)

    def test_every_spelling_of_the_same_handle_is_the_same_handle(self):
        for t in ("@PosterChan_AI geni a cat", "@PosterChan AI geni a cat",
                  "@PosterChanAI geni a cat", "@posterchan-ai geni a cat",
                  "@PosterChan.AI geni a cat"):
            self.assertTrue(self._m(t), t)

    def test_it_still_takes_an_at_sign(self):
        """Bots are named "chess" and "news"; a room saying those words must not be answered."""
        self.assertFalse(self._m("posterchan ai geni a cat"))
        self.assertFalse(self._m("I was talking about PosterChan AI yesterday"))

    def test_a_longer_handle_is_a_different_bot(self):
        self.assertFalse(self._m("@PosterChan_AI_Bot hello"))
        self.assertFalse(self._m("@PosterChanAIX hello"))

    def test_the_body_is_stripped_by_the_same_rule_it_matched_on(self):
        """If the two rules disagree, a message is accepted as addressed and then keeps the address
        in its body — so every command reads as prose and answers conversationally."""
        for t in ("@PosterChan_AI geni a cat", "@PosterChanAI geni a cat",
                  "@posterchan-ai geni a cat"):
            self.assertTrue(self._m(t), t)
            self.assertEqual(cl._strip_address(t, "npub1examplebot", self.NAMES), "geni a cat")


class OnlyWhenTagged(unittest.TestCase):
    """THE RULE THE OWNER STATED: "they bot can only respond if tagged, make sure"."""

    def _run(self, text, *, mentioned):
        """One pass of the real `process_mentions` over one message, with every socket faked."""
        said = []
        chan = {"id": "chan1", "name": "general", "streamPubkeys": ["ab" * 32]}
        msg = {"id": "m1", "at": 10 ** 13, "text": text, "pubkey": "cd" * 32, "by": "someone"}

        class _Room:
            relays = ["wss://r"]
            def read(self, cid, wraps):   return {"messages": [msg]}
            def say(self, cid, t, tags=None, kind=9):
                said.append(("say", t, tags))
                return {"wrap": {"id": "w1"}}

        class _Session:
            room, relays = _Room(), ["wss://r"]
            def refresh_controls(self, q):  return [chan]

        wire = cl.Wire(query=lambda r, f: [{"id": "x"}],
                       publish=lambda r, e: said.append(("publish", e)),
                       identity=lambda: ("npub1examplebot", "ef" * 32, ["PosterChan AI"]),
                       generate=lambda t, m: "a generated reply",
                       dispatch=lambda body, send, who: said.append(("dispatch", body)))
        state = {"room": object(), "session": _Session(), "floor_ms": 0,
                 "seen": types.SimpleNamespace(has=lambda i: False, add=lambda i: None)}
        cl.process_mentions(state, wire)
        return said

    def test_an_unaddressed_command_is_ignored_completely(self):
        """`geni a cat` said to the ROOM, naming nobody, must produce nothing at all — not a
        picture, not a reply. A bot that runs commands for any passing line gets removed."""
        self.assertEqual(self._run("geni a cat", mentioned=False), [])

    def test_ordinary_room_chatter_is_ignored(self):
        self.assertEqual(self._run("anyone seen the new film?", mentioned=False), [])

    def test_an_addressed_command_reaches_the_dispatcher(self):
        acts = self._run("@PosterChan AI geni a cat", mentioned=True)
        self.assertIn(("dispatch", "geni a cat"), acts)

    def test_an_addressed_question_still_gets_a_plain_reply(self):
        """Adding commands must not take the ordinary answer away."""
        acts = self._run("@PosterChan AI what do you think?", mentioned=True)
        self.assertTrue(any(a[0] == "say" for a in acts), acts)
        self.assertFalse(any(a[0] == "dispatch" for a in acts), acts)


class OneDispatcherTwoTransports(unittest.TestCase):

    def test_the_nostr_path_still_posts_a_note(self):
        """The default transport must reach `send_reply` — and must NOT be the self-referential
        lambda that first shipped here, which recursed for ever on every Nostr mention."""
        import nostrListener as nl
        got = []
        real, nl.send_reply = nl.send_reply, lambda note, text="", **kw: got.append((note, text))
        try:
            nl._dispatch({"user": {"pubkey": "aa"}}, "help", {}, [])
        finally:
            nl.send_reply = real
        self.assertEqual(len(got), 1)
        self.assertIn("Poster-Chan", got[0][1])

    def test_an_injected_transport_replaces_it_entirely(self):
        import nostrListener as nl
        posted, sent = [], []
        real, nl.send_reply = nl.send_reply, lambda note, text="", **kw: posted.append(text)
        try:
            nl._dispatch(None, "help", None, None, reply=lambda t="", **kw: sent.append(t),
                         sender_key="zz", media_ok=False)
        finally:
            nl.send_reply = real
        self.assertEqual(len(sent), 1)
        self.assertEqual(posted, [], "the room transport leaked a note onto the timeline")

    def test_a_file_command_says_it_cannot_read_room_attachments(self):
        """`media_ok=False` must REFUSE with a sentence. Doing nothing is the failure this whole
        listener already shipped once."""
        import nostrListener as nl
        from bot_commands import MEDIA_COMMANDS
        sent = []
        nl._dispatch(None, MEDIA_COMMANDS[0] + " x", None, None,
                     reply=lambda t="", **kw: sent.append(t), sender_key="z", media_ok=False)
        self.assertEqual(len(sent), 1)
        self.assertIn("attachment", sent[0].lower())


class MediaShapes(unittest.TestCase):
    """A command hands back bytes, a list of bytes, or a list of (bytes, mime) — three shapes.

    Measured: a first version of the room transport iterated `image_bytes` directly, which walks a
    bare `bytes` one INTEGER at a time, so every `geni` reply died with "object supporting the
    buffer API required" AFTER the picture had been generated and paid for.
    """

    def _uploaded(self, **kw):
        """Drive the real `_room_reply` with the uploader and relay faked, and report what it
        tried to upload."""
        import nostr as _mk
        from app.services.nostr import media as _media
        got, said = [], []

        async def _fake_upload(cfg, sk, data, mime):
            got.append((bytes(data), mime))
            return {"url": "https://media.example/%d.bin" % len(got), "mime": mime}

        class _Room:
            def say(self, cid, t, tags=None, kind=9):
                said.append((t, tags))
                return {"wrap": {"id": "w"}}

        real_up, _media.upload = _media.upload, _fake_upload
        try:
            send = cl._room_reply(_Room(), "c1", lambda r, e: None, ["wss://r"])
            send(**kw)
        finally:
            _media.upload = real_up
        return got, said

    def test_a_bare_bytes_image_uploads_as_one_file(self):
        got, said = self._uploaded(text="Here is your image.", image_bytes=b"\x89PNG-pretend")
        self.assertEqual(len(got), 1, "a bare bytes image was not uploaded as one file")
        self.assertEqual(got[0][0], b"\x89PNG-pretend")
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0][1][0][0], "imeta")

    def test_a_list_of_images_uploads_each(self):
        got, _ = self._uploaded(image_bytes=[b"one", b"two", b"three"])
        self.assertEqual([g[0] for g in got], [b"one", b"two", b"three"])

    def test_a_list_of_pairs_keeps_each_mime(self):
        got, _ = self._uploaded(image_bytes=[(b"a", "image/webp"), (b"b", "image/gif")])
        self.assertEqual([g[1] for g in got], ["image/webp", "image/gif"])

    def test_video_and_audio_ride_the_same_path(self):
        self.assertEqual(self._uploaded(video_bytes=b"mp4")[0][0][1], "video/mp4")
        self.assertEqual(self._uploaded(audio_bytes=b"mp3")[0][0][1], "audio/mpeg")

    def test_the_caption_and_the_picture_are_ONE_message(self):
        """Two messages reads as a bot talking to itself."""
        _, said = self._uploaded(text="Here is your image.", image_bytes=[b"a", b"b"])
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0][0], "Here is your image.")
        self.assertEqual(len(said[0][1]), 2)

    def test_a_lost_upload_is_never_captioned_as_a_delivered_image(self):
        """geni's caption is "Here is your image." Sent alone after the upload failed, that is the
        bot asserting something untrue about work it really did do."""
        from app.services.nostr import media as _media
        said = []

        async def _boom(cfg, sk, data, mime):
            raise RuntimeError("401 blocked")

        class _Room:
            def say(self, cid, t, tags=None, kind=9):
                said.append((t, tags))
                return {"wrap": {"id": "w"}}

        real, _media.upload = _media.upload, _boom
        try:
            cl._room_reply(_Room(), "c", lambda r, e: None, ["wss://r"])(
                text="Here is your image. Hope you like it.", image_bytes=b"png")
        finally:
            _media.upload = real
        self.assertEqual(len(said), 1)
        self.assertIn("couldn't upload", said[0][0])
        self.assertIsNone(said[0][1])

    def test_the_shapes_are_the_nostr_normaliser_not_a_second_copy(self):
        import nostr as _mk
        self.assertEqual(_mk._to_media_list(b"x", None, None), [(b"x", "image/png")])
        self.assertEqual(_mk._to_media_list([b"x"], None, None), [(b"x", "image/png")])


class TheAttachmentShape(unittest.TestCase):

    def test_an_imeta_is_what_the_client_actually_parses(self):
        """`concord.js:publicAttachments` refuses a tag carrying `encryption-algorithm` or a
        non-https url, and reads `url`/`m`/`name` as "key value" strings inside one imeta tag."""
        tag = cl._imeta("https://media.example/x.png", "image/png", "reply0.png")
        self.assertEqual(tag[0], "imeta")
        fields = dict(p.split(" ", 1) for p in tag[1:])
        self.assertEqual(fields["url"], "https://media.example/x.png")
        self.assertEqual(fields["m"], "image/png")
        self.assertEqual(fields["name"], "reply0.png")
        self.assertNotIn("encryption-algorithm", fields)

    def test_the_client_parser_accepts_it(self):
        """Run the SHIPPED parser's rules against the tag we build, rather than trusting the shape:
        a fixture that agreed with the bug is how the cord bridge shipped without `URL`."""
        src = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
        self.assertIn("function publicAttachments", src)
        tag = cl._imeta("https://media.example/x.png", "image/png", "reply0.png")
        fields = dict(p.split(" ", 1) for p in tag[1:])
        # The three conditions publicAttachments applies, transcribed from the source it asserts on.
        self.assertNotIn("encryption-algorithm", fields)
        self.assertRegex(fields["url"], r"^https://")
        self.assertRegex(fields["m"], r"^[\w.+-]+/[\w.+-]+$")


if __name__ == "__main__":
    unittest.main()
