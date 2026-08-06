"""One event with no `tags` must not be able to take down every timeline in the client.

Run: venv-unified/bin/python -m pytest tests/test_client_store_tags.py

The reported symptom was a whole feed of "⚠ couldn't render this post", with the "↩ replying to
<name>" line above each stub rendering perfectly — reported from production as *"Home timeline says
Replying to FUGGLES couldn't render this post"*, for many posts at once, with
`TypeError: Cannot read properties of undefined (reading 'length')` in the console.

The cause was not the posts. `tags` is REQUIRED by NIP-01, but the client's cache is fed from places
that cannot promise it — a kind-6 repost embeds its original as arbitrary JSON in `content`, and
whatever that parsed to was saved as an event on the strength of having an `id`. It then stored
SILENTLY, because the indexer guards (`ev.tags || []`). `buildCounts()` in app.js does not: it walks
every cached event to build the shared count index that EVERY note card asks for, and it assigns
CIDX only after the loop — so one bad event threw once per card and no card in any timeline could
render, while the reply-context line (which guards) still could. It also survived every reload,
because the bad event was on disk in IndexedDB.

So the fix is at the Store boundary, and these tests are written against the real shipped
`static/js/client/store.js` loaded in a `vm` with a `window` stub — no port, no mock of the thing
under test. The app.js half (per-event containment in buildCounts) is asserted by reading the
source, since that function is inside a browser IIFE with no export.
"""
import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "static", "js", "client", "store.js")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(body):
    """Load the real store.js in a stubbed browser context and run `body` against it."""
    harness = textwrap.dedent(f"""
        const fs = require('fs'), vm = require('vm');
        const src = fs.readFileSync({json.dumps(STORE)}, 'utf8');
        const ctx = {{ console, setTimeout, clearTimeout, setInterval, clearInterval,
                      indexedDB: undefined, crypto: require('crypto').webcrypto,
                      localStorage: {{ _d:{{}}, getItem(k){{return this._d[k]||null}},
                                      setItem(k,v){{this._d[k]=String(v)}},
                                      removeItem(k){{delete this._d[k]}} }},
                      navigator: {{ onLine:true }} }};
        ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const Store = ctx.window.Store;

        // The exact thing buildCounts() does to every cached event. Kept verbatim rather than
        // imported because app.js is an IIFE — if this ever stops matching app.js, the assertion
        // below is worthless, so test_buildcounts_is_still_the_shape_this_models guards it.
        const lastE = e => {{ for (let i = e.tags.length - 1; i >= 0; i--)
                                if (e.tags[i][0] === 'e') return e.tags[i][1];
                              return null; }};
        {body}
    """)
    path = "/tmp/pcai-store-tags-harness.js"
    with open(path, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", path], capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()[:3000]
    return json.loads(out.stdout.decode())


def test_an_event_with_no_tags_cannot_poison_the_count_index():
    """The production failure, end to end: save the bad event, then walk the cache the way the
    count index does. Before the fix this threw and every note card in the app became a stub."""
    r = _run("""
        Store.saveEvent({ id:'good', pubkey:'a', kind:1, created_at:1, tags:[['e','parent']],
                          content:'hi', sig:'x' });
        Store.saveEvent({ id:'bad', pubkey:'b', kind:1, created_at:2, content:'no tags here', sig:'x' });
        let err = null, walked = 0;
        try { for (const e of Store.all()) { lastE(e); walked++; } }
        catch (e) { err = String(e && e.message || e); }
        process.stdout.write(JSON.stringify({ err, walked, tags: Store.get('bad').tags }));
    """)
    assert r["err"] is None, f"one tag-less event still breaks every timeline: {r['err']}"
    assert r["walked"] == 2
    assert r["tags"] == [], "tags must be normalised to an array, not left undefined"


def test_a_repost_carrying_junk_as_its_original_is_survivable():
    """noteHtml JSON.parses a kind-6's `content` and saves the result. That payload is written by
    whatever client made the repost, so it is arbitrary — it must not be able to reach the index."""
    r = _run("""
        // what JSON.parse(repost.content) can hand back: an id and nothing else that is required
        const inner = JSON.parse('{"id":"inner1","pubkey":"c","kind":1,"content":"x","created_at":5}');
        Store.saveEvent(inner);
        // and a `tags` that is present but not a list of tags
        Store.saveEvent({ id:'inner2', pubkey:'c', kind:1, created_at:6, tags:['e','parent'], content:'x' });
        let err = null;
        try { for (const e of Store.all()) lastE(e); } catch (e) { err = String(e && e.message || e); }
        process.stdout.write(JSON.stringify({ err, t1: Store.get('inner1').tags,
                                              t2: Store.get('inner2').tags }));
    """)
    assert r["err"] is None, f"a malformed repost payload still poisons the cache: {r['err']}"
    assert r["t1"] == []
    assert r["t2"] == [], "non-array tag entries must be dropped, not indexed"


def test_a_cache_poisoned_before_the_fix_heals_on_reload():
    """The part that matters to whoever is already broken: the bad event is on THEIR disk. If only
    saveEvent normalised, hydrate would restore it intact and the feed would stay dead forever."""
    src = open(STORE, encoding="utf-8").read()
    hydrate = src[src.index("async init()"):src.index("has(id){")]
    sets = re.findall(r"mem\.events\.set\(ev\.id,\s*([^)]+)\)", hydrate)
    assert sets, "hydrate no longer sets events the way this test reads it — re-check it normalises"
    assert all("_normEvent" in s for s in sets), (
        "hydrate puts IndexedDB events into memory WITHOUT normalising: a cache poisoned before "
        f"this shipped never recovers. Found: {sets}")


def test_well_formed_tags_are_left_exactly_alone():
    """Normalising must not quietly rewrite real events — every reader downstream trusts order."""
    r = _run("""
        const tags = [['e','root','','root'], ['e','par','','reply'], ['p','someone'], ['client','x']];
        Store.saveEvent({ id:'ok', pubkey:'a', kind:1, created_at:1, tags, content:'hi', sig:'x' });
        process.stdout.write(JSON.stringify({ tags: Store.get('ok').tags, last: lastE(Store.get('ok')) }));
    """)
    assert r["tags"] == [["e", "root", "", "root"], ["e", "par", "", "reply"],
                         ["p", "someone"], ["client", "x"]]
    assert r["last"] == "par"


def test_an_event_with_no_id_is_refused():
    r = _run("""
        const saved = Store.saveEvent({ pubkey:'a', kind:1, created_at:1, tags:[], content:'x' });
        process.stdout.write(JSON.stringify({ saved, n: Store.all().length }));
    """)
    assert r["saved"] is False
    assert r["n"] == 0


def test_buildcounts_is_still_the_shape_this_models():
    """These tests reimplement buildCounts' walk. Assert the real one still guards, so the model
    above cannot drift into testing nothing."""
    src = open(APP, encoding="utf-8").read()
    i = src.index("function buildCounts()")
    body = src[i:src.index("function countsFor(", i)]
    assert "e.tags.length" not in body, "buildCounts reads e.tags.length unguarded again"
    assert "try{" in body and "catch(err)" in body, (
        "buildCounts lost its per-event containment — one unknown-shaped event can again cost the "
        "whole timeline rather than its own counts")
    assert "ME && e.pubkey===ME.pubkey" in body, (
        "buildCounts dereferences ME again: a guest reading the public feed has no key, and that "
        "throws on the first kind-7, taking out every card")
