"""The control that grants the SMS role is reachable when you have messages, not only when you don't.

    "there is no button"
    "POsterchan is not the this phones messaging app when i send message"

Those are the same fact from either end. Reading this phone's texts needs READ_SMS; SENDING needs the
ROLE. The role button lived in the EMPTY state only — so the moment somebody's messages appeared,
which is the moment they would try to reply, the one control that grants the role disappeared.
Exactly backwards.

Also here: the note under the header used to pick "an encrypted copy of your phone's messages … your
phone has to be reachable" whenever `phoneState()` answered `canRead:false`, which includes every way
that call can simply fail. A person holding their own phone, with their own texts on the screen
directly below, was told they were looking at a remote copy — a sentence contradicted by the thing it
sits on.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMS = ROOT / "static/js/client/sms.js"


def fn(src, decl):
    i = src.index(decl)
    j = src.index("{", i)
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1


class TheRoleCanBeAskedForWithMessagesOnScreen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SMS.read_text()

    def test_the_bar_is_in_the_main_markup(self):
        """Not inside the `S.msgs.size ? … : …` empty branch."""
        self.assertIn('id="sms-rolebar"', self.src)
        i = self.src.index('id="sms-rolebar"')
        j = self.src.index('<div class="sms-threads">')
        self.assertLess(i, j, "the role bar is below the thread list — it is in the empty state again")

    def test_it_is_bound_even_while_hidden(self):
        """`noteWhere` reveals it asynchronously, after the handlers are attached. A button revealed
        with no handler is the dead-button bug one layer along."""
        self.assertIn("PC.$('#sms-role2')", self.src)
        i = self.src.index("PC.$('#sms-role2')")
        self.assertIn("onclick", self.src[i:i + 160])

    def test_it_is_only_offered_where_it_can_work(self):
        """A tablet with no radio must never be asked; a phone that already holds it must not be
        asked again."""
        body = fn(self.src, "async function noteWhere")
        self.assertIn("sms-rolebar", body)
        self.assertIn("st.present && !st.isDefault", body)

    def test_there_is_one_implementation(self):
        """Two copies of a flow whose whole difficulty is what to say when it silently fails is how
        one copy ends up saying nothing."""
        self.assertEqual(self.src.count("P.requestSms()"), 1,
                         "the role request is implemented more than once")
        self.assertIn("async function askForRole", self.src)

    def test_both_buttons_use_it(self):
        for btn in ("#sms-role'", "#sms-role2'"):
            with self.subTest(button=btn):
                i = self.src.index("PC.$('" + btn.rstrip("'") + "'")
                self.assertIn("askForRole", self.src[i:i + 200])

    def test_the_result_is_re_read_not_believed(self):
        """Android refuses a role it will not grant by starting the request activity and finishing it
        instantly — no dialog, no error, indistinguishable from declining."""
        body = fn(self.src, "async function askForRole")
        self.assertIn("await phoneState()", body)
        self.assertLess(body.index("P.requestSms()"), body.index("await phoneState()"))


class TheNoteDoesNotContradictTheScreen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SMS.read_text()

    def test_a_local_read_is_remembered(self):
        self.assertIn("localRead", self.src)
        self.assertIn("S.localRead = true", self.src)

    def test_every_loader_sets_it(self):
        """Both paging loops read this device's own store."""
        self.assertEqual(self.src.count("S.localRead = true"), 2)

    def test_it_outranks_a_failed_state_query(self):
        body = fn(self.src, "async function noteWhere")
        self.assertIn("S.localRead", body)
        self.assertLess(body.index("S.localRead"), body.index("has to be reachable"),
                        "the remote-copy sentence is still chosen before the local fact is consulted")


if __name__ == "__main__":
    unittest.main()
