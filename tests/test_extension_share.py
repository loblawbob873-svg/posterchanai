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
    depth, k = 0, src.index("{", i)
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    return src[i:k + 1]


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
    got = _node(_fn(src, "_shareTags") + _fn(src, "_pageUrl") + """
      const out = {};
      out.page  = _shareTags('look at this', 'https://example.com/a?b=1');
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
    assert got["tags"] == [["t", "nostr"], ["t", "bitcoin"]], \
        f"hashtags are not lowercased+deduped into t tags: {got['tags']}"
    assert got["notag"] == [], f"a non-hashtag '#' became a tag: {got['notag']}"
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
  const relayUrls = () => ['wss://vault.example'];
  const _uniqRelays = (list) => {
    const out = [];
    for(const u of list){ const n = String(u || '').replace(/\\/$/, ''); if(n && !out.includes(n)) out.push(n); }
    return out.slice(0, 6);
  };
"""


def _relay_node(body, plan):
    src = _src("background.js")
    code = "\n".join(_fn(src, n) for n in ("_publishTo", "broadcast", "postRelayUrls"))
    return _node((_RELAY_HARNESS % {"code": code, "plan": json.dumps(plan)}) + """
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
    for el in ("pane-post", "post-text", "post-go", "post-note", "post-after", "post-copy"):
        assert f'id="{el}"' in html, f"the post screen is missing #{el}"
    assert "type:'share-post'" in js, "the Post button does not message the background"
    # A note cannot be recalled, and a browser-action popup is a live window you can click twice.
    assert "_postedText" in js, "nothing stops the same note being published twice"
    # The nav is the only thing that reveals a pane; a tab without data-pane is a dead button.
    assert 'data-pane="pane-post"' in html, "the Post tab is not wired to its pane"


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
