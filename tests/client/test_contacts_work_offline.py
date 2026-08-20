"""Contacts open with no server, and a cached read may never delete anything.

"contacts should be like notes and passwords, work offline."

Notes and the vault paint what the client already holds and refresh behind it. Contacts went to the
network first and had nothing to show when it could not be reached — a whole screen reading "could
not reach your contacts" over an address book the device had already downloaded. It happened on an
ordinary service restart.

THE DANGEROUS HALF, and the reason this is not simply "cache it": the phone-book reconcile decides
what to DELETE from a handset from this same list. A keep-set built from a cache is a cache deciding
somebody's contacts, and that code has twice come close to emptying a real phone book. So a cached
read is marked unconfirmed, `loadedOk` is never set from it, and `partial` stays true — which is the
flag the sweep already refuses to delete under. A cached read can put a name on screen; it can never
take one off a phone.
"""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "contacts.js"


class ContactsWorkOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SRC.read_text()

    def _fn(self, name):
        i = self.src.index("function %s(" % name)
        j = self.src.index("{", i)
        depth, k = 0, j
        while k < len(self.src):
            if self.src[k] == "{":
                depth += 1
            elif self.src[k] == "}":
                depth -= 1
                if depth == 0:
                    return self.src[i:k + 1]
            k += 1
        raise AssertionError("%s has unbalanced braces" % name)

    def test_the_device_copy_is_painted_before_the_first_await(self):
        load = self._fn("load")
        cache = load.index("_loadCache()")
        first_await = load.index("await api(")
        self.assertLess(cache, first_await,
                        "the cached address book is read after the network call it exists to "
                        "survive")

    def test_a_cached_read_is_never_a_keep_set(self):
        """This is the line between a convenience and somebody losing their contacts."""
        cache = self._fn("_loadCache")
        self.assertIn("S.partial = true", cache,
                      "a cached load is not marked partial, so the phone sweep may delete from it")
        self.assertNotIn("S.loadedOk = true", cache,
                         "a cached load claims a completed load — the sweep will trust it")

    def test_only_a_whole_load_is_cached(self):
        """Caching a partial one would persist the short list this feature has twice nearly lost an
        address book to."""
        load = self._fn("load")
        m = re.search(r"if\(whole\)\{[^}]*_saveCache\(\)", load)
        self.assertIsNotNone(m, "the cache is written from a load that may have been partial")

    def test_a_server_read_clears_the_cached_flag(self):
        """Otherwise one offline visit would mark the session unconfirmed for ever, and the phone
        would stop applying deletions permanently."""
        load = self._fn("load")
        self.assertIn("S.fromCache = false", load)

    def test_a_failed_refresh_over_a_cached_book_is_not_an_error_screen(self):
        """The contacts are on screen; what failed is finding out whether they changed."""
        # Anchor on the ASSIGNMENT, not the first mention: `S.stale = ''` resets it at the top of
        # the function, and matching that asserts against the wrong line entirely.
        load = self._fn("load")
        i = load.index("S.stale = msg")
        self.assertIn("S.books.length", load[max(0, i - 200):i],
                      "the quiet notice is shown even when there is nothing on screen to caption — "
                      "with no contacts it should be a real error, not a caption over emptiness")

    def test_the_notice_sits_above_the_list_rather_than_replacing_it(self):
        self.assertIn("${staleBar}${head()}${list()}", self.src)
        self.assertIn("S.stale = ''", self.src, "the notice is never cleared when a refresh starts")


if __name__ == "__main__":
    unittest.main()
