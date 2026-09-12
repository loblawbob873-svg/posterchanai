"""A tapback must reach the SCREEN, not just the module that understands it.

`sms-reactions.js` could parse and project SMS fallback reactions, was covered by its own tests,
and was loaded by NOTHING — no template, no bundle, and `sms.js` never mentioned the word. So on
the web Texts screen every reaction an iPhone sent still arrived as its own bubble reading
`Loved “your message”`, and a long thread with an iPhone user was half tapback noise. Reported as
"SMS app on android is still missing reactions like ios/android have"; the NATIVE Texts app has
had them since it shipped (`SmsReactions.java`), which is exactly what made the gap invisible.

The module also had no `format`, so the web half could only ever READ a reaction and never send
one. That half has to be byte-identical to the Java or a tapback lands on the other phone as a
line of prose — asserted across both languages below.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "static/js/client/sms-reactions.js"
SMS_JS = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsReactions.java"


def node(script):
    done = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


class TheModuleReachesTheScreen(unittest.TestCase):

    def test_it_is_loaded_by_every_shell_that_loads_the_texts_screen(self):
        """A module wired to nothing is the bug. Both shells that ship sms.js must ship this too,
        and BEFORE it — sms.js reads `window.PCSmsReactions` while painting."""
        for page in ("templates/client.html",
                     "mobile/android/app/src/main/assets/public/index.html"):
            html = (ROOT / page).read_text(encoding="utf-8")
            self.assertIn("sms-reactions.js", html, page)
            self.assertLess(html.index("sms-reactions.js"), html.index("client/sms.js"),
                            page + ": sms.js loads before the module it uses")

    def test_the_screen_actually_calls_it(self):
        self.assertIn("PCSmsReactions", SMS_JS)
        self.assertIn("reactionsFor", SMS_JS)
        self.assertIn("chipsHtml", SMS_JS)

    def test_a_reaction_row_is_removed_before_grouping_not_hidden(self):
        """`grp`/`cont` is computed from the PREVIOUS bubble, so a consumed row left in the list
        would break the run-of-messages spacing around every tapback while drawing nothing."""
        self.assertIn("_shown", SMS_JS)
        self.assertIn("const prev = _shown[i-1];", SMS_JS)

    def test_the_chip_styles_are_scoped_to_texts(self):
        """`.bubble` is the DM's. Adding `position` to it would move every absolutely positioned
        thing on the DM screen as well."""
        self.assertIn(".sms-msgs .bubble{position:relative", CSS)
        self.assertNotRegex(CSS, r"\n\.bubble\{[^}]*position:relative")


class TheProjection(unittest.TestCase):
    """Run the SHIPPED module over a thread shaped the way sms.js shapes one."""

    def _project(self, rows, complete=True):
        script = (
            "const R=require(%s);"
            "const out=R.project(%s,%s);"
            "console.log(JSON.stringify({chips:out.chipsByTarget,consumed:out.consumedIds}));"
        ) % (json.dumps(str(MODULE)), json.dumps(rows), "true" if complete else "false")
        return json.loads(node(script))

    def _row(self, rid, body, date, incoming, actor="+15551234567"):
        return {"id": rid, "thread": "5551234", "actor": actor, "body": body, "date": date,
                "incoming": incoming, "eligible": True, "group": False}

    def test_a_tapback_becomes_a_chip_and_stops_being_a_message(self):
        rows = [self._row("a", "see you at 8", 100, False, "me"),
                self._row("b", "Loved “see you at 8”", 200, True)]
        out = self._project(rows)
        self.assertEqual(out["consumed"], ["b"], "the tapback still renders as a message")
        self.assertEqual(len(out["chips"]["a"]), 1)
        self.assertEqual(out["chips"]["a"][0]["emoji"], "❤️")

    def test_taking_one_back_removes_the_chip(self):
        rows = [self._row("a", "see you at 8", 100, False, "me"),
                self._row("b", "Liked “see you at 8”", 200, True),
                self._row("c", "Removed a like from “see you at 8”", 300, True)]
        out = self._project(rows)
        self.assertEqual(out["chips"], {}, "a removed reaction is still shown")
        self.assertEqual(sorted(out["consumed"]), ["b", "c"])

    def test_an_ambiguous_target_stays_an_ordinary_message(self):
        """Two identical messages: nothing can say which was reacted to, so it must stay text
        rather than attach to the wrong one."""
        rows = [self._row("a", "ok", 100, False, "me"),
                self._row("a2", "ok", 150, False, "me"),
                self._row("b", "Liked “ok”", 200, True)]
        out = self._project(rows)
        self.assertEqual(out["consumed"], [])
        self.assertEqual(out["chips"], {})

    def test_incomplete_history_projects_nothing(self):
        """A reaction whose target has not loaded must not attach to whatever else matches."""
        rows = [self._row("a", "hi", 100, False, "me"),
                self._row("b", "Liked “hi”", 200, True)]
        self.assertEqual(self._project(rows, complete=False)["consumed"], [])

    def test_prose_that_merely_looks_like_one_is_left_alone(self):
        rows = [self._row("a", "Liked it a lot", 100, True),
                self._row("b", 'Loved "unclosed', 200, True)]
        out = self._project(rows)
        self.assertEqual(out["consumed"], [])


class SendingOne(unittest.TestCase):

    def test_the_web_half_can_send_at_all(self):
        """It could only ever READ one before: `format` lived in Java and nowhere else."""
        out = node("const R=require(%s);console.log(R.format('heart',false,'see you at 8'));"
                   % json.dumps(str(MODULE)))
        self.assertEqual(out.strip(), "Loved “see you at 8”")

    def test_it_is_byte_identical_to_the_java(self):
        """The curly quotes are the interoperable form. A mismatch here means the tapback lands on
        the other phone as a line of prose — silently, and only on real hardware."""
        forms = re.findall(r'\{"(\w+)","([^"]+)","([^"]+)","([^"]+)"\}',
                           JAVA.read_text(encoding="utf-8"))
        self.assertEqual(len(forms), 6, "the Java table changed shape")
        script = ["const R=require(%s);const out=[];" % json.dumps(str(MODULE))]
        for kind, _emoji, _add, _undo in forms:
            for remove in ("false", "true"):
                script.append("out.push(R.format(%s,%s,'a b'));" % (json.dumps(kind), remove))
        script.append("console.log(JSON.stringify(out));")
        got = json.loads(node("".join(script)))
        want = []
        for kind, _emoji, add, undo in forms:
            want.append(add + " “a b”")
            want.append(undo + " “a b”")
        self.assertEqual(got, want)

    def test_every_form_round_trips_through_the_parser(self):
        script = ("const R=require(%s);const bad=[];"
                  "for(const f of R.forms)for(const rm of [false,true]){"
                  "const s=R.format(f.kind,rm,'hello there');const p=R.parse(s);"
                  "if(!p||p.kind!==f.kind||p.text!=='hello there'"
                  "||p.operation!==(rm?'remove':'add'))bad.push(s);}"
                  "console.log(JSON.stringify(bad));") % json.dumps(str(MODULE))
        self.assertEqual(json.loads(node(script)), [])

    def test_it_refuses_what_it_cannot_send(self):
        script = ("const R=require(%s);const out=[];"
                  "try{R.format('duck',false,'x');out.push('kind-allowed');}catch(e){}"
                  "try{R.format('heart',false,'');out.push('empty-allowed');}catch(e){}"
                  "console.log(JSON.stringify(out));") % json.dumps(str(MODULE))
        self.assertEqual(json.loads(node(script)), [])

    def test_the_picker_offers_only_the_six_that_can_be_sent(self):
        """The full emoji picker would let somebody choose an emoji the protocol has no words for,
        which goes out as an ordinary message rather than a reaction."""
        self.assertIn("sms-react-pick", SMS_JS)
        self.assertNotIn("openEmojiPopover(btn", SMS_JS.split("data-sms-react")[1][:1200])
        kinds = json.loads(node("const R=require(%s);console.log(JSON.stringify("
                                "R.forms.map(f=>f.kind)));" % json.dumps(str(MODULE))))
        self.assertEqual(kinds, ["heart", "like", "dislike", "laugh", "emphasize", "question"])


if __name__ == "__main__":
    unittest.main()
