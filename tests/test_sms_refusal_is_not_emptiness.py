""""No messages" and "I was not allowed to look" are different answers, on every surface.

    "i see 0 of my sms messages in Text"

`SmsStore.query` catches a SecurityException and returns an empty list, which is exactly what a phone
with no texts returns. `SmsStore.refused()` exists to tell them apart — and it was surfaced only in
`diagnose`, a panel nobody opens until they have already decided the app is broken. The READ path the
Texts screen is actually built on could not report it, so an empty list was drawn over a full inbox
with nothing to do about it.

This is the same rule as the drive check and the folder-sync deletion guard, in a third place: "the
store said no" and "the store could not be asked" are different, and only one of them is the user's
problem to fix.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java"
STORE = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsStore.java"
SMS_JS = ROOT / "static/js/client/sms.js"


def method(src, decl):
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


class TheStoreCanStillTellThemApart(unittest.TestCase):
    def setUp(self):
        self.src = STORE.read_text()

    def test_it_reports_a_refusal(self):
        self.assertIn("public static boolean refused()", self.src)

    def test_every_read_resets_it(self):
        """A flag only ever set to true latches: one refusal and the screen blames permissions for
        the rest of the session."""
        body = method(self.src, "private static List<SmsMsg> query")
        self.assertIn("refused = false;", body)
        self.assertLess(body.index("refused = false;"), body.index("refused = true;"))


class EveryReadCarriesIt(unittest.TestCase):
    """Not just `diagnose` — the methods the screen is built on."""

    def setUp(self):
        self.src = PLUGIN.read_text()

    def test_list_reports_it(self):
        self.assertIn('o.put("refused"', method(self.src, "public void list("))

    def test_threads_reports_it(self):
        self.assertIn('put("refused"', method(self.src, "public void threads("))

    def test_it_is_read_beside_its_own_query(self):
        """`refused` describes the LAST read; sampled later, another read could have overwritten it."""
        body = method(self.src, "public void threads(")
        self.assertLess(body.index("SmsStore.refused()"), body.index('out.put("threads"'),
                        "the refusal is sampled after the rows are serialised")


class TheScreenSaysWhichKindOfEmpty(unittest.TestCase):
    def setUp(self):
        self.src = SMS_JS.read_text()

    def test_the_loader_carries_it_out(self):
        body = self.src[self.src.index("async function loadFromPhone"):]
        body = body[:body.index("\n  }")]
        self.assertIn("answer.refused", body)
        self.assertIn("refused: refused", body)

    def test_a_refusal_on_any_attempt_counts(self):
        """The reader asks in strict history pages; a refusal on any page means the answer is not
        the phone's real total. Cleared inside the loop, only the last page would count."""
        body = self.src[self.src.index("async function loadFromPhone"):]
        body = body[:body.index("\n  }")]
        loop = body[body.index("for(let page = 0"):]
        self.assertNotIn("refused = false", loop,
                         "the flag is reset inside the loop, so only the last attempt counts")
        self.assertIn("if(answer.refused) refused = true;", loop)

    def test_the_ordinary_entry_path_reads_it_too(self):
        """The route almost everybody takes — opening Texts with the permission already granted —
        discarded `loadFromPhone`'s answer entirely. The Allow button had the message; the path to it
        did not, so the refusal was silent for anyone who had granted the permission elsewhere."""
        # The first await load() primes the encrypted cache before first paint. Inspect the phone
        # entry path specifically; otherwise adding unrelated cache-first work makes this guard
        # silently look at the wrong block.
        phone_entry = self.src.index("if(st.canRead){")
        i = self.src.index("await load();", phone_entry)
        seg = self.src[i:i + 900]
        # The RESULT MUST BE BOUND AND READ. Pinned to the `.then((r)` spelling this guard went red
        # when the entry path was made `await`, which is the same code doing the same thing — a
        # test that fails for a rewrite it does not care about teaches people to edit the test.
        self.assertIn("await loadFromPhone()", seg,
                      "the entry path no longer calls the loader")
        self.assertRegex(seg, r"(const|let)\s+r\s*=\s*await loadFromPhone\(\)",
                         "the entry path throws away the loader's result")
        self.assertIn("r.refused", seg)

    def test_it_does_not_overwrite_a_screen_that_has_messages(self):
        """A refusal on a later page of a sweep that already found messages must not blank the
        explanation over a list somebody is reading."""
        # ANCHORED AT THE ENTRY PATH, the way its sibling above is. There are several callers of
        # loadFromPhone and the first one in the file is the Allow BUTTON, which deliberately has no
        # such guard: somebody who just pressed Allow is told the provider still refused either way,
        # and `emptyWhy` is only ever drawn by the empty state (see the `No messages here yet`
        # fallback), so it cannot cover a list. Taking whichever call site happened to come first
        # made this guard assert the claim against the one place it was never about.
        entry = self.src.index("if(st.canRead){")
        i = self.src.index("const r = await loadFromPhone()", entry)
        self.assertIn("!S.msgs.size", self.src[i:i + 400])

    def test_granted_but_still_refused_is_its_own_message(self):
        """Android saying yes does not make the provider answer — a work profile or an OEM
        permission manager can still refuse."""
        i = self.src.index("const r = await loadFromPhone()")
        self.assertIn("r.refused", self.src[i:i + 600])
        self.assertIn("emptyWhy", self.src[i:i + 600])

    def test_header_allow_button_does_not_discard_provider_refusal(self):
        """The header's Allow button uses askForRead, not the empty-state button handler."""
        i = self.src.index("async function askForRead")
        seg = self.src[i:self.src.index("async function runBackfill", i)]
        self.assertIn("const r = await loadFromPhone()", seg)
        self.assertIn("r && r.refused", seg)


if __name__ == "__main__":
    unittest.main()
