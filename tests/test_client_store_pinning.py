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
import re
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


def test_mini_app_announcements_survive_the_firehose():
    """Games → Webxdc reads these back. They were announced in February, so the newest-N rule
    evicts every one of them after a few minutes of feed reading — and the directory then reads
    "nothing here yet" exactly when the relays are unreachable and a downloaded, cached, playable
    game is worth the most."""
    r = _run("""
        const mk = i => ({ id:'x'+i, pubkey:'stranger', kind:1063, created_at: 2000+i, sig:'x',
          tags:[['url','https://b.example/'+i+'.xdc'],['m','application/x-webxdc'],
                ['x', String(i).padStart(64,'0')],['webxdc','u'+i]], content:'' });
        for (let i = 0; i < 12; i++) Store.saveEvent(mk(i));
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        process.stdout.write(JSON.stringify({ apps: Store.query([{ kinds:[1063], limit:5000 }]).length }));
    """)
    assert r["apps"] == 12, f"the firehose evicted mini apps: {r['apps']}/12 left"


def test_a_1063_that_is_not_a_mini_app_is_not_pinned():
    """kind 1063 is generic file metadata — pinning every image someone posted would defeat the
    bound entirely. Only the webxdc mime earns it."""
    r = _run("""
        const mk = i => ({ id:'f'+i, pubkey:'stranger', kind:1063, created_at: 2000+i, sig:'x',
          tags:[['url','https://b.example/'+i+'.png'],['m','image/png']], content:'' });
        for (let i = 0; i < 500; i++) Store.saveEvent(mk(i));
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        process.stdout.write(JSON.stringify({ kept: Store.query([{ kinds:[1063], limit:99999 }]).length }));
    """)
    assert r["kept"] < 500, "plain file-metadata events should still be evictable"


def test_the_mini_app_pin_is_capped():
    """UNLIKE EVERY OTHER PIN HERE, this one is written by ANYBODY — a kind-1063 needs no
    relationship to the signed-in user. Uncapped, a spammer publishing a few thousand cheap
    announcements mints that many unevictable entries: _evictMem keeps pinned events in full and
    _pruneIDB may not delete them, so the set only grows, the quota fills, and the cache can free
    nothing to recover. The overflow is not deleted — it just evicts like any other event."""
    r = _run("""
        const mk = i => ({ id:'x'+i, pubkey:'spammer', kind:1063, created_at: 2000+i, sig:'x',
          tags:[['url','https://b.example/'+i+'.xdc'],['m','application/x-webxdc'],
                ['x', String(i).padStart(64,'0')],['webxdc','u'+i]], content:'' });
        for (let i = 0; i < 3000; i++) Store.saveEvent(mk(i));
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        const apps = Store.query([{ kinds:[1063], limit:99999 }]);
        process.stdout.write(JSON.stringify({
          apps: apps.length,
          newestKept: apps.some(e => e.id === 'x2999'),
          posts: Store.query([{kinds:[1], limit:99999}]).length }));
    """)
    assert r["apps"] <= 400, f"{r['apps']} announcements pinned — the cap is not holding"
    assert r["apps"] >= 300, f"the cap threw away too much ({r['apps']})"
    assert r["newestKept"], "the cap must keep the NEWEST announcements, not an arbitrary slice"
    assert r["posts"] >= 400, f"pinned announcements starved the timeline cache ({r['posts']})"

# ---------------------------------------------------------------------------------------------
# The TWO registrations, checked against each other.
# ---------------------------------------------------------------------------------------------
def _carry_families():
    """The d-tags app.js carries to a new relay (`_CARRY_D`), as sample d-tag strings."""
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js")).read()
    m = re.search(r"const _CARRY_D = \[(.*?)\];", src, re.S)
    assert m, "_CARRY_D is gone from app.js"
    out = []
    for pat in re.findall(r"/\^([^/$]+)\$?/", m.group(1)):
        out.append(pat + "x" if pat.endswith(":") else pat)
    assert out, "no patterns parsed out of _CARRY_D"
    return out


def _pinned_rules():
    """The d-tag tests inside store.js `_isPinned`, as (kind, literal) pairs."""
    src = open(STORE).read()
    # Brace-matched, not "up to the first `return false;`" — the function opens with a
    # `if (!ev) return false;` guard, and slicing there yielded an EMPTY rule set that would have
    # made this test pass vacuously for ever.
    i = src.index("function _isPinned")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1; started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                break
        j += 1
    seg = src[i:j]
    rules = [("prefix", v) for v in re.findall(r"startsWith\('([^']+)'\)", seg)]
    rules += [("exact", v) for v in re.findall(r"t\[1\] === '([^']+)'", seg)]
    assert rules, "no d-tag rules parsed out of _isPinned"
    return rules


def test_every_carried_doc_is_also_pinned():
    """A private doc has to be in BOTH lists, and every one of them has missed one at least once.

    `_CARRY_D` (app.js) republishes it when the relay set changes; `_isPinned` (store.js) exempts it
    from the newest-N cache eviction that is right for the firehose and fatal for a document only its
    author can decrypt. Miss the first and the doc is left behind on the old relay; miss the second
    and a few minutes of reading the global feed evicts it — after which the app draws its DEFAULT
    for that feature, which reads as data loss rather than as a cache miss. Neither failure logs
    anything, which is why this is a test and not a convention.
    """
    rules = _pinned_rules()
    for d in _carry_families():
        ok = any((k == "prefix" and d.startswith(v)) or (k == "exact" and d == v)
                 for k, v in rules)
        assert ok, f"{d} is carried across relays but is NOT pinned in the client cache"



def test_your_own_notifications_survive_the_firehose():
    """Reported from the phone: "I have many notifications, mobile only shows 4 when I load the app".

    A mention, reaction, zap or repost that p-tags YOU is a handful of events about you personally,
    and they live in the same newest-N cache as the global feed, which produces thousands. A few
    minutes of timeline reading evicts every one, and the next cold start renders whatever survived
    — a single-digit number — while the relay still holds them all. Not empty, just far too few,
    which is why it reads as a broken view rather than as a cache miss.
    """
    r = _run("""
        ctx.localStorage.setItem('pc_nostr_session', JSON.stringify({pubkey:'me'}));
        const mention = i => ({ id:'m'+i, pubkey:'them', kind:1, created_at: 3000+i, sig:'x',
                                tags:[['p','me']], content:'hey @me '+i });
        const zap     = i => ({ id:'z'+i, pubkey:'them', kind:9735, created_at: 4000+i, sig:'x',
                                tags:[['p','me']], content:'' });
        for (let i = 0; i < 40; i++) { Store.saveEvent(mention(i)); Store.saveEvent(zap(i)); }
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        const mine = Store.query([{ '#p':['me'], limit:5000 }]).length;
        process.stdout.write(JSON.stringify({ mine }));
    """)
    assert r["mine"] == 80, f"the firehose evicted your notifications: {r['mine']}/80 left"


def test_a_stranger_cannot_mint_unevictable_rows():
    """The second pin here written by strangers rather than by the signed-in user, so it takes the
    same cap as the mini-app one: anybody can p-tag anybody."""
    r = _run("""
        ctx.localStorage.setItem('pc_nostr_session', JSON.stringify({pubkey:'me'}));
        const spam = i => ({ id:'s'+i, pubkey:'flooder', kind:1, created_at: 5000+i, sig:'x',
                             tags:[['p','me']], content:'spam '+i });
        for (let i = 0; i < 3000; i++) Store.saveEvent(spam(i));
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        process.stdout.write(JSON.stringify({ kept: Store.query([{ '#p':['me'], limit:20000 }]).length }));
    """)
    assert r["kept"] <= 900, f"the notification pin is unbounded: {r['kept']} kept"
    assert r["kept"] >= 700, f"the cap threw away far more than it should: {r['kept']}"


def test_your_own_posts_are_not_notifications():
    """A post you wrote that p-tags you (a self-mention, or a reply in your own thread) is timeline
    content and must age out normally, or every post you ever make becomes unevictable."""
    r = _run("""
        ctx.localStorage.setItem('pc_nostr_session', JSON.stringify({pubkey:'me'}));
        const own = i => ({ id:'o'+i, pubkey:'me', kind:1, created_at: 6000+i, sig:'x',
                            tags:[['p','me']], content:'mine '+i });
        for (let i = 0; i < 40; i++) Store.saveEvent(own(i));
        for (let i = 0; i < 9000; i++) Store.saveEvent(post(i));
        process.stdout.write(JSON.stringify({ own: Store.query([{ authors:['me'], limit:5000 }]).length }));
    """)
    assert r["own"] < 40, f"your own posts are being pinned as notifications: {r['own']}/40 kept"
