"""A card whose referenced event never arrives must ask again.

Run: venv-unified/bin/python -m pytest tests/client/test_need_event_retry.py

Reported twice, the second time as "tablet is doing that timeline bug again where no posts are shown
but you see REPLYING TO ...".

That description is precise about which half fails. The "↩ replying to …" header is built from the
reply's OWN tags, so it always renders; the body underneath comes from an event that has to be
FETCHED (`needEvent` → `flushEvents`), and when that fetch comes back empty the card stays a shell
for ever, because nothing asked again. One lost query — a socket the OS froze while the app was
backgrounded, a relay that answered nothing before the timeout, a reconnect landing mid-flight — and
the feed sits half-drawn. On a desktop something usually repaints and re-queues; on a tablet left on
one screen, nothing does.

Reported a third time, from the Android APK: "bring the app back from the background and every card
is a placeholder; real posts take a very long time to appear, or never do without a reload". The
retry existed by then — it was the GIVE-UP that was wrong. Retrying is bounded because "no relay we
hold has it" is a real answer, but a frozen socket does not give that answer, it gives none at all:
relay.js drops a REQ written to a socket that is not OPEN, and a socket the OS thawed reads OPEN
while being dead, so either way `query` resolves empty on its timer. Counted as a refusal, ~40s in a
pocket spends the whole budget, and the id is abandoned for the life of the page — the redraw on
resume buys exactly one more attempt, which the still-thawing socket eats too.

The real `flushEvents` is run under node against a stubbed relay, so what is asserted is the
BEHAVIOUR — it asks again, it stops asking when the relays have ANSWERED that many times, it does not
stop because nobody answered, and a rejection is not fatal — rather than the presence of a retry.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"


def _closing_brace(src, open_idx):
    """Index of the `}` matching the `{` at open_idx."""
    depth = 0
    for j in range(open_idx, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    raise AssertionError("unbalanced braces from offset %d" % open_idx)


def _extract(src, decl):
    i = src.index(decl)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced braces extracting " + decl)


def _harness(script):
    src = APP.read_text(encoding="utf-8")
    for decl in ("async function flushEvents(){",):
        assert decl in src, f"{decl!r} is gone from app.js — this test is testing nothing"
    parts = [
        re.search(r"^\s*(const _evQ=new Set\(\); let _evT=null; const _evTries=new Map\(\);)",
                  src, re.M).group(1),
        re.search(r"^\s*(const _EV_TRIES_MAX = \d+;)", src, re.M).group(1),
        re.search(r"^\s*(const _EV_STALL_MAX = \d+;)", src, re.M).group(1),
        re.search(r"^\s*(const _evStalls = new Map\(\);)", src, re.M).group(1),
        re.search(r"^\s*(function needEvent\(id\)\{.*?\})\s*$", src, re.M | re.S).group(1).split("\n")[0],
        _extract(src, "async function flushEvents(){"),
    ]
    boot = """
// Stubs for everything flushEvents touches but this test is not about.
const saved = new Map();
const Store = { get: id => saved.get(id) || null, saveEvent: e => saved.set(e.id, e) };
const needProfile = () => {};
const patchLoaded = () => {};
const decorateProfiles = () => {};
let ASKS = [];
let ANSWER = () => [];
// `complete` is the real query()'s marker for "every relay we asked EOSE'd", and flushEvents now
// reads it, so the stub has to answer it honestly. Default true = the relays SPOKE and did not have
// it; `answer(ids, false)` is a query nobody answered (frozen socket / nothing open to ask).
const answer = (evs, complete = true) => {
  try { Object.defineProperty(evs, 'complete', { value: complete, enumerable: false, configurable: true }); } catch (_) {}
  return evs;
};
let LIVE = true;
const Relay = {
  ready: async () => LIVE,
  query: async (f) => { ASKS.push(f[0].ids.slice()); return answer(ANSWER(f[0].ids)); },
};
"""
    return subprocess.run(
        ["node", "-e", boot + "\n".join(parts) + "\n" + script],
        capture_output=True, timeout=180)


def _run(script):
    out = _harness(script)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NeedEventRetry(unittest.TestCase):

    def test_a_missing_event_is_asked_for_again(self):
        """The whole bug in one assertion: an empty answer used to be the end of it."""
        got = _run("""
        (async () => {
          needEvent('a'.repeat(64));
          await new Promise(r => setTimeout(r, 250));      // let the 150ms debounce fire
          await new Promise(r => setTimeout(r, 1200));     // …and the first retry
          console.log(JSON.stringify({ asks: ASKS.length }));
        })();""")
        self.assertGreaterEqual(got["asks"], 2, "a lost fetch was never retried")

    def test_it_stops_asking_eventually(self):
        """"Not on any relay we are connected to" is a real and common answer. Retrying it for ever
        would turn a feed full of unreachable references into a query loop."""
        got = _run("""
        (async () => {
          needEvent('b'.repeat(64));
          await new Promise(r => setTimeout(r, 40000));
          console.log(JSON.stringify({ asks: ASKS.length, max: _EV_TRIES_MAX }));
        })();""")
        self.assertLessEqual(got["asks"], got["max"] + 1,
                             f"it kept asking: {got['asks']} times")

    def test_an_answer_stops_the_retries(self):
        got = _run("""
        (async () => {
          const id = 'c'.repeat(64);
          ANSWER = (ids) => ids.map(i => ({ id: i, pubkey: 'p' }));
          needEvent(id);
          await new Promise(r => setTimeout(r, 3000));
          console.log(JSON.stringify({ asks: ASKS.length, tries: _evTries.size, has: !!Store.get(id) }));
        })();""")
        self.assertEqual(got["asks"], 1, "it asked again for something it already had")
        self.assertTrue(got["has"])
        self.assertEqual(got["tries"], 0, "the attempt counter was not cleared on success")

    def test_a_rejected_query_is_not_fatal(self):
        """Unhandled, a rejection killed the whole flush — every id in that batch went unasked AND
        unqueued, which is the same shell-card outcome with no retry at all."""
        got = _run("""
        (async () => {
          Relay.query = async () => { throw new Error('no relay is up'); };
          needEvent('d'.repeat(64));
          await new Promise(r => setTimeout(r, 2500));
          console.log(JSON.stringify({ queued: _evQ.size + _evTries.size }));
        })();""")
        self.assertGreater(got["queued"], 0, "a rejection dropped the id instead of re-queueing it")

    def test_the_attempt_map_cannot_grow_without_limit(self):
        src = APP.read_text(encoding="utf-8")
        assert "_evTries.size>2000" in src, (
            "a long session scrolling a busy feed would grow the counter map for ever")
        assert "_evStalls.size>2000" in src, "the stall counter needs the same bound"

    def test_a_pocket_full_of_dead_sockets_does_not_end_the_asking(self):
        """The APK bug, end to end.

        The app is backgrounded and every relay socket is frozen: `query` resolves empty WITHOUT any
        relay having EOSE'd (`complete:false`). Under the old code that was read as four refusals and
        the id was abandoned — including the one extra attempt the resume redraw buys, which lands
        while the socket is still thawing. Then the relay comes back and nothing ever asks again.
        """
        got = _run("""
        (async () => {
          const id = 'e'.repeat(64);
          let DEAD = true;
          Relay.ready = async () => !DEAD;
          Relay.query = async (f) => { ASKS.push(f[0].ids.slice());
            await new Promise(r => setTimeout(r, 300));          // the query's own timeout, shortened
            return DEAD ? answer([], false) : answer(f[0].ids.map(i => ({ id:i, pubkey:'p' }))); };
          needEvent(id);
          await new Promise(r => setTimeout(r, 16000));          // 16s in a pocket
          needEvent(id);                                         // the redraw on resume re-queues it
          await new Promise(r => setTimeout(r, 2000));           // …and the socket is still dead
          DEAD = false;                                          // relays healthy from here
          await new Promise(r => setTimeout(r, 40000));
          console.log(JSON.stringify({ has: !!Store.get(id), asks: ASKS.length }));
        })();""")
        self.assertTrue(got["has"],
                        "the parent was never fetched after the relays came back — a frozen socket "
                        f"spent the whole retry budget ({got['asks']} asks, then silence)")

    def test_an_answered_no_stays_a_no(self):
        """The generous stall budget must not resurrect an id the relays have already answered
        about — otherwise a reference that genuinely is on no relay we hold becomes a query loop the
        moment the connection goes flaky."""
        got = _run("""
        (async () => {
          const id = 'f'.repeat(64);
          _evTries.set(id, _EV_TRIES_MAX);                       // already answered its last time
          Relay.ready = async () => false;
          Relay.query = async (f) => { ASKS.push(f[0].ids.slice()); return answer([], false); };
          needEvent(id);
          await new Promise(r => setTimeout(r, 4000));
          console.log(JSON.stringify({ asks: ASKS.length, queued: _evQ.size }));
        })();""")
        self.assertEqual(got["asks"], 1)
        self.assertEqual(got["queued"], 0, "a settled 'not on any relay' was re-queued by a stall")

    def test_the_stall_budget_is_bounded_too(self):
        """A relay that is down for good must not be polled for ever either — the stall counter is
        wider than the answered one, not infinite."""
        got = _run("""
        (async () => {
          const id = '9'.repeat(64);
          _evStalls.set(id, _EV_STALL_MAX - 1);                  // one attempt short of the cap
          Relay.ready = async () => false;
          Relay.query = async (f) => { ASKS.push(f[0].ids.slice()); return answer([], false); };
          needEvent(id);
          await new Promise(r => setTimeout(r, 40000));
          console.log(JSON.stringify({ asks: ASKS.length }));
        })();""")
        self.assertLessEqual(got["asks"], 2, f"it kept asking a dead relay: {got['asks']} times")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ReaskOnResume(unittest.TestCase):
    """Coming back has to RE-ASK, because a repaint does not.

    `_drawTimeline` reconciles the feed by key and reuses the cards already on screen, so nothing
    re-runs the `needEvent()` that built a placeholder. Without an explicit re-ask, a card that gave
    up while the phone was in a pocket stays a shell until the page is reloaded — which is precisely
    what "or never do without a manual reload" describes.
    """

    def _reask(self, dom, script=""):
        src = APP.read_text(encoding="utf-8")
        boot = """
const saved = new Map();
const Store = { get: id => saved.get(id) || null };
let ASKED = [];
const needEvent = id => ASKED.push(id);
const document = { querySelectorAll: sel => DOM.filter(el => sel.split(',').some(s => {
  const a = s.trim().replace(/^\\.note/, '').replace(/[\\[\\]]/g, '');
  return el.dataset[{'data-orig':'orig','data-qload':'qload','data-nctx':'nctx'}[a]] !== undefined;
})) };
const DOM = %s;
""" % dom
        body = _extract(src, "function _reaskMissing(){")
        decls = "\n".join([
            re.search(r"^\s*(const _evQ=new Set\(\); let _evT=null; const _evTries=new Map\(\);)", src, re.M).group(1),
            re.search(r"^\s*(const _evStalls = new Map\(\);)", src, re.M).group(1),
            re.search(r"^\s*(let _lastReask = 0;)", src, re.M).group(1),
        ])
        out = subprocess.run(["node", "-e", boot + decls + "\n" + body + "\n" + script],
                             capture_output=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(out.stderr.decode()[-2000:])
        return json.loads(out.stdout.decode() or "null")

    def test_it_asks_for_every_placeholder_still_on_screen(self):
        dom = ("[{dataset:{orig:'"+'a'*64+"'}},{dataset:{qload:'"+'b'*64+"'}},"
               "{dataset:{nctx:'"+'c'*64+"'}}]")
        got = self._reask(dom, "_reaskMissing(); console.log(JSON.stringify({asked: ASKED.length}));")
        self.assertEqual(got["asked"], 3,
                         "a repost, a quote and a notification preview are the three shells "
                         "patchLoaded knows how to fill — all three have to be re-asked")

    def test_it_clears_the_give_up_state(self):
        got = self._reask("[]", """
          _evTries.set('x', 4); _evStalls.set('y', 8);
          _reaskMissing();
          console.log(JSON.stringify({ tries: _evTries.size, stalls: _evStalls.size }));""")
        self.assertEqual((got["tries"], got["stalls"]), (0, 0),
                         "coming back must undo the give-up, or the re-ask is refused on arrival")

    def test_it_is_throttled(self):
        """onReconnect fires on every socket reopen, and a flapping relay would otherwise turn this
        into a query loop that also keeps clearing the budget it is meant to respect."""
        dom = "[{dataset:{orig:'"+'a'*64+"'}}]"
        got = self._reask(dom, """
          _reaskMissing(); _reaskMissing(); _reaskMissing();
          console.log(JSON.stringify({asked: ASKED.length}));""")
        self.assertEqual(got["asked"], 1)


class ResumeIsWiredToBothSignals(unittest.TestCase):
    """The pause and the resume must be armed from the SAME signals.

    app.js arms `_tlBackground()` from `visibilitychange` AND from Capacitor's `appStateChange`,
    deliberately, because a phone can coalesce its `visibilitychange` away. `_tlForeground()` was
    released from `visibilitychange` only — so on exactly the phones the native listener exists for,
    the timeline subscription was dropped 20 seconds into a pocket and never re-armed on the way
    back. It came back only by accident, through the redraw `Relay.onReconnect` does after `wake()`,
    which does not happen at all when `_resumeRelay`'s 4-second debounce swallows the wake.
    """

    def test_the_native_listener_block_releases_what_it_arms(self):
        src = APP.read_text(encoding="utf-8")
        i = src.index("if(!window.__pcNativeBound){")
        block = src[i:src.index("function bindGlobalsOnce()")]
        self.assertIn("_tlBackground()", block, "this test is looking at the wrong block")
        self.assertIn("_tlForeground()", block,
                      "the native resume signal arms the timeline PAUSE but never releases it — a "
                      "phone that coalesces visibilitychange never gets its timeline back")

    def test_every_listener_that_pauses_also_resumes(self):
        """The rule per LISTENER, not per file: a handler that can arm the pause must be able to
        release it. Counting call sites would be wrong — the resume side is deliberately armed from
        both `resume` and `appStateChange`, the same redundancy `_nativeResume` already has.
        """
        src = APP.read_text(encoding="utf-8")

        def opening_brace(idx):
            """Index of the `{` opening the innermost block that contains idx."""
            depth = 0
            for j in range(idx, -1, -1):
                if src[j] == "}":
                    depth += 1
                elif src[j] == "{":
                    if depth == 0:
                        return j
                    depth -= 1
            raise AssertionError("no enclosing block for offset %d" % idx)

        for start in [m.start() for m in re.finditer(r"(?<!function )_tlBackground\(\)", src)]:
            # Widen from the call site until the block IS the listener (both handlers are registered
            # with addEventListener/addListener), then require the release inside that same listener.
            i, body = start, ""
            for _ in range(8):
                i = opening_brace(i - 1)
                body = src[i:_closing_brace(src, i) + 1]
                if "addEventListener(" in body or "addListener(" in body:
                    break
                if i == 0:
                    break
            self.assertIn("_tlForeground()", body,
                          "a listener arms the timeline PAUSE with no way to release it:\n"
                          + body[:400])


if __name__ == "__main__":
    unittest.main()
