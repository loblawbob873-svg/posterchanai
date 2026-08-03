"""The local event cache must never evict a user's own notes.

Run: venv-unified/bin/python -m pytest tests/test_client_store_pinning.py

`static/js/client/store.js` bounds three caches — the in-memory map, what is hydrated from
IndexedDB at startup, and the on-disk store itself — and each one keeps "the newest N by
created_at". That is right for the firehose: timeline content is endless and refetchable. It is
wrong for a NOTE, which only its author can decrypt, so an evicted note is not "scroll back to
refetch it" — it is a note missing from the notebook, missing exactly when there is no network to
refetch it with. A few minutes of reading the global feed is enough to push a whole imported
library out of a 3000-event window, so this is the ordinary case rather than an edge one.

The file is a browser IIFE, so it is loaded here in a `vm` context with a `window` stub — the real
shipped code, no port and no mock of the thing being tested.
"""
import json
import os
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "static", "js", "client", "store.js")

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
        const note = (i, d) => ({{ id:'n'+i, pubkey:'me', kind:30078, created_at: 1000 + i,
                                  tags:[['d', d || ('pcai:note:'+i)], ['l','pcai-notes']],
                                  content:'ct'+i, sig:'x' }});
        const post = (i) => ({{ id:'p'+i, pubkey:'them', kind:1, created_at: 9000000 + i,
                               tags:[], content:'hello '+i, sig:'x' }});
        {body}
    """)
    path = "/tmp/pcai-store-harness.js"
    with open(path, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", path], capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()[:3000]
    return json.loads(out.stdout.decode())


def test_notes_survive_a_firehose_that_overflows_the_cache():
    """The actual scenario: import a library, then read the global feed for a while."""
    r = _run("""
        for (let i = 0; i < 400; i++) Store.saveEvent(note(i));
        // Far more than MEM_MAX, and all NEWER than every note — under a plain newest-N bound this
        // is what pushes the notebook out.
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        const got = Store.query([{ authors:['me'], kinds:[30078], '#l':['pcai-notes'], limit:5000 }]);
        process.stdout.write(JSON.stringify({ notes: got.length, posts: Store.query([{kinds:[1], limit:99999}]).length }));
    """)
    assert r["notes"] == 400, f"the firehose evicted notes: only {r['notes']}/400 left"
    # …and the timeline is still usefully cached: pinning must not starve everything else.
    assert r["posts"] >= 400, f"pinning starved the timeline cache ({r['posts']} posts)"


def test_folders_are_pinned_too():
    """A notebook whose folders were evicted shows every note as 'Unfiled'."""
    r = _run("""
        for (let i = 0; i < 50; i++) Store.saveEvent(note(i, 'pcai:notefolder:'+i));
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        const got = Store.query([{ kinds:[30078], '#l':['pcai-notes'], limit:5000 }]);
        process.stdout.write(JSON.stringify({ folders: got.length }));
    """)
    assert r["folders"] == 50


def test_other_30078_docs_are_not_pinned():
    """The exemption is deliberately narrow — pinning every app document would defeat the bound
    that stops a long session going sluggish."""
    r = _run("""
        for (let i = 0; i < 400; i++) Store.saveEvent({ id:'c'+i, pubkey:'me', kind:30078,
          created_at: 1000+i, tags:[['d','pcai:chat:'+i]], content:'x', sig:'x' });
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        const got = Store.query([{ kinds:[30078], limit:5000 }]);
        process.stdout.write(JSON.stringify({ kept: got.length }));
    """)
    assert r["kept"] < 400, "unrelated app docs should still be evictable"


def test_a_tombstoned_note_does_not_come_back():
    """Deleting publishes an EMPTY replacement. The cache collapses addressable events to the
    latest, so the tombstone — not the old body — must be what a query returns."""
    r = _run("""
        Store.saveEvent(note(1, 'pcai:note:keepme'));
        Store.saveEvent({ id:'tomb', pubkey:'me', kind:30078, created_at: 5000,
                          tags:[['d','pcai:note:keepme'],['l','pcai-notes']], content:'', sig:'x' });
        const got = Store.query([{ kinds:[30078], '#l':['pcai-notes'], limit:50 }]);
        process.stdout.write(JSON.stringify({ n: got.length, content: got[0] ? got[0].content : null }));
    """)
    assert r["n"] == 1
    assert r["content"] == "", "the old note body outlived its tombstone"


def test_notes_attachment_urls_survive_the_markdown_sanitiser():
    """`![pic](pcres:<sha>)` must reach the DOM as an <img>.

    Notes reference their encrypted attachments with a `pcres:<sha256>` URL, which notes.js swaps
    for a decrypted blob: URL after render. app.js's `_mdUrl` is a strict allowlist (https / root
    relative) because it renders UNTRUSTED note markdown, and it silently dropped `pcres:` — so an
    imported note showed its pictures as bare alt text and there was no <img> for the decryption
    step to fill in. "Images not displaying", with every attachment correctly encrypted, uploaded
    and linked.

    Checked at the source level: `_mdUrl` lives inside app.js's IIFE and cannot be imported. That is
    weaker than exercising it, but it does catch the allowance being deleted, which is the actual
    regression risk.
    """
    app = os.path.join(ROOT, "static", "js", "client", "app.js")
    with open(app) as f:
        src = f.read()
    assert "pcres:[0-9a-f]{64}" in src, "_mdUrl no longer allows Notes attachment URLs"
    i = src.index("function _mdUrl")
    body = src[i:i + 400]
    assert "pcres" in body, "the pcres allowance is not inside _mdUrl any more"
