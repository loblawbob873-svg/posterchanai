"""Posting the current page to Nostr from the browser extension.

This is the extension's first UNENCRYPTED write. Everything else it publishes is AES-GCM ciphertext
that only the user can open, and it is signed by a key the popup lends to websites only behind a
per-origin, per-kind prompt. A share has neither property: it is public, permanent, and it is signed
with no prompt at all — the approval IS the button in the extension's own window.

So the things that must hold are:

  * a WEB PAGE cannot reach it. The signer refuses a page that has not been approved; this path has
    nothing to approve, so an unguarded message handler would let any site post as the user silently.
  * "posted" means a relay SAID SO. The vault's publishAndWait resolves on the first OK from any
    relay, which cannot distinguish "stored" from "one of five took it and four refused you".
  * a refusal keeps the relay's own words. "blocked: not in the web of trust" is the whole answer to
    "why did nothing happen"; a generic message throws it away.

The pure parts run under node against the real background.js, so a rewrite that keeps the comments
and loses the guard still fails.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _src(name):
    with open(os.path.join(EXT, name), encoding="utf-8") as f:
        return f.read()


def _fn(src, name):
    """One function, lifted out of the real file by name.

    Extracted rather than reimplemented: a copy of the tag builder in the test would pass forever
    while the shipped one dropped every hashtag.
    """
    i = src.index("function %s(" % name)
    # …including the `async` in front of it, or the extracted copy is a plain function whose body
    # awaits, which node refuses to parse at all.
    if src[max(0, i - 6):i] == "async ":
        i -= 6
    # These are all TOP-LEVEL functions, so the end is the first `}` in column 0. Counting braces
    # instead looked more general and was wrong: a `}` inside a regex character class
    # (`[.,;:!?)\]}>]`) closed the function early and the extract went to node unbalanced, failing
    # with a syntax error about the TEST's variables rather than anything real.
    return src[i:src.index("\n}", i) + 2]


def _consts(src, *names):
    """The real `const NAME = …` lines, for the same reason the functions are lifted rather than
    reimplemented: a dedup window or a cap redefined in the test is a value the extension does not
    have."""
    return "\n".join(src[src.index("const %s = " % n):src.index("\n", src.index("const %s = " % n))]
                     for n in names)


def _node(script):
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=EXT, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
    return json.loads(r.stdout)


# --------------------------------------------------------------- the note itself

def test_tags_make_it_a_share_and_not_a_wall_of_text():
    """`r` is the page, `t` is every hashtag actually typed.

    A hashtag that lives only in the content is invisible to every hashtag feed there is, which reads
    as "my post didn't show up". And a `#` that is not a hashtag — `C#`, a URL fragment — must not
    become one: the note then appears in a feed nobody meant it for.
    """
    src = _src("background.js")
    got = _node(_fn(src, "_shareTags") + _fn(src, "_pageUrl") + _fn(src, "_urlsIn")
                + _fn(src, "_trimUrl") + """
      const out = {};
      // A bracket can be part of a URL; a full stop cannot. Stripping `)` unconditionally tagged
      // `…/Mercury_(planet` — a permanent public note whose link 404s.
      const WIKI = 'https://en.wikipedia.org/wiki/Mercury_(planet)';
      out.paren     = _shareTags('look at ' + WIKI, WIKI);
      out.parenStop = _shareTags('see ' + WIKI + '.', '');
      out.sentence  = _shareTags('(see https://example.com/x)', '');
      // draftFor wraps a selection in “ ”, so those are the quotes most likely to touch a URL.
      out.curly = _shareTags('he said “read https://example.com/a”', '');
      out.page  = _shareTags('look at this https://example.com/a?b=1', 'https://example.com/a?b=1');
      // The address deleted from the draft, and one the user typed instead.
      out.cut   = _shareTags('just a comment, no link', 'https://intranet.example/doc?token=SECRET');
      out.other = _shareTags('read https://other.example/x instead',
                             'https://intranet.example/doc?token=SECRET');
      out.stop  = _shareTags('see https://example.com/x.', 'https://example.com/x');
      out.tags  = _shareTags('#Nostr and #nostr and #bitcoin', '');
      out.notag = _shareTags('the key of C# and https://x.example/#top', '');
      out.about = _shareTags('hi', 'about:debugging');
      out.file  = _shareTags('hi', 'file:///home/me/secret.html');
      out.ext   = _shareTags('hi', 'moz-extension://a-uuid-that-is-a-supercookie/popup.html');
      out.js    = _shareTags('hi', 'javascript:alert(1)');
      out.many  = _shareTags(Array.from({length: 40}, (_, i) => '#t' + i).join(' '), '');
      console.log(JSON.stringify(out));
    """)

    assert got["page"] == [["r", "https://example.com/a?b=1"]], \
        "the page address is not tagged, so no client can preview it and it is unfindable by URL"
    # The note is the whole of what the user agreed to publish. A tag naming a URL they deleted from
    # the draft publishes it anyway — clients render `r` as a link, so an intranet address or a URL
    # carrying a one-time token goes out on a permanent public note the user thought they had cleaned.
    assert got["cut"] == [], f"a URL deleted from the draft was still tagged: {got['cut']}"
    assert got["other"] == [["r", "https://other.example/x"]], \
        f"the tag follows the tab instead of the text the user wrote: {got['other']}"
    assert got["stop"] == [["r", "https://example.com/x"]], \
        f"a sentence's full stop was swallowed into the tagged URL: {got['stop']}"
    # A closing bracket the URL itself opened is PART of the URL. Cutting it publishes a permanent
    # note whose `r` tag 404s — worse than the leak the content-derived tag rule was added to fix.
    wiki = "https://en.wikipedia.org/wiki/Mercury_(planet)"
    assert got["paren"] == [["r", wiki]], f"a URL ending in ')' was truncated: {got['paren']}"
    assert got["parenStop"] == [["r", wiki]], \
        f"a full stop after a bracketed URL broke it: {got['parenStop']}"
    # …and one it did NOT open still closes the sentence, not the link.
    assert got["sentence"] == [["r", "https://example.com/x"]], \
        f"an unmatched ')' was kept in the URL: {got['sentence']}"
    assert got["curly"] == [["r", "https://example.com/a"]], (
        "the typographic quote draftFor itself wraps selections in was captured into the URL "
        f"(%E2%80%9D): {got['curly']}")
    assert got["tags"] == [["t", "nostr"], ["t", "bitcoin"]], \
        f"hashtags are not lowercased+deduped into t tags: {got['tags']}"
    # `C#` and a URL fragment are not hashtags. (The URL in that text IS legitimately `r`-tagged now
    # — it is in the note — so only the `t` tags are the question here.)
    assert [t for t in got["notag"] if t[0] == "t"] == [], \
        f"a non-hashtag '#' became a tag: {got['notag']}"
    for scheme in ("about", "file", "ext", "js"):
        assert got[scheme] == [], (
            f"a {scheme}: address was tagged on a PUBLIC note — it says nothing to anyone else, and "
            "the extension's own URL carries a per-install UUID")
    assert len(got["many"]) == 20, f"the tag count is unbounded: {len(got['many'])}"


# --------------------------------------------------------------- what "posted" means

_RELAY_HARNESS = """
  const fs = require('fs');
  const src = fs.readFileSync('background.js', 'utf8');
  %(code)s
  // A relay that answers however the plan says, or never answers at all.
  const PLAN = %(plan)s;
  class FakeWS {
    constructor(url){ this.url = url; this.sent = []; setTimeout(() => this.onopen && this.onopen(), 0); }
    send(s){
      const ev = JSON.parse(s)[1];
      const p = PLAN[this.url];
      if(!p) return;                      // silent: the socket stays open and says nothing
      setTimeout(() => this.onmessage && this.onmessage({
        data: JSON.stringify(['OK', ev.id, p.ok, p.why || '']) }), 1);
    }
    close(){ }
  }
  const cfg = { relay: 'wss://paired.example', relays: ['wss://paired.example/', 'wss://second.example'] };
  let userRelays = %(user)s;
  const relayUrls = () => userRelays.length ? _uniqRelays(userRelays) : ['wss://vault.example'];
"""


def _relay_node(body, plan, user_relays=()):
    """The REAL _uniqRelays and normRelay, not a stand-in.

    The harness used to define its own `_uniqRelays` that just stripped a trailing slash, while the
    shipped one runs every URL through `normRelay` — scheme coercion, host validation, dropping
    unusable entries. So postRelayUrls was exercised against dedup behaviour the extension does not
    have, and a divergence between the two — the exact bug this is here to catch — could not fail it.
    """
    src = _src("background.js")
    code = "\n".join(_fn(src, n) for n in
                     ("_publishTo", "broadcast", "postRelayUrls", "_uniqRelays", "normRelay"))
    return _node((_RELAY_HARNESS % {"code": code, "plan": json.dumps(plan),
                                    "user": json.dumps(list(user_relays))}) + """
      global.WebSocket = FakeWS;
      (async () => { console.log(JSON.stringify(await (async () => { %s })())); })();
    """ % body)


def test_a_post_goes_wider_than_the_vault_syncs():
    """The vault deliberately narrows to ONE relay; a note that reached one relay reached nobody.

    They are opposite problems. A replaceable document synced to two URLs of the same server made a
    delete reappear, so relayUrls() defaults to a single relay — correct there, and useless for a
    public, append-only note whose whole purpose is to be somewhere other people read.
    """
    got = _relay_node("return postRelayUrls();", {})
    assert "wss://vault.example" in got, "a post skips the relay the vault is actually connected to"
    assert "wss://second.example" in got, \
        f"a post only reaches the vault's own relay, so nobody else sees it: {got}"
    assert len(got) == len(set(got)), f"the same relay is counted twice: {got}"


def test_a_relay_list_the_user_typed_is_not_widened():
    """Widening is for the DEFAULT, never for an explicit choice.

    relayUrls() promises "the user's explicit choice wins", and somebody who narrows the list in the
    Relays pane is usually removing a relay they do not want carrying their public identity. Posting
    to it anyway is a privacy defect, not a nicety — and the Relays pane went on reporting the narrow
    set as "in use" while the wide one was being published to.
    """
    got = _relay_node("return postRelayUrls();", {}, user_relays=["wss://mine.example"])
    assert got == ["wss://mine.example"], \
        f"a post went to relays the user deliberately removed: {got}"


def test_the_count_is_what_relays_actually_accepted():
    got = _relay_node("return await broadcast({ id: 'abc' });", {
        "wss://vault.example": {"ok": True},
        "wss://paired.example": {"ok": False, "why": "blocked: not in the web of trust"},
        "wss://second.example": {"ok": True},
    })
    assert got["accepted"] == 2 and got["tried"] == 3, \
        f"the relay count is not measured: {got}"
    assert got["why"] == "blocked: not in the web of trust", \
        "the relay's own reason for refusing was replaced with something invented"
    assert sorted(got["urls"]) == ["wss://second.example", "wss://vault.example"], (
        "the nevent points at a relay that REFUSED the note — which is pointing a reader at nothing")


def test_a_relays_own_refusal_outranks_a_dead_socket():
    """The first relay in the list is not the most informative one.

    `res.find(r => !r.ok && r.why)` returned whichever failed FIRST in URL order, so one unreachable
    relay at the top reported "could not connect" while the others were saying "blocked: not in the
    web of trust" — sending the user to debug a network problem they do not have, and losing the one
    sentence they could act on.
    """
    got = _relay_node("return await broadcast({ id: 'abc' });", {
        # vault.example is not in the plan at all: its socket opens and nothing ever answers.
        "wss://paired.example": {"ok": False, "why": "blocked: not in the web of trust"},
        "wss://second.example": {"ok": False, "why": "blocked: not in the web of trust"},
    })
    assert got["accepted"] == 0 and got["failed"] == 3
    assert got["why"] == "blocked: not in the web of trust", \
        f"a transport error masked the relay's actual reason: {got['why']!r}"


def test_a_relay_that_says_nothing_is_not_a_success():
    """A socket that opens, takes the event and never answers is the failure that reads as success.

    `send()` on a live socket cannot fail, so anything short of an OK has to be treated as "not
    stored" — the same rule the vault learned when a credential's only copy sat unsent.
    """
    got = _relay_node("return await _publishTo('wss://silent.example', { id: 'abc' }, 120);", {})
    assert got["ok"] is False and got["why"], f"a silent relay reported success: {got}"


def test_a_rejection_is_reported_and_not_swallowed():
    got = _relay_node(
        "return await _publishTo('wss://vault.example', { id: 'abc' }, 500);",
        {"wss://vault.example": {"ok": False, "why": "rate-limited: slow down"}})
    assert got["ok"] is False
    assert got["why"] == "rate-limited: slow down"


# --------------------------------------------------------------- save to Notes

def test_a_saved_note_is_the_apps_own_note_format():
    """A note the app cannot see is a note that was thrown away.

    The extension writes into a library the app owns, so every part of the envelope has to match what
    `static/js/client/notes.js` reads: kind 30078, `d = pcai:note:<id>`, the `l=pcai-notes` label the
    whole library is subscribed by, and a JSON body encrypted to the user's OWN key. Nothing about a
    wrong d-tag or a missing label fails loudly — the note is published, accepted by the relay, and
    simply never appears.
    """
    src = _src("background.js")
    code = (_consts(src, "NOTE_KIND", "D_NOTE", "L_NOTE") + "\n" +
            "\n".join(_fn(src, n) for n in
                      ("_publishTo", "broadcast", "postRelayUrls", "_uniqRelays", "normRelay",
                       "randomId", "saveNote")))
    got = _node((_RELAY_HARNESS % {"code": code,
                                   "plan": json.dumps({"wss://vault.example": {"ok": True}}),
                                   "user": json.dumps(["wss://vault.example"])}) + """
      global.WebSocket = FakeWS;
      global.crypto = require('crypto').webcrypto;
      Object.assign(cfg, { mode:'full', sk:'11'.repeat(32), pubkey:'22'.repeat(32) });
      const key = new Uint8Array(32);
      const sent = [];
      const _skBytes = () => new Uint8Array(32).fill(1);
      // Records WHO the note was encrypted to — the invariant that makes it private.
      const seen = { plain: [] };
      const NT = () => ({ nip44: { v2: {
        utils: { getConversationKey: (sk, pub) => { seen.to = pub; return 'ck'; } },
        encrypt: (plain, ck) => { seen.plain.push(plain); return 'CIPHERTEXT(' + ck + ')'; } } } });
      const finalize = (t) => { const ev = Object.assign({}, t, { id:'ev1', pubkey: cfg.pubkey });
                                sent.push(ev); return ev; };
      (async () => {
        const many = await saveNote({ text: 'Page Title\\n\\n“a quote”\\n\\nhttps://example.com/' });
        const one  = await saveNote({ text: 'just one line' });
        console.log(JSON.stringify({ many, one, ev: sent[0], ev2: sent[1], seen,
                                     note: JSON.parse(seen.plain[0]),
                                     note2: JSON.parse(seen.plain[1]) }));
      })();
    """)

    ev = got["ev"]
    assert ev["kind"] == 30078, f"a note must be kind 30078, got {ev['kind']}"
    tags = {t[0]: t[1] for t in ev["tags"]}
    assert tags["d"].startswith("pcai:note:"), f"wrong d-tag prefix: {tags['d']}"
    assert re.fullmatch(r"[0-9a-f]{32}", tags["d"].split(":")[-1]), \
        f"the note id is not a 16-byte hex id like the app's: {tags['d']}"
    assert tags["l"] == "pcai-notes", (
        "without the l=pcai-notes label the note is outside the one subscription the library reads, "
        "so it is published and invisible")
    # Encrypted to the user's OWN key: that is what makes it unreadable by this server or any other.
    assert got["seen"]["to"] == "22" * 32, \
        f"the note was encrypted to somebody else's key: {got['seen']['to']}"
    assert ev["content"].startswith("CIPHERTEXT("), "the note body was published in the clear"

    note = got["note"]
    for f in ("v", "id", "title", "body", "folder", "tags", "created", "updated", "res"):
        assert f in note, f"the note object is missing `{f}`, which the app's reader expects"
    assert note["title"] == "Page Title", f"the first line is not the title: {note['title']!r}"
    assert "a quote" in note["body"] and "Page Title" not in note["body"], \
        f"the body did not get the rest of the draft: {note['body']!r}"
    assert note["folder"] == "", "a note must land Unfiled, not in a folder id that does not exist"
    # A one-line note whose body is empty opens in Notes as a title with nothing under it, which
    # reads as "the thing I saved was thrown away".
    assert got["one"]["ok"] is True
    assert got["note2"]["title"] == "just one line" and got["note2"]["body"] == "just one line", \
        f"a one-line note lost its body: {got['note2']}"
    assert got["ev2"]["tags"][0][1] != got["ev"]["tags"][0][1], \
        "two notes were saved under the same id — the second replaces the first"


def test_the_note_envelope_matches_the_apps_reader():
    """The two files have to agree, and only one of them is in this repo's test suite by default.

    If the app renames its d-tag prefix or its label, the extension keeps writing the old shape and
    every note saved from the browser silently stops appearing.
    """
    bg = _src("background.js")
    with open(os.path.join(ROOT, "static", "js", "client", "notes.js"), encoding="utf-8") as fh:
        app = fh.read()
    for want in ("const D_NOTE = 'pcai:note:';", "const KIND = 30078;"):
        assert want in app, f"notes.js changed shape ({want!r} is gone) — re-check the extension"
    assert "const L_TAG = 'pcai-notes';" in app
    assert "const D_NOTE = 'pcai:note:';" in bg, "the extension's note d-tag drifted from the app's"
    assert "const L_NOTE = 'pcai-notes';" in bg, "the extension's note label drifted from the app's"
    assert "const NOTE_KIND = 30078;" in bg


def test_a_long_first_line_is_not_thrown_away():
    """Truncating it into the title and starting the body at line 2 silently dropped the rest."""
    src = _src("background.js")
    code = (_consts(src, "NOTE_KIND", "D_NOTE", "L_NOTE") + "\n" +
            "\n".join(_fn(src, n) for n in
                      ("_publishTo", "broadcast", "postRelayUrls", "_uniqRelays", "normRelay",
                       "randomId", "saveNote")))
    got = _node((_RELAY_HARNESS % {"code": code,
                                   "plan": json.dumps({"wss://vault.example": {"ok": True}}),
                                   "user": json.dumps(["wss://vault.example"])}) + """
      global.WebSocket = FakeWS;
      global.crypto = require('crypto').webcrypto;
      Object.assign(cfg, { mode:'full', sk:'11'.repeat(32), pubkey:'22'.repeat(32) });
      const key = new Uint8Array(32);
      const _skBytes = () => new Uint8Array(32).fill(1);
      const plain = [];
      const NT = () => ({ nip44: { v2: {
        utils: { getConversationKey: () => 'ck' },
        encrypt: (p) => { plain.push(p); return 'CT'; } } } });
      const finalize = (t) => Object.assign({}, t, { id:'ev1', pubkey: cfg.pubkey });
      (async () => {
        const long = 'L'.repeat(400);
        await saveNote({ text: long + '\\n\\nhttps://example.com/' });
        await saveNote({ text: 'x'.repeat(200000) });
        console.log(JSON.stringify({ note: JSON.parse(plain[0]), big: JSON.parse(plain[1]) }));
      })();
    """)
    note = got["note"]
    assert len(note["title"]) == 200, f"the title is not capped: {len(note['title'])}"
    assert note["body"].count("L") == 400, \
        "the 200 characters past the title cap exist in neither the title nor the body"
    assert "https://example.com/" in note["body"], "the rest of the draft was dropped"
    # And an enormous note is trimmed up front rather than signed, sent and refused by the relay.
    assert len(got["big"]["body"]) <= 100000, \
        f"no length cap: a note too big to be one event is published anyway ({len(got['big']['body'])})"


def test_read_only_cannot_save_a_note_either():
    """No signing key means no note — and it must say so, not fail at the button."""
    fn = _fn(_src("background.js"), "saveNote")
    assert "cfg.mode === 'full' && cfg.sk" in fn, "a read-only pairing can write notes"
    assert "READ-ONLY" in fn, "a read-only pairing fails with no explanation"
    assert "if(!r.accepted)" in fn, \
        "saveNote reports success without a relay accepting it — the note will not be there later"


def test_read_only_disables_both_destinations():
    """Disabling only the public button left "Save to Notes" live, and its handler returns silently on
    a read-only pairing — a button that does nothing at all, with no message."""
    js = _src("popup.js")
    i = js.index("if(_mode === 'ro')")
    block = js[i:i + 700]
    assert "nb.disabled = true" in block, \
        "the Notes button stays enabled on a read-only pairing over a handler that returns silently"
    assert "postWas" in js, \
        "saving a note re-arms the Post button unconditionally, undoing the post-publish lock"
    # And BOTH move together during a publish: an idle-looking button whose handler no-ops is the
    # same dead control one step later.
    # THREE destinations now: the page-screenshot button joined post and note, and it was left out of
    # this lock at first — live during a post, and during another capture, over a handler that returns
    # silently. Which is the same "button that does nothing" this test was written for.
    assert "function _postBusy(" in js and "for(const b of [go, nb, sh])" in js, \
        "the publish lock does not cover both destinations"


def test_only_the_popup_can_save_a_note():
    bg = _src("background.js")
    i = bg.index("case 'note-save'")
    block = bg[i:i + 300]
    assert "_fromPopup(sender)" in block, \
        "any web page can write notes into the user's notebook with the user's key"
    assert block.index("_fromPopup(sender)") < block.index("saveNote("), \
        "the guard runs after the write"


# --------------------------------------------------------------- the guards

def test_only_the_popup_can_post():
    """The one guard with no second line of defence.

    Everything the NIP-07 signer does for a website is gated by a prompt naming the origin and the
    event kind. This path has no prompt by design — the user pressed the button in the extension's
    own window — so if the message handler took it from a page, any site could post as the user with
    no window ever appearing. A content script always has `sender.tab`.
    """
    bg = _src("background.js")
    i = bg.index("case 'share-post'")
    block = bg[i:i + 400]
    assert "_fromPopup(sender)" in block, \
        "the share handler does not check the message came from the popup — any page can post as the user"
    assert block.index("_fromPopup(sender)") < block.index("sharePost("), \
        "the guard runs after the post; it has to refuse before anything is signed"


def test_read_only_cannot_post_and_says_so():
    """No signing key means no note, and it must not be queued in the vault's outbox either — that
    queue is drained by the APP publishing vault items and has never heard of a note."""
    bg = _src("background.js")
    fn = _fn(bg, "sharePost")
    assert "cfg.mode === 'full' && cfg.sk" in fn, "a read-only pairing can reach the signer"
    assert "READ-ONLY" in fn, "a read-only pairing fails with no explanation"
    assert "outbox" not in fn, "a note was queued in the VAULT's outbox, which the app will never send"
    assert "if(!r.accepted)" in fn, \
        "sharePost reports success without a single relay accepting the note"


def test_the_popup_has_a_post_screen_that_is_wired():
    html = _src("popup.html")
    js = _src("popup.js")
    for el in ("pane-post", "post-text", "post-go", "post-note", "note-go"):
        assert f'id="{el}"' in html, f"the post screen is missing #{el}"
    assert "type:'share-post'" in js, "the Post button does not message the background"
    # The nav is the only thing that reveals a pane; a tab without data-pane is a dead button.
    assert 'data-pane="pane-post"' in html, "the Post tab is not wired to its pane"


def test_a_note_cannot_be_published_twice():
    """RUN the guard, do not grep for it.

    This test used to assert that the strings `_posting` and `POST_SENT` appeared somewhere in
    popup.js. Deleting the guard from any one call site left those strings elsewhere in the file, so
    it stayed green while the defect came back — it documented the fix instead of testing it. Worse,
    it was asserting the popup-side guard, which could not work at all: the popup is destroyed on
    focus loss, which routinely happens DURING the up-to-8s publish, so the record was never written
    for the publish most likely to be repeated.

    The guard is now a content hash in the background, which sees every publish whether the window
    survived it or not. So: publish, then publish the same text again, and require a refusal.
    """
    src = _src("background.js")
    code = (_consts(src, "POSTED_KEY", "POSTED_KEEP", "POSTED_WINDOW") + "\n" +
            "\n".join(_fn(src, n) for n in
                      ("_publishTo", "broadcast", "postRelayUrls", "_uniqRelays", "normRelay",
                       "_pageUrl", "_urlsIn", "_trimUrl", "_shareTags", "_postedLog",
                       "_rememberPost", "_contentHash", "sharePost")))
    got = _node((_RELAY_HARNESS % {"code": code,
                                   "plan": json.dumps({"wss://vault.example": {"ok": True}}),
                                   "user": json.dumps(["wss://vault.example"])}) + """
      global.WebSocket = FakeWS;
      // Just enough extension around sharePost: a full pairing, a signer, and a storage area.
      const store = {};
      const B = { storage: { local: {
        get: async (k) => (k in store ? { [k]: store[k] } : {}),
        set: async (o) => Object.assign(store, o) } } };
      Object.assign(cfg, { mode:'full', sk:'11'.repeat(32), pubkey:'22'.repeat(32) });
      const key = new Uint8Array(32);
      let n = 0;
      const finalize = (t) => Object.assign({}, t, { id: 'ev' + (++n), pubkey: cfg.pubkey });
      const NT = () => ({ nip19: { neventEncode: () => 'nevent1fake' } });
      (async () => {
        const first  = await sharePost({ text: 'the same words', url: 'https://example.com/' });
        const second = await sharePost({ text: 'the same words', url: 'https://example.com/' });
        const edited = await sharePost({ text: 'the same words, edited', url: 'https://example.com/' });
        console.log(JSON.stringify({ first, second, edited, published: n }));
      })();
    """)

    assert got["first"]["ok"] is True, f"the first post did not go out: {got['first']}"
    assert got["second"]["ok"] is False and got["second"].get("duplicate") is True, (
        "posting the identical text twice published a second permanent, unrecallable note: "
        f"{got['second']}")
    assert got["second"].get("nevent"), "the refusal does not point at the note it already published"
    assert got["edited"]["ok"] is True, "editing the text could not post a new note — the guard is a trap"
    assert got["published"] == 2, \
        f"the duplicate was signed and broadcast before being refused ({got['published']} events)"


def test_the_publish_lock_and_the_record_survive_the_popup():
    """The two halves the behaviour test above cannot reach from node.

    `_posting` is the in-flight lock in the popup document, and the record has to be written by the
    BACKGROUND — if it ever moves back into popup.js, the popup being destroyed mid-publish loses it
    again. Checked structurally because there is no DOM here, but checked at the level that would
    actually regress: which FILE owns the record.
    """
    js, bg = _src("popup.js"), _src("background.js")
    assert "if(_posting) return" in js, "the in-flight lock is not honoured by the input/prepare paths"
    assert "if(box) box.disabled = on" in js, \
        "the textarea stays live during a publish, which re-arms the buttons"
    assert "POSTED_KEY" in bg and "_rememberPost" in bg, \
        "the record of what was published is not in the background"
    # (`pcpwGen` is the generator's own preference and predates all of this.)
    for gone in ("POST_SENT", "POST_DRAFT", "pcpwPostDraft', JSON"):
        assert gone not in js, (
            f"`{gone}` is back in the popup: the draft/published state it used to keep here is what "
            "four review rounds of defects were about, and the popup is destroyed on focus loss")


def test_the_generator_is_reachable_before_pairing():
    """It always was, from the header, and moving it into a pair-gated row deleted it silently.

    Generating a password for the account you are about to create is the most ordinary thing to do
    before you have paired anything — and the Pair tab has to be there too, or opening the generator
    on a fresh install is a one-way trip away from the box you paste the code into.
    """
    html = _src("popup.html")
    nav = html[html.index('<nav'):html.index('</nav>')]
    gen = nav[nav.index('id="tab-gen"'):]
    gen = gen[:gen.index('>')]
    assert "data-vault" not in gen, \
        "the generator tab is pair-gated — a fresh install cannot reach the password generator"
    assert 'id="tab-pair"' in nav, "there is no way back to the pairing screen from the generator"
    for tab in ('id="tab-list"', 'id="tab-post"', 'id="bm-tab"'):
        seg = nav[nav.index(tab):]
        assert "data-vault" in seg[:seg.index('>')], f"{tab} is offered before there is a vault"


def test_the_switcher_has_one_owner():
    """Every tab is a `data-pane` button handled in one place.

    Two features have already shipped unreachable in this popup because a pane was added and the
    thing that reveals it was not. A second `onclick` assigned to a nav button further down the file
    silently REPLACES the switcher's — which is exactly how the bookmark toggle became unreachable
    the first time.
    """
    html = _src("popup.html")
    js = _src("popup.js")
    nav = html[html.index('<nav'):html.index('</nav>')]
    ids = [m for m in ("tab-list", "tab-post", "tab-gen", "bm-tab") if f'id="{m}"' in nav]
    assert len(ids) == 4, f"the nav lost a tab: {ids}"
    for tab in ids:
        assert f"$('#{tab}').onclick" not in js, \
            f"#{tab} has a second handler that overwrites the switcher's"
