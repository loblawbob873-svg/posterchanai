"""Typing a DM must survive the message that arrives while you type.

    venv-unified/bin/python -m pytest tests/client/test_dm_draft_survives.py

THE BUG, as reported: "toaster removing what I type in message in DM". The toast is not the culprit,
it is the timestamp. An incoming DM calls `_dmNotify` (the toast) and then `_scheduleDmRefresh`,
which 350ms later calls `renderDmThread(dmActive)` — and that function rebuilt the whole pane with
one `wrap.innerHTML = ...`, textarea included. So the half-written message went away roughly a third
of a second after the toast said someone had answered. The attachment went with it: the preview
strip is DERIVED from image URLs sitting in that same text (`wireImgAttach`), so there was no
separate copy of it to survive.

The functions run here are EXTRACTED from static/js/client/app.js, not retyped, and they run in real
headless Chrome — the bug lives entirely in DOM lifetime (which nodes are replaced, which listeners
go with them, where the caret is), which is exactly what a hand-written fake DOM would model away.
Same reasoning as test_client_qr_encoder.py decoding real QRs instead of comparing pixels.

Four scenarios, one per way the pane gets rebuilt or the draft gets lost:

  keeps-draft-on-refresh   The reported bug: a message arrives mid-sentence.
  keeps-focus-and-caret    Weaker guard, same bug — text restored but the caret sent to the end is
                           still "it ate what I was typing" if you were editing the middle.
  survives-full-rebuild    renderMessages() replaces the whole #feed above this function (the NIP-17
                           backfill does it on load), so the composer is a genuinely new element and
                           reuse cannot help — the draft map has to seed it.
  no-double-handlers       The reuse path keeps #dm-msgs alive precisely because its long-press and
                           lightbox handlers hang off the container. Rebinding them per refresh would
                           open one lightbox per message ever received.
  sent-text-does-not-return  sendDm re-renders the pane itself, so a draft still in the map when it
                           does would be seeded straight back and the message you just sent would sit
                           there looking unsent.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")

pytestmark = pytest.mark.skipif(CHROME is None, reason="chrome not installed")

# Extracted verbatim: the pane renderer plus every helper it calls that is part of what broke.
WANT = [
    "function wireImgAttach(inp, strip, opts){",
    "function _dmClock(ts){",
    "function _dmDayLabel(ts){",
    "function _dmHidden(){",
    "function _dmHide(id){",
    # Called by renderDmThread. Left out when it was extracted from the render path into its own
    # function, which made the whole harness throw before a single assertion ran.
    "function _dmBodyHtml(m){",
    "function _dmQuoteOf(text){",
    "function _dmReplyBanner(){",
    "function _dmMsgMenu(anchorEl, pk, mid){",
    "function _wireBubbleActions(box, pk){",
    "function bindDmMediaActions(){",
    "function _dmPinBottom(m){",
    "function _threadSig(pk){",
    "async function renderDmThread(pk){",
]


def _extract(src, decl):
    i = src.index(decl)
    j = src.index("{", i + len(decl) - 1)
    depth, k = 0, j
    while k < len(src):
        c = src[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError("unbalanced braces extracting " + decl)


def _sources():
    src = open(APP, encoding="utf-8").read()
    out = []
    for decl in WANT:
        assert decl in src, f"{decl!r} is gone from app.js — this test is testing nothing"
        out.append(_extract(src, decl))
    # `enc` is a one-line arrow, and the draft is written through it into the textarea, so a broken
    # escape would be a real (and injectable) bug — take the real one rather than a stand-in.
    m = re.search(r"^\s*(const enc = .*?;)$", src, re.M)
    assert m, "enc is no longer a one-line const in app.js"
    out.append(m.group(1))
    # The draft map itself.
    m = re.search(r"^\s*(const _dmDrafts = new Map\(\);)", src, re.M)
    assert m, "_dmDrafts is gone — the draft is no longer kept anywhere"
    out.append(m.group(1))
    return "\n".join(out)


HARNESS = r"""
<!doctype html><meta charset="utf-8"><div id="feed"></div><pre id="out"></pre>
<script>
const RESULTS = [];
const ok = (name, cond, detail) => RESULTS.push({name, ok: !!cond, detail: detail || null});

// ---- the stubs the extracted code talks to. Everything the BUG is about (the DOM, the events, the
// listeners) is real; only the network, the crypto and the unrelated UI are stood in for.
const $  = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => [...(r || document).querySelectorAll(s)];
const LOGO = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
const NO_IMAGES = false;
const CFG = {gif_enabled: false};
const MUTED = new Set();
const dmPeers = new Map();
const _dmShown = new Map(), _dmFull = new Set();
const _DM_INIT = 25, _DM_STEP = 30;
const _DM_HIDDEN_KEY = 'dmHidden';
let VIEW = 'messages', dmActive = null, _dmLastPk = '', _dmThreadSig = '', _dmRefreshTimer = null;
let _dmScrollTop = false, _dmReply = null, _dmProg = '';
const ME = {pubkey: 'me'.repeat(32)};
const _store = {};
const ClientSettings = {get: (k, d) => (k in _store ? _store[k] : d), set: (k, v) => { _store[k] = v; }};
const Store = {saveEvent: () => true, byKind: () => []};
const Relay = {query: () => Promise.resolve([]), publish: () => Promise.resolve({ok: true}), subscribe: () => {}};
const NT = () => ({nip19: {npubEncode: pk => 'npub1' + pk.slice(0, 20)}});
const profOf = () => ({name: 'Peer'});
const needProfile = () => {};
const niceNip05 = v => v || '';
const emojiName = (pk, n) => enc(n);
const applyEmojis = ev => ev;
const linkify = t => enc(t || '');
const isImageUrl = u => /\.(png|jpg|jpeg|gif|webp)$/i.test(u || '');
const toast = () => {};
const attachEmojiAutocomplete = () => {};
const blossomPicker = () => {};
const gifPicker = () => {};
const uploadBlob = () => Promise.resolve('https://example.invalid/x.png');
// Encrypted attachments: the composer's 🔒 reads its state from here, and every rebuild of the
// bubbles asks decorateEncAtts to fill in the placeholders. Neither has anything to do with the
// draft, but renderDmThread calls both, so the harness has to answer them.
const uploadSharedEnc = () => Promise.resolve('https://example.invalid/y.enc#pcenc1=AA');
const dmEncOn = () => !!ClientSettings.get('dmEncryptAtts');
const dmPickMedia = () => () => {};
const dmPickGif = () => () => {};
const decorateEncAtts = () => {};
const openMenuPopover = () => {};
const renderProfileView = () => {};
const toggleMute = () => Promise.resolve();
const ensureDmInboxList = () => {};
const decryptMsg = () => Promise.resolve();
let LIGHTBOX = 0;
const openLightbox = () => { LIGHTBOX++; };
let _scheduleDmRefresh = () => {};
// renderMessages rebuilds #feed wholesale and then re-renders the open thread — the real one does
// exactly this, and it is the path the draft map (rather than the reuse path) has to cover.
const renderMessages = async () => {
  $('#feed').innerHTML = '<div class="dm-wrap"><div class="dm-list" id="dm-list"></div>'
    + '<div class="dm-thread" id="dm-thread"></div></div>';
  if (dmActive) await renderDmThread(dmActive);
};
// sendDm re-renders mid-send, like the real one (it shows your own copy before delivery resolves).
// FAIL_AT says WHERE it fails: 'before' is a signer that never answers, 'after' is a publish that
// fails once the pane has already been rebuilt — different failures for the recovery path.
let SENT = [], FAIL_AT = null;
const sendDm = async (pk, body) => {
  if (FAIL_AT === 'before') throw new Error('signer said no');
  SENT.push([pk, body]); await renderMessages();
  if (FAIL_AT === 'after') throw new Error('relay said no');
};

__EXTRACTED__
bindDmMediaActions();

// ---- helpers -------------------------------------------------------------------------------
const mount = () => { $('#feed').innerHTML =
  '<div class="dm-wrap"><div class="dm-list" id="dm-list"></div><div class="dm-thread" id="dm-thread"></div></div>'; };
const msg = (id, t, mine, text) => ({id, t, mine, text, nip17: true, em: []});
const type = (s, selStart, selEnd) => {
  const ta = $('#dm-in');
  ta.focus(); ta.value = s;
  if (selStart != null) ta.setSelectionRange(selStart, selEnd == null ? selStart : selEnd);
  ta.dispatchEvent(new Event('input'));
  return ta;
};
const PK = 'ab'.repeat(32);

(async () => {
try {
  // ---- 1. the reported bug: a DM lands while you are typing ---------------------------------
  mount(); dmActive = PK;
  dmPeers.set(PK, [msg('m1', 1000, false, 'hey')]);
  await renderDmThread(PK);
  type('half a sentence and an image https://example.invalid/pic.png', 12);
  const attsBefore = $('#dm-atts').children.length;
  // ...and their reply arrives. This is exactly what _scheduleDmRefresh does 350ms after the toast.
  dmPeers.get(PK).push(msg('m2', 1001, false, 'you there?'));
  await renderDmThread(PK);
  const ta = $('#dm-in');
  ok('keeps-draft-on-refresh',
     ta && ta.value === 'half a sentence and an image https://example.invalid/pic.png',
     {value: ta && ta.value});
  ok('keeps-attachment-strip', $('#dm-atts').children.length === attsBefore && attsBefore === 1,
     {before: attsBefore, after: $('#dm-atts').children.length});
  ok('new-message-rendered', $$('#dm-msgs .bubble').length >= 1 && $('#dm-msgs').innerHTML.includes('you there?'),
     {bubbles: $$('#dm-msgs .bubble').length});

  // ---- 2. the caret, not just the text ------------------------------------------------------
  ok('keeps-focus-and-caret',
     document.activeElement === ta && ta.selectionStart === 12,
     {focused: document.activeElement && document.activeElement.id, caret: ta.selectionStart});

  // ---- 3. a rebuild from ABOVE us (renderMessages replacing #feed) ---------------------------
  await renderMessages();
  const ta3 = $('#dm-in');
  ok('survives-full-rebuild',
     ta3 && ta3 !== ta && ta3.value === 'half a sentence and an image https://example.invalid/pic.png',
     {fresh: ta3 !== ta, value: ta3 && ta3.value});

  // ---- 4. handlers must not stack up on the surviving container -----------------------------
  mount(); dmPeers.set(PK, [msg('n1', 2000, false, 'look <img>')]);
  await renderDmThread(PK);
  $('#dm-msgs').innerHTML = '<div class="bubble in" data-mid="n1"><img src="' + LOGO + '"></div>';
  for (let i = 0; i < 4; i++) { dmPeers.get(PK).push(msg('n' + (i + 2), 2001 + i, false, 'x')); await renderDmThread(PK); }
  LIGHTBOX = 0;
  const im = $('#dm-msgs img') || (() => { const d = document.createElement('div');
    d.className = 'bubble'; d.innerHTML = '<img src="' + LOGO + '">'; $('#dm-msgs').appendChild(d);
    return d.querySelector('img'); })();
  im.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  ok('no-double-handlers', LIGHTBOX === 1, {opened: LIGHTBOX});

  // ---- 5. what you just SENT must not come back as a draft -----------------------------------
  mount(); dmActive = PK; dmPeers.set(PK, [msg('s1', 3000, false, 'hi')]); SENT = [];
  await renderDmThread(PK);
  type('this is going out now');
  $('#dm-send').dispatchEvent(new MouseEvent('click', {bubbles: true}));
  await new Promise(r => setTimeout(r, 30));
  ok('sent-text-does-not-return', SENT.length === 1 && ($('#dm-in') || {}).value === '',
     {sent: SENT, left: ($('#dm-in') || {}).value});

  // ---- 6. a send that FAILS keeps the text -----------------------------------------------------
  // "Clear only on success" is a deliberate rule, and dropping the draft BEFORE sending (scenario 5)
  // is exactly the kind of change that quietly breaks it. Both failure points, because they leave the
  // composer in different states: one never re-rendered, the other did.
  for (const where of ['before', 'after']) {
    mount(); dmActive = PK; dmPeers.set(PK, [msg('f1', 4000, false, 'hi')]); SENT = [];
    await renderDmThread(PK);
    FAIL_AT = where;
    type('do not lose this');
    $('#dm-send').dispatchEvent(new MouseEvent('click', {bubbles: true}));
    await new Promise(r => setTimeout(r, 30));
    const live = $('#dm-in');
    ok('failed-send-keeps-text-' + where,
       live && live.value === 'do not lose this' && _dmDrafts.get(PK) === 'do not lose this',
       {value: live && live.value, draft: _dmDrafts.get(PK)});
    FAIL_AT = null;
  }
} catch (e) {
  RESULTS.push({name: 'harness', ok: false, detail: String(e && e.stack || e)});
}
document.getElementById('out').textContent = JSON.stringify(RESULTS);
document.title = 'done';
})();
</script>
"""


@pytest.fixture(scope="module")
def results():
    page = HARNESS.replace("__EXTRACTED__", _sources())
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "dm.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        r = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--user-data-dir=" + os.path.join(d, "profile"),
             "--virtual-time-budget=8000", "--dump-dom", "file://" + path],
            capture_output=True, text=True, timeout=180,
        )
        dom = r.stdout
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    assert m and m.group(1).strip(), f"the page produced no results:\n{dom[-2000:]}\n{r.stderr[-1500:]}"
    rows = json.loads(m.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return {row["name"]: row for row in rows}


def _check(results, name):
    assert "harness" not in results, f"the harness itself threw: {results['harness']['detail']}"
    assert name in results, f"scenario missing: {name!r} (have {sorted(results)})"
    assert results[name]["ok"], f"{name}: {json.dumps(results[name]['detail'])}"


def test_keeps_draft_on_refresh(results):
    """The reported bug: their reply lands, your half-written message goes."""
    _check(results, "keeps-draft-on-refresh")


def test_keeps_attachment_strip(results):
    """The attachment is URLs in the draft text, so losing the text lost the upload too."""
    _check(results, "keeps-attachment-strip")


def test_new_message_rendered(results):
    """Preserving the composer must not cost the refresh — the new message still has to appear."""
    _check(results, "new-message-rendered")


def test_keeps_focus_and_caret(results):
    """Restoring text but not the caret is still "it ate what I was typing" mid-word."""
    _check(results, "keeps-focus-and-caret")


def test_survives_full_rebuild(results):
    """renderMessages() replaces #feed above us; the composer is new and must be seeded."""
    _check(results, "survives-full-rebuild")


def test_no_double_handlers(results):
    """The reuse path keeps #dm-msgs, so its listeners must not be bound again per refresh."""
    _check(results, "no-double-handlers")


def test_sent_text_does_not_return(results):
    """sendDm re-renders mid-send; a draft left in the map reappears as unsent text."""
    _check(results, "sent-text-does-not-return")


@pytest.mark.parametrize("where", ["before", "after"])
def test_failed_send_keeps_text(results, where):
    """"Clear only on success" is the rule dropping the draft early is most likely to break."""
    _check(results, "failed-send-keeps-text-" + where)
