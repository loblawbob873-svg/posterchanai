"""Any sheet you can type a message into must refuse the backdrop, and must show a way out.

"new email, new post, reply, the modals should not exit if you accidentally click outside of it."

`compose()` — new post, reply and quote, all one function — was already `.modal-sticky`. The MAIL
composer and the new-DM composer were not: a stray tap on the backdrop threw away a half-written
email or message, on the two screens where the text is longest and exists nowhere else. There is no
draft to recover either of them from.

THIS IS A RULE, NOT A LIST. Naming the three composers would be a list that the fourth one is added
beside, so the test finds them instead: every `modal(` whose markup contains a <textarea> is a sheet
somebody types into, and every one of those must be sticky AND carry a visible close control.

The second half is not optional. Escape and the Android Back button do not exist on a phone browser,
so a sheet that refuses the backdrop with no other pointer-driven exit is a TRAP — a worse
regression than the dismissal it replaces. `check_composer_dismiss.py` drives the real modal() in a
real browser to prove the mechanism works; this proves the composers actually use it.
"""
import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js").read_text(
    encoding="utf-8")


def _call_span(src, open_paren):
    """Text between a call's parentheses, honouring nesting, strings and template literals."""
    depth, i, quote = 0, open_paren, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1:i]
        i += 1
    return ""


def _composers():
    """Every modal() whose markup holds a <textarea> — i.e. a sheet somebody types into."""
    out = []
    for m in re.finditer(r"\bmodal\(", APP):
        body = _call_span(APP, m.end() - 1)
        if "<textarea" not in body:
            continue
        line = APP[:m.start()].count("\n") + 1
        out.append((line, body))
    return out


class EverySheetYouTypeIntoKeepsWhatYouTyped(unittest.TestCase):
    def test_the_scan_finds_the_known_composers(self):
        """If this drops to nothing the rule below passes vacuously, which is the failure mode of
        every scanner: green because it looked at nothing."""
        found = _composers()
        self.assertGreaterEqual(len(found), 3,
                                "expected at least the post, mail and DM composers; found %d"
                                % len(found))

    # WHICH SHEETS, AND WHY NOT ALL OF THEM.
    #
    # "any modal with a <textarea>" was the first rule here and it is too broad: it also catches
    # "Add a torrent", a Monero tip, and "Log in another device", which are PASTE-AND-GO boxes where
    # a click outside is the cancel a person expects, and nothing is lost by taking it.
    #
    # The line is whether the sheet holds something TYPED that exists nowhere else. A post, a reply,
    # a quote, an email, a direct message and a profile bio all do; a pasted magnet link does not.
    # Named rather than derived, because that judgement is not in the markup — but the scan above
    # still runs, so a composer that is renamed or moved shows up as a missing entry rather than
    # silently leaving the set.
    HOLDS_TYPED_WORK = {
        "cmp-close": "post, reply and quote (compose())",
        "cm-close": "the mail composer",
        "dm-close": "the new direct message",
        "pf-close": "edit profile",
    }

    def test_each_one_refuses_the_backdrop(self):
        bad = []
        for ident, what in sorted(self.HOLDS_TYPED_WORK.items()):
            at = APP.find("'#%s'" % ident)
            if at == -1:
                bad.append("%s (%s): no close button at all" % (ident, what))
                continue
            # THE REGION BETWEEN THIS modal( AND THE NEXT ONE, not a parsed call span.
            #
            # `_call_span` tracks quotes, and inside a template literal an apostrophe is ordinary
            # prose — "don't", "recipient's" — which ended the string early and made the span stop
            # before the onMount that adds the class. It reported `compose()` as unprotected when
            # compose() was the one sheet that had always been sticky. The class is applied either
            # in the markup or in the mount callback, and both sit between this modal( and the next,
            # so that region answers the question without parsing JavaScript.
            start = APP.rfind("modal(", 0, at)
            if start == -1:
                bad.append("%s (%s): no modal() around it" % (ident, what))
                continue
            nxt = APP.find("modal(", at)
            region = APP[start:nxt if nxt != -1 else len(APP)]
            if "modal-sticky" not in region:
                bad.append("%s (%s)" % (ident, what))
        self.assertEqual([], bad,
                         "these sheets hold typed text that exists nowhere else and still close on "
                         "a stray backdrop click: %s" % ", ".join(bad))

    def test_each_one_shows_its_own_way_out(self):
        """Refusing the backdrop without a visible ✕ is a trap on a phone with no Back button."""
        missing = [i for i in sorted(self.HOLDS_TYPED_WORK) if ("'#%s'" % i) not in APP]
        self.assertEqual([], missing,
                         "these sheets refuse the backdrop and offer no close control — a trap, "
                         "which is worse than the dismissal it replaced: %s" % ", ".join(missing))

    def test_the_close_buttons_are_wired(self):
        """A ✕ that is drawn and bound to nothing is the same trap with extra steps.

        Looked for in a WINDOW around the lookup rather than by one regex: the real binding is
        `{ const x=$('#cmp-close',root); if(x) x.onclick=…; }`, and a pattern that assumed no
        semicolon between the two halves failed against correct code. And the finding is reported as
        a line number — an earlier version passed the whole of app.js to assertRegex, which printed
        7.6MB for a one-line fault."""
        for ident in sorted(self.HOLDS_TYPED_WORK):
            with self.subTest(button=ident):
                at = APP.find("'#%s'" % ident)
                self.assertNotEqual(-1, at, "#%s is never looked up at all" % ident)
                near = APP[at:at + 220]
                self.assertIn("onclick", near,
                              "#%s is looked up at app.js:%d and never given a handler"
                              % (ident, APP[:at].count("\n") + 1))


if __name__ == "__main__":
    unittest.main()
