"""The ✨ AI menu on an open email: Summarize, AI reply, Add to Budget.

The rule every entry obeys: THE MODEL ONLY EVER PRODUCES TEXT THE USER THEN REVIEWS. A summary is
read, a reply draft opens in the composer with Send untouched, a bill parse opens Budget's editable
review modal. Nothing in this feature sends mail, files a bill, or acts on a message by itself —
and these tests are what keeps that a property rather than a phase.

Three layers: the /api/mail/ai endpoint RUN with the model stubbed; the bill pipeline RUN with a
text attachment (an email is a bill that never needed OCR — same pipeline, same prompt); and the
client wiring pinned structurally, because the row/menu split is exactly the kind of thing a
refactor flattens back into seven buttons.
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("POSTERCHANAI_SKIP_DB", "1")

from fastapi import HTTPException  # noqa: E402

from app.routers import mail as M  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeChat:
    def __init__(self, answer="", boom=False):
        self.answer, self.boom, self.calls = answer, boom, []

    async def chat(self, msgs):
        self.calls.append(msgs)
        if self.boom:
            raise RuntimeError("model down")
        return self.answer


def _run(req, chat):
    class _CS:
        def __init__(self, db, user=None):
            self.chat_service = chat
    with mock.patch("app.services.command_service.CommandService", _CS):
        return asyncio.run(M.mail_ai(req, db=None, current_user=object()))


class EndpointTests(unittest.TestCase):
    def test_summarize_returns_the_models_text(self):
        chat = _FakeChat("• a bill\n• due friday")
        out = _run(M.MailAiReq(mode="summarize", text="Subject: x\n\nhello"), chat)
        self.assertEqual(out["content"], "• a bill\n• due friday")
        self.assertEqual(len(chat.calls), 1)

    def test_reply_carries_the_email_first_and_the_instruction_last(self):
        """Order is the fix, not a style: instruction-first made the recency-biased local model
        CONTINUE the email — a payroll summary came back verbatim as "the reply". The email is
        fenced as quoted material and the instruction lands last, next to an explicit cue."""
        chat = _FakeChat("No thank you.")
        out = _run(M.MailAiReq(mode="reply", text="Subject: offer\n\nbuy now",
                               instruction="politely decline"), chat)
        self.assertEqual(out["content"], "No thank you.")
        user_msg = chat.calls[0][-1]["content"]
        self.assertIn("politely decline", user_msg)
        self.assertIn("buy now", user_msg)
        self.assertLess(user_msg.index("buy now"), user_msg.index("politely decline"),
                        "the instruction drifted back above the email — the parrot returns")
        self.assertIn("<<<EMAIL", user_msg, "the email lost its fence")

    def test_the_instruction_is_marked_as_conveyance_not_content(self):
        """"Thanks!" as the instruction came back as literally "Thanks!" — the parrot one level up
        from echoing the email. The prompt marks the instruction as what to CONVEY and demands a
        response that refers to what the sender wrote."""
        chat = _FakeChat("ok")
        _run(M.MailAiReq(mode="reply", text="hi there", instruction="Thanks!"), chat)
        sys_msg = chat.calls[0][0]["content"]
        self.assertIn("CONVEY", sys_msg)
        self.assertIn("refers to what the sender actually wrote", sys_msg)
        self.assertIn("What it should convey:", chat.calls[0][-1]["content"])

    def test_a_placeholder_signature_is_stripped_no_matter_what_the_model_thinks(self):
        """A rule a model follows most of the time is a rule; a line of code is a guarantee."""
        chat = _FakeChat("Thanks for these!\n\nBest,\n[Your Name]")
        out = _run(M.MailAiReq(mode="reply", text="hi", instruction="thanks"), chat)
        self.assertEqual(out["content"], "Thanks for these!")
        chat2 = _FakeChat("All good.\n[Name]")
        out2 = _run(M.MailAiReq(mode="reply", text="hi", instruction="ok"), chat2)
        self.assertEqual(out2["content"], "All good.")
        # …and a signature is kept only when the name was GIVEN — with no myName, "Dustin" is as
        # unverifiable as "Jordan", and both go.
        chat3 = _FakeChat("On it.\n\nBest,\nDustin")
        out3 = _run(M.MailAiReq(mode="reply", text="hi", instruction="ok", myName="Dustin"), chat3)
        self.assertEqual(out3["content"], "On it.\n\nBest,\nDustin")

    def test_commitments_may_only_come_from_the_instruction(self):
        """"I've called the front desk and scheduled the Mid-Year Review for next Tuesday at 10 AM"
        — a fabricated past-tense action over a vague "will do". The rule is in the prompt; probed
        live 3/3 on the exact bait before landing."""
        chat = _FakeChat("ok")
        _run(M.MailAiReq(mode="reply", text="hi", instruction="will do"), chat)
        self.assertIn("NEVER INVENT COMMITMENTS", chat.calls[0][0]["content"])

    def test_the_signoff_is_grounded_or_absent(self):
        chat = _FakeChat("ok")
        _run(M.MailAiReq(mode="reply", text="hi", instruction="ok", myName="Dustin"), chat)
        self.assertIn("sign exactly as: Dustin", chat.calls[0][0]["content"])
        chat2 = _FakeChat("ok")
        _run(M.MailAiReq(mode="reply", text="hi", instruction="ok"), chat2)
        self.assertIn("Do not add any sign-off", chat2.calls[0][0]["content"])

    def test_an_invented_name_is_stripped_when_no_name_was_given(self):
        """"Best, Jordan" — signed as somebody who does not exist. With no myName, any trailing
        valediction+name block is an invention and code removes it; with a name given, a real
        signature is left alone."""
        chat = _FakeChat("On it, thanks!\n\nBest,\nJordan")
        out = _run(M.MailAiReq(mode="reply", text="hi", instruction="ok"), chat)
        self.assertEqual(out["content"], "On it, thanks!")
        chat2 = _FakeChat("On it, thanks!\n\nBest,\nDustin")
        out2 = _run(M.MailAiReq(mode="reply", text="hi", instruction="ok", myName="Dustin"), chat2)
        self.assertEqual(out2["content"], "On it, thanks!\n\nBest,\nDustin")

    def test_the_client_grounds_the_name_from_the_to_header(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        at = app.index("async aiReply(msg")
        body = app[at:at + 2200]
        self.assertIn("myName", body)
        self.assertIn("msg.to", body, "the name comes from somewhere other than the To header")

    def test_drafting_runs_at_task_temperature(self):
        chat = _FakeChat("ok")
        _run(M.MailAiReq(mode="reply", text="hi", instruction="ok"), chat)
        self.assertEqual(getattr(chat, "temperature", None), 0.2,
                         "the drafting call runs at chat temperature and varies run to run")

    def test_the_to_line_travels_with_the_message(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        at = app.index("_msgText(msg){")
        self.assertIn("msg.to", app[at:at + 1200],
                      "without To: the model cannot know the user's name and invents placeholders")

    def test_a_reply_with_no_instruction_is_refused(self):
        with self.assertRaises(HTTPException) as c:
            _run(M.MailAiReq(mode="reply", text="hi"), _FakeChat("x"))
        self.assertEqual(c.exception.status_code, 400)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(HTTPException) as c:
            _run(M.MailAiReq(mode="delete-everything", text="hi"), _FakeChat("x"))
        self.assertEqual(c.exception.status_code, 400)

    def test_an_empty_message_is_refused_before_the_model_is_asked(self):
        chat = _FakeChat("x")
        with self.assertRaises(HTTPException):
            _run(M.MailAiReq(mode="summarize", text="   "), chat)
        self.assertEqual(chat.calls, [], "the model was asked about an empty message")

    def test_a_dead_model_is_a_502_not_a_traceback(self):
        with self.assertRaises(HTTPException) as c:
            _run(M.MailAiReq(mode="summarize", text="hi"), _FakeChat(boom=True))
        self.assertEqual(c.exception.status_code, 502)

    def test_the_prompt_never_asks_the_model_to_act(self):
        """The system prompts describe producing text. If someone later wires tools into this
        endpoint, this is the line that asks them to look up."""
        chat = _FakeChat("ok")
        _run(M.MailAiReq(mode="reply", text="hi", instruction="say hi"), chat)
        import re
        sys_msg = chat.calls[0][0]["content"].lower()
        for word in ("send", "delete", "file"):
            self.assertIsNone(re.search(r"\b%s\b" % word, sys_msg),
                              "the reply prompt speaks of %r" % word)


class BillFromTextTests(unittest.TestCase):
    """An email is a bill that never needed OCR: a text attachment feeds the same extraction."""

    def _cs(self, answer):
        from app.services.command_service import CommandService
        cs = CommandService.__new__(CommandService)
        cs.user = type("U", (), {"id": 1})()
        cs.db = None
        cs.chat_service = _FakeChat(answer)
        return cs

    def test_a_text_attachment_reaches_the_extraction(self):
        cs = self._cs('{"vendor": "Water Co", "amount": 43.10, "due_date": "2026-09-01"}')
        out = asyncio.run(cs._bill_command(
            "", attachments=[("email.txt", b"Subject: bill\n\nWater Co total $43.10 due Sep 1", "text/plain")]))
        self.assertEqual(out["type"], "bill", out)
        self.assertEqual(out["vendor"], "Water Co")
        self.assertEqual(out["amount"], 43.10)

    def test_the_email_text_is_what_the_model_reads(self):
        cs = self._cs('{"vendor": "V", "amount": 1, "due_date": null}')
        asyncio.run(cs._bill_command(
            "", attachments=[("email.txt", b"pay the piper", "text/plain")]))
        self.assertIn("pay the piper", cs.chat_service.calls[0][-1]["content"])

    def test_no_attachment_still_asks_for_one(self):
        cs = self._cs("")
        out = asyncio.run(cs._bill_command("", attachments=None))
        self.assertEqual(out["type"], "text")
        self.assertIn("Attach", out["content"])


class ClientWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8") as fh:
            cls.app = fh.read()

    def test_the_row_has_one_ai_button_and_the_actions_live_in_the_menu(self):
        """The row is a grid; every action added AS A BUTTON costs a phone a column. The mobile
        check counts 7 buttons, so an eighth fails there — this pins the other half: the three AI
        actions live behind the one ✨ entry."""
        self.assertIn('data-act="ai"', self.app)
        self.assertNotIn('data-act="bill"', self.app, "Add to Bills grew back into a row button")
        at = self.app.index("if(act==='ai')")
        menu = self.app[at:at + 900]
        for entry in ("Summarize this email", "AI reply", "Add to Budget"):
            self.assertIn(entry, menu, "the ✨ menu lost its '%s' entry" % entry)

    def test_the_reply_draft_lands_in_the_composer_not_in_an_outbox(self):
        at = self.app.index("async aiReply(msg")
        body = self.app[at:at + 1600]
        self.assertIn("this.compose({ mode:'reply'", body)
        self.assertNotIn("/send", body, "the AI reply path sends mail by itself")

    def test_compose_honours_the_drafted_body_only_in_reply_modes(self):
        self.assertIn("if(opts.body) body=String(opts.body);", self.app)

    def test_html_mail_is_reduced_with_domparser_not_the_iframe(self):
        at = self.app.index("_msgText(msg){")
        body = self.app[at:at + 900]
        self.assertIn("DOMParser", body)
        self.assertNotIn("contentDocument", body,
                         "the sandboxed mail iframe has no reachable document; reading it returns "
                         "nothing and every HTML mail would summarize as empty")

    def test_every_ai_action_shows_its_wait_and_lands_failures_somewhere(self):
        """An AI call from mail is a signer round trip plus a possibly-cold model — up to a minute.
        A two-second toast in front of that reads as "nothing happened" (reported: "no AI requests
        are working"). Each action opens a holding modal before the network, and its catch writes
        into that modal, not into the void."""
        self.assertIn("_aiHold(", self.app)
        for fn in ("aiSummarize", "aiReply", "addToBills"):
            at = self.app.index(fn + "(msg")
            body = self.app[at:at + 1600]
            self.assertIn("_aiHold(", body, fn + " gives no feedback for the whole wait")
            self.assertIn("hold.fail", body, fn + " loses its failure")

    def test_the_bill_path_rides_the_same_scan_endpoint(self):
        at = self.app.index("async addToBills(msg){")
        body = self.app[at:at + 1400]
        self.assertIn("/api/budget/scan", body)
        self.assertIn("email.txt", body)
        self.assertIn("reviewParsed", body, "the parse skips Budget's editable review")

    def test_budget_exports_the_review_modal(self):
        with open(os.path.join(ROOT, "static", "js", "client", "budget.js"), encoding="utf-8") as fh:
            self.assertIn("reviewParsed", fh.read())

    def test_the_review_modal_survives_a_signer_that_does_not_answer(self):
        """It used to closeModal() and THEN await load() — which waits on the signer to decrypt
        the doc. A signer that never answered left a blank screen, a lost edit, and an unhandled
        rejection: 'Add to budget never does anything'. The modal must close on SUCCESS only."""
        with open(os.path.join(ROOT, "static", "js", "client", "budget.js"), encoding="utf-8") as fh:
            src = fh.read()
        at = src.index("#bg-aok")
        body = src[at:at + 1800]
        close = body.index("closeModal()")
        load = body.index("await load()")
        self.assertLess(load, close, "the modal closes before the save path has even started")
        self.assertIn("catch", body[:close + 400], "a failed save has nowhere to land")
        self.assertIn("nothing was written", body, "a failure does not say whether anything was saved")


if __name__ == "__main__":
    unittest.main()
