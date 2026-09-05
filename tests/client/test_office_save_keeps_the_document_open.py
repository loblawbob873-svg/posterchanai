"""SAVE SAVES. IT DOES NOT CLOSE.

The Office Save button ended in `drop()` + `shut()` -- deleting the server-side editing session and
closing the window -- so every Save ended the edit. Reported as "saving an office document should
not close it too". Nothing about writing the bytes back requires ending the session, and somebody
who saves mid-document is saying the opposite: they intend to carry on.

Closing is still one button away, and THAT path still drops the session -- which matters, because an
editor closed without it leaks a server-side document for the whole session TTL.

`origBytes` has to move with the save. The "no changes to save" check is not cosmetic: an unchanged
upload re-points the drive index at a new blob, which is why identical bytes are refused. Leaving
`origBytes` at the file's ORIGINAL contents would make the second Save of a document you are still
editing compare against the wrong thing.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def save_handler(code_only=True):
    """The handler. `code_only` strips comments -- the note explaining why Save no longer closes
    necessarily contains the words `drop()` and `shut()`, and a test that reads its own explanation
    as evidence proves nothing."""
    body = APP[APP.index("$('#office-save',root).onclick="):]
    body = body[: body.index("\n        };") + 10]
    if not code_only:
        return body
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))


class TestSaveDoesNotEndTheSession(unittest.TestCase):
    def test_it_does_not_close_the_window(self):
        self.assertNotIn("shut()", save_handler(),
                         "Save still closes the document it just saved")

    def test_it_does_not_delete_the_editing_session(self):
        self.assertNotIn("drop()", save_handler(),
                         "Save still drops the server-side session, so editing cannot continue")

    def test_the_button_comes_back(self):
        """It is disabled and relabelled 'Saving…' while it works; a Save that returns must give the
        button back or the document can only ever be saved once."""
        body = save_handler()
        self.assertGreaterEqual(body.count("b.disabled=false"), 3,
                                "some path leaves the Save button disabled for ever")

    def test_an_unchanged_document_still_refuses_to_upload(self):
        """An unchanged upload mints a blob and re-points the drive index at it."""
        self.assertIn("_sameBytes(bytes, origBytes)", save_handler())

    def test_the_baseline_moves_with_the_save(self):
        body = save_handler()
        self.assertIn("origBytes = bytes", body,
                      "the second Save would compare against the file's original contents")
        self.assertLess(body.index("saveBack("), body.index("origBytes = bytes"),
                        "the baseline moves before the bytes are known to have landed")


class TestCloseStillCleansUp(unittest.TestCase):
    def test_the_close_button_drops_the_session_and_shuts(self):
        line = [l for l in APP.splitlines() if "$('#office-close',root).onclick" in l][0]
        self.assertIn("drop()", line, "closing leaks a server-side document for the whole TTL")
        self.assertIn("shut()", line)


if __name__ == "__main__":
    unittest.main()
