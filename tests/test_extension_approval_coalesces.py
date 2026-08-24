"""One approval prompt per permission, however many callers are waiting on it.

Run: venv-unified/bin/python -m pytest tests/test_extension_approval_coalesces.py

Signing in restores everything sealed to your own key at once — the notes library, the theme, the
client prefs, the settings — which is dozens of `nip44.decrypt` calls in the same tick, all of them
the same question about the same origin.

The extension caps concurrent approvals at three, for a good reason: a page can call signEvent in a
loop and without a cap that is two hundred windows in somebody's face. But past the cap it answered
`deny` — SILENTLY, without ever opening a prompt. So a fresh login came up with no notes and the
default theme, and looked exactly like the signer being broken. It was answering. Three times.

Reported as "no notes, not my theme, like the signer aint working" and "i did not see the pop up".

The fix is not a looser cap — it is asking the same question once. Identical requests wait on the
first one's answer, so a hundred copies of one question is one window rather than ninety-seven
silent denials.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = os.path.join(ROOT, "extension", "background.js")


def _fn(src, head):
    i = src.index(head)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{head} never closes")


class ApprovalsCoalesce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(BG, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.ask = _fn(cls.src, "async function _ask(")

    def test_an_identical_request_waits_instead_of_racing(self):
        self.assertIn("_askingByKey", self.ask,
                      "concurrent identical approvals still race the cap, so most of them are "
                      "denied without ever showing a prompt")
        # The wait must come BEFORE the cap, or the cap denies them first.
        body = re.sub(r"/\*.*?\*/", "", self.ask, flags=re.S)
        self.assertLess(body.index("_askingByKey.get"), body.index("open >= 3"),
                        "the coalescing check runs after the cap, which is the same bug with an "
                        "extra map")

    def test_the_cap_is_still_there(self):
        """Removing it would be two hundred windows with no gesture required."""
        self.assertIn("open >= 3", self.ask)

    def test_the_key_is_released_on_every_exit(self):
        """A key left behind makes every later request wait on a promise nobody will settle — the
        signer would then look permanently dead rather than merely unprompted."""
        self.assertIn("releaseKey", self.ask)
        # Both the answered path and the no-window path.
        self.assertGreaterEqual(self.ask.count("releaseKey"), 2)
        self.assertIn("answered.then(releaseKey, releaseKey)", self.src)

    def test_a_remembered_answer_still_short_circuits_before_any_of_this(self):
        body = re.sub(r"/\*.*?\*/", "", self.ask, flags=re.S)
        self.assertLess(body.index("if(perms[k]) return perms[k];"), body.index("_askingByKey.get"),
                        "a stored allow/deny must be honoured without waiting on anything")

    def test_both_builds_share_this_file(self):
        """extension/background-chrome.js is a thin shim; the logic must not be duplicated there, or
        the fix reaches one browser."""
        chrome = os.path.join(ROOT, "extension", "background-chrome.js")
        self.assertLess(os.path.getsize(chrome), 8000,
                        "background-chrome.js has grown into a second copy of the logic")


if __name__ == "__main__":
    unittest.main()
