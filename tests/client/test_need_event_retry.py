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

The real `flushEvents` is run under node against a stubbed relay, so what is asserted is the
BEHAVIOUR — it asks again, it stops asking eventually, and a rejection is not fatal — rather than the
presence of a retry.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"


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
const Relay = { query: async (f) => { ASKS.push(f[0].ids.slice()); return ANSWER(f[0].ids); } };
"""
    return subprocess.run(
        ["node", "-e", boot + "\n".join(parts) + "\n" + script],
        capture_output=True, timeout=60)


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


if __name__ == "__main__":
    unittest.main()
