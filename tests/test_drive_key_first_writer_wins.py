"""The account's drive key is FIRST-WRITER-WINS at the server, for ever.

Two fresh devices can both pull an empty index, both mint a master key, and both save; with
last-writer-wins on `mk` the account was silently re-keyed under whichever device saved LAST,
after the other had sealed thousands of blobs under its own — measured on a BRAND-NEW pair
(2026-08-18): the laptop could not open a single file the desktop had just uploaded. The server is
the only choke point every client build passes through, so the rule lives in /client/files-index:
an existing key is never replaced, the losing sender is told in the save answer, and the client
adopts on the spot."""
import inspect
import unittest

from app.routers import client as client_router


class FirstWriterWins(unittest.TestCase):
    def _src(self):
        return inspect.getsource(client_router.files_index)

    def test_an_existing_key_is_never_replaced(self):
        src = self._src()
        self.assertIn('data.index["mk"] = prev["mk"]', src,
                      "a save carrying a different mk re-keys the whole account")
        # the guard must run BEFORE the doc is stored
        self.assertLess(src.index('data.index["mk"] = prev["mk"]'),
                        src.index('put_doc(port, sk, "pcai:files-index"'))

    def test_the_loser_is_told_in_the_answer(self):
        src = self._src()
        self.assertIn('"mk": mk_kept', src,
                      "the losing device learns nothing until its next pull — and keeps sealing "
                      "uploads with the losing key meanwhile")

    def test_the_client_adopts_from_the_save_answer(self):
        app = open("static/js/client/app.js", encoding="utf-8").read()
        a = app.index("losing drive key")
        seg = app[a - 600:a + 900]
        self.assertIn("this._mkWrapped = jr.mk", seg)
        self.assertIn("this.mk = null", seg, "the unwrapped cache survives the adopt — decrypts "
                                             "keep using the losing key for the session")
        self.assertIn("saveLocal()", seg)


if __name__ == "__main__":
    unittest.main()
