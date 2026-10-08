"""Texts ✨ — a suggested reply to the last message in a text conversation.

The endpoint (/api/texts/ai-reply) is RUN with the model stubbed, through both of its ways in: the
web session (browser/desktop/tablet) and the phone's own signed request (ThreadActivity has no
session — it signs a kind-27235 bound to `texts-ai-reply` with the keystore key its archive already
uses). The rules pinned here:

  * the prompt is built from the LAST message plus a BOUNDED window before it, labelled Me/Them;
  * the draft comes back as text for the composer — nothing sends;
  * the AI gate is nip05_access's predicate (the same one chat/websearch use) and a refusal never
    reaches the model; a nostr-only node refuses too;
  * an empty conversation is refused with a sentence;
  * NO LOG LINE carries the conversation or the draft.
"""
import asyncio
import base64
import json
import logging
import os
import unittest
from unittest import mock

os.environ.setdefault("POSTERCHANAI_SKIP_DB", "1")

from app.routers import texts as T  # noqa: E402
from app.services import texts_ai_service as S  # noqa: E402
from app.services.nostr import event as E  # noqa: E402

SECRET_IN = "the pin code for the storage unit is 4471"
SECRET_OUT = "Thanks — heading there after work"


class _FakeChat:
    def __init__(self, answer="", boom=False):
        self.answer, self.boom, self.calls = answer, boom, []
        self.temperature = 0.7

    async def chat(self, msgs):
        self.calls.append(msgs)
        if self.boom:
            raise RuntimeError("model down " + SECRET_IN)
        return self.answer


class _User:
    def __init__(self, ai=True):
        self.ai = ai
        self.is_admin = False
        self.can_ai = ai
        self.nostr_npub = ""


def _run(req, chat, user=None, allowed=True, member=False):
    class _CS:
        def __init__(self, db, user=None):
            self.chat_service = chat

    async def ai_allowed(u):
        return allowed

    async def is_member(pk):
        return member

    with mock.patch("app.services.command_service.CommandService", _CS), \
         mock.patch("app.services.nip05_access.ai_allowed", ai_allowed), \
         mock.patch("app.services.nip05_access.is_member", is_member):
        return asyncio.run(T.texts_ai_reply(req, db=None, session_user=user))


def _body(resp):
    if isinstance(resp, dict):
        return 200, resp
    return resp.status_code, json.loads(resp.body)


def _thread(n, last="Are you coming tonight?"):
    msgs = [T.TextsMsg(me=(i % 2 == 0), text=f"message {i}") for i in range(n - 1)]
    msgs.append(T.TextsMsg(me=False, text=last))
    return msgs


def _signed(purpose=T.AUTH_PURPOSE):
    sk = bytes(range(1, 33))
    ev = E.build_event(sk, 27235, purpose, tags=[])
    return ev["pubkey"], base64.b64encode(json.dumps(ev).encode()).decode()


class PromptTests(unittest.TestCase):
    def test_the_prompt_is_the_last_message_plus_a_bounded_window(self):
        chat = _FakeChat("Yes, see you at 8!")
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(25)), chat, user=_User()))
        self.assertEqual(code, 200)
        self.assertEqual(out["content"], "Yes, see you at 8!")
        self.assertEqual(len(chat.calls), 1)
        user_msg = chat.calls[0][-1]["content"]
        # The last message is there, labelled as theirs, and it is the LAST line of the fence.
        self.assertIn("Them: Are you coming tonight?\nTEXTS", user_msg)
        # Bounded: 25 messages in, only the last MAX_CONTEXT reach the model.
        fenced = user_msg.split("<<<TEXTS\n", 1)[1].split("\nTEXTS", 1)[0].split("\n")
        self.assertEqual(len(fenced), S.MAX_CONTEXT)
        self.assertTrue(all(l.startswith(("Me: ", "Them: ")) for l in fenced), fenced)
        self.assertNotIn("message 0\n", user_msg)
        self.assertNotIn("message 14\n", user_msg)
        self.assertIn("message 15", user_msg)
        self.assertIn("reply to their last message", user_msg)

    def test_each_message_is_clipped_and_blank_ones_are_dropped(self):
        ctx = S.clean_context([{"me": True, "text": "   "}, {"me": False, "text": "x" * 5000}])
        self.assertEqual(len(ctx), 1)
        self.assertEqual(len(ctx[0][1]), S.MAX_CHARS_EACH)

    def test_when_the_last_message_is_mine_the_ask_is_a_follow_up(self):
        chat = _FakeChat("ok")
        _run(T.TextsAiReplyReq(messages=[T.TextsMsg(me=False, text="hi"),
                                         T.TextsMsg(me=True, text="want lunch?")]), chat, user=_User())
        self.assertIn("follow-up to my last message", chat.calls[0][-1]["content"])

    def test_labels_quotes_and_extra_paragraphs_are_stripped(self):
        self.assertEqual(S.clean_draft('Me: "Sounds good!"'), "Sounds good!")
        self.assertEqual(S.clean_draft("Sure thing.\n\n(Feel free to adjust.)"), "Sure thing.")
        self.assertEqual(len(S.clean_draft("y" * 5000)), S.MAX_REPLY_CHARS)


class RefusalTests(unittest.TestCase):
    def test_an_empty_conversation_is_refused_with_a_sentence(self):
        chat = _FakeChat("never")
        for msgs in ([], [T.TextsMsg(me=False, text="   ")]):
            code, out = _body(_run(T.TextsAiReplyReq(messages=msgs), chat, user=_User()))
            self.assertEqual(code, 400)
            self.assertIn("no message to reply to", out["error"])
        self.assertEqual(chat.calls, [])

    def test_a_user_without_ai_is_refused_before_the_model(self):
        chat = _FakeChat("never")
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3)), chat,
                               user=_User(ai=False), allowed=False))
        self.assertEqual(code, 403)
        self.assertIn("AI access", out["error"])
        self.assertEqual(chat.calls, [])

    def test_a_nostr_only_node_refuses_even_an_admin(self):
        chat = _FakeChat("never")
        with mock.patch.dict(os.environ, {"POSTERCHANAI_NOSTR_ONLY": "1"}):
            code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3)), chat, user=_User()))
            _, probe = _body(_run(T.TextsAiReplyReq(probe=True), chat, user=_User()))
        self.assertEqual(code, 403)
        self.assertFalse(probe["allowed"])
        self.assertEqual(chat.calls, [])

    def test_no_session_and_no_signature_is_401(self):
        chat = _FakeChat("never")
        code, _ = _body(_run(T.TextsAiReplyReq(messages=_thread(3)), chat))
        self.assertEqual(code, 401)
        self.assertEqual(chat.calls, [])

    def test_model_failure_is_a_sentence_not_a_blank(self):
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3)), _FakeChat(boom=True), user=_User()))
        self.assertEqual(code, 502)
        self.assertTrue(out["error"])
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3)), _FakeChat("   "), user=_User()))
        self.assertEqual(code, 502)


class NativeAuthTests(unittest.TestCase):
    """The phone's ThreadActivity: no session, a signed request instead."""

    def test_a_signed_member_request_is_answered(self):
        pk, auth = _signed()
        chat = _FakeChat("On my way")
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3), pubkey=pk, auth=auth),
                               chat, member=True))
        self.assertEqual(code, 200)
        self.assertEqual(out["content"], "On my way")

    def test_a_signed_non_member_is_refused(self):
        pk, auth = _signed()
        chat = _FakeChat("never")
        code, _ = _body(_run(T.TextsAiReplyReq(messages=_thread(3), pubkey=pk, auth=auth),
                             chat, member=False))
        self.assertEqual(code, 403)
        self.assertEqual(chat.calls, [])

    def test_a_proof_for_another_purpose_is_not_accepted(self):
        pk, auth = _signed("files-index")
        chat = _FakeChat("never")
        code, _ = _body(_run(T.TextsAiReplyReq(messages=_thread(3), pubkey=pk, auth=auth),
                             chat, member=True))
        self.assertEqual(code, 401)

    def test_probe_answers_without_calling_the_model(self):
        pk, auth = _signed()
        chat = _FakeChat("never")
        _, yes = _body(_run(T.TextsAiReplyReq(probe=True, pubkey=pk, auth=auth), chat, member=True))
        _, no = _body(_run(T.TextsAiReplyReq(probe=True, pubkey=pk, auth=auth), chat, member=False))
        self.assertEqual((yes["allowed"], no["allowed"]), (True, False))
        self.assertEqual(chat.calls, [])


class PrivacyTests(unittest.TestCase):
    def test_no_log_line_carries_the_conversation_or_the_draft(self):
        root = logging.getLogger()
        records = []

        class _Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _Grab(level=logging.DEBUG)
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        try:
            msgs = [T.TextsMsg(me=False, text=SECRET_IN)]
            _run(T.TextsAiReplyReq(messages=msgs), _FakeChat(SECRET_OUT), user=_User())
            _run(T.TextsAiReplyReq(messages=msgs), _FakeChat(boom=True), user=_User())
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        self.assertTrue(any("[texts-ai]" in r for r in records), records)   # it did log — sizes
        for r in records:
            self.assertNotIn("storage unit", r)
            self.assertNotIn("heading there", r)


class WiringTests(unittest.TestCase):
    def test_the_router_is_mounted(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "main.py"), encoding="utf-8").read()
        self.assertIn("app.include_router(texts_router.router)", src)

    def test_the_gate_is_nip05_access_not_a_new_rule(self):
        import inspect
        src = inspect.getsource(S.allowed)
        self.assertIn("nip05_access.ai_allowed", src)
        self.assertIn("nip05_access.is_member", src)


if __name__ == "__main__":
    unittest.main()


class ChoiceTests(unittest.TestCase):
    """"give you a few choices in maybe a menu popup": the web ✨ asks for 3 different drafts, made in
    ONE model call (the GPU is shared), parsed defensively, and `content` stays the first so the
    Android conversation screen — which reads that field alone — is unchanged."""

    def test_three_choices_from_one_model_call(self):
        chat = _FakeChat("1. Yes! See you at 7\n2. Sounds great, what should I bring?\n3) Can we make it 7:30?")
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(4), count=3), chat, user=_User()))
        self.assertEqual(code, 200)
        self.assertEqual(out["choices"], ["Yes! See you at 7", "Sounds great, what should I bring?", "Can we make it 7:30?"])
        self.assertEqual(out["content"], out["choices"][0])
        self.assertEqual(len(chat.calls), 1, "one model call for all the choices")
        self.assertIn("3 DIFFERENT", chat.calls[0][0]["content"])
        self.assertIn("never invent facts", chat.calls[0][0]["content"])

    def test_the_default_is_still_one_draft(self):
        chat = _FakeChat("Yes, see you at 8!")
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3)), chat, user=_User()))
        self.assertEqual((code, out["content"], out["choices"]), (200, "Yes, see you at 8!", ["Yes, see you at 8!"]))
        self.assertNotIn("DIFFERENT", chat.calls[0][0]["content"])

    def test_a_model_that_ignored_the_format_still_gives_its_one_reply(self):
        chat = _FakeChat("Sure, I'll be there")
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3), count=3), chat, user=_User()))
        self.assertEqual((code, out["choices"]), (200, ["Sure, I'll be there"]))

    def test_labels_quotes_duplicates_and_extras_are_cleaned(self):
        got = S.parse_choices('Here are some options:\n1. "Me: On my way"\n2. On my way\n- 3. Running late, sorry!\n'
                              '4. Be there soon\n5. extra', 3)
        self.assertEqual(got, ["On my way", "Running late, sorry!", "Be there soon"])

    def test_unnumbered_lines_are_still_separate_choices(self):
        # Measured 2026-10-08 on the node's model: asked for 5 numbered replies to a longer message, it wrote
        # four lines with NO numbers -- and the phone got one draft in the composer instead of a menu.
        out = ("Sure, I'll grab milk on the way. I'll call her too.\n"
               "No problem, milk's on me. I'll ring Mom later.\n"
               "Got it, I'll pick up milk and call her tonight\n"
               "Will do! Anything else you need from the store?")
        got = S.parse_choices(out, 5)
        self.assertEqual(len(got), 4, got)
        self.assertEqual(got[3], "Will do! Anything else you need from the store?")
        self.assertEqual(S.parse_choices("Here are some options:\n- On my way\n- Running late, sorry!", 5),
                         ["On my way", "Running late, sorry!"])
        self.assertEqual(S.parse_choices("**1.** On my way\n**2.** Be there soon", 5), ["On my way", "Be there soon"])

    def test_one_line_is_still_one_choice_and_one_asked_is_one_given(self):
        self.assertEqual(S.parse_choices("Sure, I'll be there", 5), ["Sure, I'll be there"])
        self.assertEqual(len(S.parse_choices("a line\nanother line", 1)), 1)

    def test_the_count_is_bounded(self):
        chat = _FakeChat("\n".join(f"{i}. option {i}" for i in range(1, 10)))
        code, out = _body(_run(T.TextsAiReplyReq(messages=_thread(3), count=50), chat, user=_User()))
        self.assertEqual(len(out["choices"]), S.MAX_CHOICES)


# ---- "give you a few choices for generate a reply based on the last message they sent" ----------------
def test_choices_answer_their_latest_message_by_name():
    from app.services.texts_ai_service import build_choice_messages
    ask = build_choice_messages([(False, "hi"), (False, "want pizza tonight?")], 3)[1]["content"]
    assert 'replies to their latest message: "want pizza tonight?"' in ask, ask
    assert ask.rstrip().endswith("My 3 options:")


def test_choices_still_answer_them_when_my_message_is_newest():
    """When MY message was newest the ask used to be 'a follow-up to my last message', so the options
    answered me rather than them."""
    from app.services.texts_ai_service import build_choice_messages
    ask = build_choice_messages([(False, "are you coming saturday?"), (True, "let me check")], 3)[1]["content"]
    tail = ask.split("TEXTS\n\n", 1)[1]
    assert 'their latest message: "are you coming saturday?"' in tail, tail
    assert '"let me check"' in tail and "do not repeat" in tail, tail
    assert "follow-up to my last message" not in tail, tail


def test_choices_keep_the_conversation_fenced():
    from app.services.texts_ai_service import build_choice_messages
    ask = build_choice_messages([(False, "x"), (True, "y")], 3)[1]["content"]
    assert ask.count("<<<TEXTS") == 1 and ask.count("\nTEXTS\n") == 1, ask
