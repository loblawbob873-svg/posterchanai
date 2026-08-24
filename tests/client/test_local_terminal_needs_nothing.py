"""A shell on THIS machine never waits for a signer or a network.

Run: venv-unified/bin/python -m pytest tests/client/test_local_terminal_needs_nothing.py

On PosterChanOS the terminal is how somebody fixes a broken machine, so it must not depend on the
parts most likely to be broken. The local PTY is a process in the desktop: no key, no socket, no
server. Nothing about opening it needs an instance to answer.

That was not true. `loadHosts()` — which `render()` awaits BEFORE it opens anything — called
`PC.ensureAiSession()` and then fetched `/api/ssh/hosts`. Neither FAILS when the instance is
unreachable or the signer is asleep; both HANG. `ensureAiSession` can be waiting on a phone to
approve a signature, and there is no answer coming and no error either. So a broken signer meant no
local terminal at all, on a machine whose own shell was sitting right there.

The rule: when a local PTY exists, the host list is answered from it IMMEDIATELY and the server's
hosts arrive behind it. Every remaining call to the instance is bounded, because the failure this is
about is a request that never answers — a timeout is the only thing that turns it back into an
error.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TERM = os.path.join(ROOT, "static", "js", "client", "term.js")


def _decomment(js):
    """Comments are prose, not code. Twice now a guard has read the paragraph EXPLAINING why a call
    must not appear as the call appearing."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


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


class TheLocalShellNeedsNothing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TERM, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_a_local_pty_answers_the_host_list_without_asking_the_server(self):
        body = _decomment(_fn(self.src, "async function loadHosts()"))
        # The early return must come BEFORE anything that talks to the instance.
        early = body.index("if(LOCAL()){ hosts = _withLocal([]);")
        for call in ("ensureAiSession", "authFetch"):
            self.assertGreater(body.index(call), early,
                               f"loadHosts reaches {call} before answering from the local PTY, so a "
                               "hung signer or a dead network stops the local terminal opening")

    def test_the_calls_that_remain_cannot_hang_for_ever(self):
        body = _decomment(_fn(self.src, "async function loadHosts()"))
        for call in ("ensureAiSession", "authFetch"):
            for m in re.finditer(rf"await[^\n]*{call}[^\n]*", body):
                with self.subTest(call=m.group(0).strip()[:70]):
                    self.assertIn("_bounded", m.group(0),
                                  "an unbounded await on the instance: it does not fail when the "
                                  "server is unreachable, it hangs, and render() is waiting on it")

    def test_the_background_refresh_is_never_awaited_by_the_opener(self):
        """It exists precisely so nothing waits for it."""
        body = _fn(self.src, "async function loadHosts()")
        self.assertIn("_hostsRefresh();", body)
        self.assertNotIn("await _hostsRefresh", body)

    def test_history_is_skipped_without_a_key_rather_than_waiting_for_one(self):
        """Shell history is NIP-44 to your own key. With no key there is nothing to encrypt to — it
        must not become a reason the terminal does not open."""
        body = _decomment(_fn(self.src, "function _histStart()"))
        self.assertIn("if(!me) return;", body)
        head = body[:body.index("if(!me) return;")]
        self.assertNotIn("await", head, "_histStart waits on something before it checks for a key")

    def test_the_local_transport_opens_no_socket(self):
        """`_openLocal` is the whole point: a PTY in the desktop process, not a connection."""
        body = _decomment(_fn(self.src, "function _openLocal(frame)"))
        self.assertNotIn("new WebSocket", body)
        self.assertNotIn("authFetch", body)
        self.assertNotIn("ensureAiSession", body)


if __name__ == "__main__":
    unittest.main()
