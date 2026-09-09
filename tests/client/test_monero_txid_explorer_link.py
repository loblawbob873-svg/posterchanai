"""A TRANSACTION ID IS CLICKABLE IN BOTH WALLETS, AND CLICKING IT IS A PRIVACY DECISION.

The shipped `monero-wallet.js` under node, driven through the real render path for BOTH surfaces —
the operator's node wallet and the user's own custodial one — with every request intercepted and no
explorer ever contacted.

What each rule here is protecting, and how it fails silently without the test:

  * **The id was being dropped.** Both histories carry a `txid` (the node wallet passes
    `get_transfers` through, the per-user one builds the field explicitly) and `transferView`
    returned `{incoming, amount, date}`. The screen looked complete: a row with a direction, an
    amount and a date and no way at all to look the payment up.
  * **The network decides the explorer, and it is decided on the NODE.** A mainnet explorer URL
    holding a stagenet txid is a "not found" page, which on a wallet screen reads as a lost payment.
    The client only ever appends an id to a base the node built from its own wallet's network, so a
    client that is handed no base draws no link rather than guessing one.
  * **Leaving goes through `PC.openExternal`.** A bare `<a target=_blank>` or `window.open` does
    NOTHING in the APK's WebView, silently — the documented way this feature would ship broken on
    the shell most people use.
  * **It asks first.** A block-explorer request tells that explorer's operator this device's IP
    address and which transaction it cares about. The app must not spend that on the user's behalf,
    so the first open per device is a question that names the host, and copying — which leaks
    nothing — is offered beside it and always available.
  * **The id is validated before it reaches a URL or the DOM.** It arrives from a remote API;
    anyone who can get a row into that history controls the string.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET = ROOT / "static" / "js" / "client" / "monero-wallet.js"

STAGENET = "5" + "A" * 94
TXID = "a1" * 32                       # 64 hex characters, which is what a Monero txid is
TXID2 = "b3" * 32

BOOT = r"""
globalThis.window = globalThis;

let _html = '';
let _dom = {};
const feed = { get innerHTML(){ return _html; }, set innerHTML(v){ _html = String(v); _dom = {}; } };

/* querySelectorAll over the HTML the module actually produced. Deliberately a fake and not a DOM
   library: it reads the SHIPPED markup, so a renamed class or a dropped data-tx makes the binding
   find nothing here exactly as it would in a browser. Memoised per selector so the object the
   module bound its handler to is the object the test presses. */
function _q(sel){
  if(_dom[sel]) return _dom[sel];
  const cls = sel.replace(/^\./,'');
  const re = new RegExp('<button[^>]*class="' + cls + '"[^>]*?data-tx="([^"]*)"', 'g');
  const out = []; let m;
  while((m = re.exec(_html))){ const tx = m[1];
    out.push({ onclick:null, getAttribute:(k)=> k === 'data-tx' ? tx : null }); }
  _dom[sel] = out; return out;
}

const el = () => ({
  value:'', textContent:'', innerHTML:'', disabled:false, checked:false, isConnected:true,
  style:{}, dataset:{}, classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  appendChild(){}, setAttribute(){}, getAttribute(){ return null; }, addEventListener(){},
  focus(){}, remove(){}, select(){}, setSelectionRange(){},
  querySelector(){ return el(); }, querySelectorAll(){ return []; },
});

globalThis.document = {
  createElement: el, body: el(), documentElement: el(),
  getElementById(id){ return id === 'feed' ? feed : null; },
  querySelector(s){ return s === '#feed' ? feed : el(); },
  querySelectorAll(s){ return _q(s); }, addEventListener(){},
};
globalThis.copied = [];
/* defineProperty, not assignment: node ships its own read-only `navigator`, and a plain
   `globalThis.navigator = …` is silently ignored — the clipboard call then throws into the
   module's execCommand fallback and the test measures the wrong path. */
Object.defineProperty(globalThis, 'navigator', { configurable:true, writable:true,
  value: { clipboard: { writeText: async (t) => { copied.push(String(t)); } } } });

globalThis.stored = {};
globalThis.localStorage = {
  getItem:(k)=> Object.prototype.hasOwnProperty.call(stored,k) ? stored[k] : null,
  setItem:(k,v)=>{ stored[k] = String(v); }, removeItem:(k)=>{ delete stored[k]; },
};

globalThis.modals = [];
globalThis.toasts = [];
globalThis.opened = [];          // every URL handed to PC.openExternal
globalThis.openWorks = true;     // what openExternal reports back
function fakeRoot(html){
  const cache = {};
  return { html, isConnected:true, querySelectorAll(){ return []; },
           querySelector(sel){ return cache[sel] || (cache[sel] = el()); } };
}
globalThis.window.__PC = {
  VIEW: 'wallet',
  $: (s) => (s === '#feed' ? feed : el()),
  toast(t){ toasts.push(String(t)); },
  closeModal(){ },
  modal(html, cb){ const r = fakeRoot(html); modals.push(r); if (cb) cb(r); return r; },
  openExternal(u){ opened.push(String(u)); return openWorks; },
};

globalThis.requests = [];
globalThis.replies = {
  '/api/wallet/xmr/status': { network:'stagenet', mainnet:false,
                              explorer_tx_base:'https://stagenet.xmrchain.net/tx/' },
  '/api/wallet/xmr/balance': { balance:'1000000000000', unlocked_balance:'1000000000000' },
  '/api/wallet/xmr/address': { address: %(stagenet)s },
  '/api/wallet/xmr/history': { in:[{ txid:%(txid)s, amount:'0.25', timestamp: 1700000000 }],
                               out:[], pending:[], failed:[], pool:[] },
  '/api/wallet/xmr/me/status': { enabled:true, network:'stagenet', fee_percent:'0',
                                 explorer_tx_base:'https://stagenet.xmrchain.net/tx/' },
  '/api/wallet/xmr/me/balance': { address: %(stagenet)s, balance:'1', unlocked_balance:'1' },
  '/api/wallet/xmr/me/history': { in:[{ txid:%(txid2)s, amount:'0.5', timestamp: 1700000000 }] },
};
globalThis.failures = {};
globalThis.fetch = async (url, opts) => {
  opts = opts || {};
  const key = String(url).split('?')[0];
  requests.push({ url:String(url), key });
  const fail = failures[key];
  if (fail) return new Response(JSON.stringify({ detail: fail.detail }),
                               { status: fail.status, headers:{'Content-Type':'application/json'} });
  return new Response(JSON.stringify(replies[key] || {}),
                      { status:200, headers:{'Content-Type':'application/json'} });
};

globalThis.done = (v) => { process.stdout.write(JSON.stringify(v)); process.exit(0); };
globalThis.html = () => _html;
globalThis.txButtons = (cls) => _q('.' + cls);

require(%(wallet)s);

/** Paint the OPERATOR's wallet, exactly as opening the view does. */
globalThis.paintNode = async () => { await window.PCMoneroWallet.render(); return _html; };
/** Paint the USER's own wallet: the node wallet refuses a non-operator, and render falls through. */
globalThis.paintUser = async () => {
  failures['/api/wallet/xmr/status'] = { status:403, detail:'admin only' };
  await window.PCMoneroWallet.render();
  return _html;
};
""" % {"wallet": json.dumps(str(WALLET)), "stagenet": json.dumps(STAGENET),
       "txid": json.dumps(TXID), "txid2": json.dumps(TXID2)}


def node(script: str):
    done = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60, text=True)
    if done.returncode != 0:
        pytest.fail(f"node failed:\n{done.stdout[-2000:]}\n{done.stderr[-3000:]}")
    assert done.stdout.strip(), f"the script printed nothing; stderr:\n{done.stderr[-2000:]}"
    return json.loads(done.stdout)


@pytest.fixture(scope="module", autouse=True)
def _needs_node():
    if not WALLET.exists():
        pytest.skip("static/js/client/monero-wallet.js is not present")
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20, check=True)
    except Exception as exc:                                            # pragma: no cover
        pytest.skip(f"node not available: {exc}")


# --------------------------------------------------------------- the id reaches the row at all


def test_the_view_model_carries_the_transaction_id():
    """`transferView` returned amount/direction/date and dropped `txid` on the floor — the one
    value that lets somebody look their own payment up."""
    got = node("const v = require(%s).transferView({direction:'in', amount:'1', txid:%s});"
               "done({ txid: v.txid });" % (json.dumps(str(WALLET)), json.dumps(TXID)))
    assert got["txid"] == TXID


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "a" * 63, "a" * 65, "  ", "<script>"])
def test_only_a_real_transaction_id_survives_validation(bad):
    """The history is a remote API response: anyone who can get a row into it controls this string,
    and it ends up in a URL and in the DOM."""
    got = node("done({ id: require(%s).txidOf(%s) });"
               % (json.dumps(str(WALLET)), json.dumps(bad)))
    assert got["id"] == ""


def test_an_uppercase_id_is_normalised_rather_than_refused():
    got = node("done({ id: require(%s).txidOf(%s) });"
               % (json.dumps(str(WALLET)), json.dumps(TXID.upper())))
    assert got["id"] == TXID


# ------------------------------------------------------------------------ the URL, and the network


@pytest.mark.parametrize("base,txid,expect", [
    ("https://stagenet.xmrchain.net/tx/", TXID, "https://stagenet.xmrchain.net/tx/" + TXID),
    ("http://explorer.onion/tx/", TXID, "http://explorer.onion/tx/" + TXID),
    # No base is "this node did not say", never "guess a chain".
    ("", TXID, ""),
    (None, TXID, ""),
    # A base that is not an ordinary http(s) URL never becomes a link.
    ("javascript:alert(1)//", TXID, ""),
    ("data:text/html,x", TXID, ""),
    ('https://x/"onmouseover=', TXID, ""),
    # A bad id is a bad id however good the base is.
    ("https://stagenet.xmrchain.net/tx/", "not-a-txid", ""),
])
def test_the_explorer_url_is_built_only_from_a_validated_pair(base, txid, expect):
    got = node("done({ url: require(%s).explorerTxUrl(%s, %s) });"
               % (json.dumps(str(WALLET)), json.dumps(base), json.dumps(txid)))
    assert got["url"] == expect


def test_a_node_that_sends_no_explorer_base_gets_an_id_with_no_link():
    """The honest degradation: the id is still shown and still copyable, and nothing is guessed."""
    got = node("""
      replies['/api/wallet/xmr/status'] = { network:'stagenet', mainnet:false };
      paintNode().then(h => done({ html:h, open: txButtons('mw-tx-open').length,
                                   copy: txButtons('mw-tx-copy').length }));
    """)
    assert got["open"] == 0, "a link was drawn without the node saying where it goes"
    assert got["copy"] == 1, "the id must stay copyable — copying leaks nothing"
    assert TXID[:10] in got["html"], "the id itself disappeared with the link"


# ------------------------------------------------------------------------ both surfaces are wired


def test_the_operator_wallet_renders_a_clickable_transaction_id():
    got = node("paintNode().then(h => done({ html:h, open: txButtons('mw-tx-open').length }));")
    assert got["open"] == 1
    assert 'data-tx="%s"' % TXID in got["html"]
    assert "Received" in got["html"]


def test_the_users_own_wallet_renders_a_clickable_transaction_id():
    """The node wallet is admin-only, so an ordinary user's screen is `meWalletHtml`. It renders
    through the same `transferRows`, and it has to be handed its OWN explorer base — the two
    wallets are two states and neither may borrow the other's answer."""
    got = node("paintUser().then(h => done({ html:h, open: txButtons('mw-tx-open').length }));")
    assert "This wallet is held by this server" in got["html"], "not the user's own wallet screen"
    assert got["open"] == 1
    assert 'data-tx="%s"' % TXID2 in got["html"]


# ------------------------------------------------------------------ the click, and what it costs


def test_the_first_click_asks_before_it_leaves_and_names_the_host():
    got = node("""
      paintNode().then(async () => {
        txButtons('mw-tx-open')[0].onclick();
        const ask = modals[modals.length - 1];
        done({ asked: modals.length, opened: opened.length, html: ask ? ask.html : '' });
      });
    """)
    assert got["opened"] == 0, "it left the app before asking"
    assert got["asked"] == 1
    assert "stagenet.xmrchain.net" in got["html"], "the question did not name the host"
    assert "IP address" in got["html"], "the question did not say what it leaks"
    assert "Copy the transaction ID instead" in got["html"]


def test_confirming_opens_through_pc_open_external_with_the_right_url():
    got = node("""
      paintNode().then(async () => {
        txButtons('mw-tx-open')[0].onclick();
        modals[modals.length - 1].querySelector('#mw-exp-go').onclick();
        done({ opened, stored });
      });
    """)
    assert got["opened"] == ["https://stagenet.xmrchain.net/tx/" + TXID]
    assert got["stored"] == {}, "consent was remembered without being asked for"


def test_the_answer_is_remembered_only_when_the_box_is_ticked_and_then_it_stops_asking():
    got = node("""
      paintNode().then(async () => {
        const open = () => txButtons('mw-tx-open')[0].onclick();
        open();
        const ask = modals[modals.length - 1];
        ask.querySelector('#mw-exp-remember').checked = true;
        ask.querySelector('#mw-exp-go').onclick();
        const afterFirst = modals.length;
        open();                                   // second click, same device
        done({ afterFirst, asked: modals.length, opened: opened.length, stored });
      });
    """)
    assert got["stored"] == {"pc_xmr_explorer_ok": "1"}
    assert got["asked"] == got["afterFirst"], "it asked again after being told not to"
    assert got["opened"] == 2


def test_copy_never_asks_and_never_leaves_the_app():
    """The answer that leaks nothing has to be free of the question the other one needs."""
    got = node("""
      paintNode().then(async () => {
        txButtons('mw-tx-copy')[0].onclick();
        await new Promise(r => setImmediate(r));
        done({ copied, opened: opened.length, asked: modals.length, toasts });
      });
    """)
    assert got["copied"] == [TXID], "copy must hand over the WHOLE id, not the shortened display"
    assert got["opened"] == 0 and got["asked"] == 0
    assert any("transaction id copied" in t for t in got["toasts"]), got["toasts"]


def test_a_browser_that_never_opened_is_not_reported_as_opened():
    """`PC.openExternal` returns false when nothing happened. Claiming a page that never opened is
    the APK failure this whole path exists to avoid, one layer up."""
    got = node("""
      openWorks = false;
      paintNode().then(async () => {
        txButtons('mw-tx-open')[0].onclick();
        modals[modals.length - 1].querySelector('#mw-exp-go').onclick();
        await new Promise(r => setImmediate(r));
        done({ copied, toasts });
      });
    """)
    assert got["copied"] == [TXID], "it neither opened the page nor handed over the id"
    assert any("could not open your browser" in t for t in got["toasts"]), got["toasts"]


def test_the_link_is_a_button_bound_in_js_not_a_bare_anchor():
    """In the APK a WebView with no multiple-window support does NOTHING with `window.open`, and a
    plain off-origin anchor is what `PC.openExternal` exists to wrap. A raw `<a href="http…">` in
    this markup would be the documented silent failure."""
    got = node("paintNode().then(h => done({ html:h }));")
    row = got["html"][got["html"].index("mw-history"):]
    assert "<a " not in row, "history rows must not carry raw anchors"
    assert 'class="mw-tx-open"' in row


def test_a_hostile_history_row_cannot_reach_the_dom_or_a_url():
    """Anyone who can put a row in this history controls `txid`. It is checked against the shape of
    a real txid before it is a link, and escaped where it is text."""
    got = node("""
      replies['/api/wallet/xmr/history'] = { in:[
        { txid:'" onmouseover="alert(1)', amount:'1', timestamp:1 },
        { txid:'<img src=x onerror=alert(1)>', amount:'1', timestamp:2 }] };
      paintNode().then(h => done({ html:h, open: txButtons('mw-tx-open').length,
                                   copy: txButtons('mw-tx-copy').length }));
    """)
    assert got["open"] == 0 and got["copy"] == 0, "an invented txid was rendered as a link"
    assert "onmouseover" not in got["html"] and "onerror" not in got["html"]
    assert "<img" not in got["html"]


# ------------------------------------------------- the id has to be READABLE, in a real browser


import re                                                              # noqa: E402
import shutil                                                          # noqa: E402
import tempfile                                                        # noqa: E402
from html import unescape                                              # noqa: E402

CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")
CSS = ((ROOT / "static/css/client.css").read_text()
       + "\n" + (ROOT / "static/css/monero-wallet.css").read_text())


def _measure(width, rows_html):
    """Render the SHIPPED history markup against the SHIPPED stylesheet and measure it."""
    page = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>html,body{{margin:0;width:100%;height:100%}}{CSS}</style>
    <main class="feed"><div class="mw-wrap"><section class="mw-card">{rows_html}</section></div></main>
    <pre id="out"></pre><script>
    requestAnimationFrame(()=>{{
      const id=document.querySelector('.mw-txid');
      const all=[...document.querySelectorAll('body *:not(#out):not(script)')];
      const boxes=all.map(x=>x.getBoundingClientRect());
      out.textContent=JSON.stringify({{
        iw:innerWidth, scrollWidth:document.documentElement.scrollWidth,
        right:Math.max(...boxes.map(r=>r.right)),
        display:id?getComputedStyle(id).display:null,
        height:id?id.getBoundingClientRect().height:0,
        wide:id?id.getBoundingClientRect().width:0 }});
    }});
    </script>'''
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "tx.html"
        f.write_text(page)
        done = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                               f"--window-size={width},700", "--force-device-scale-factor=1",
                               "--dump-dom", f.as_uri()], text=True, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1200:]
    match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    assert match, done.stdout[-1200:]
    return json.loads(unescape(match.group(1)))


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
@pytest.mark.parametrize("width", [320, 412, 1280])
def test_the_id_row_lays_out_as_its_own_line_and_never_widens_the_phone(width):
    """`.mw-tx small` is (0,0,1,1) and sets `display:block`, so a bare `.mw-txid` rule (0,0,1,0)
    LOSES — the id and its Copy button would stack instead of sitting on one line, and nothing in a
    node test can see that. A 64-character id at 320px is also the widest thing this screen has ever
    had to fit."""
    rows = subprocess.run(
        ["node", "-e", "process.stdout.write(require(%s).transferRows("
                       "[{direction:'in',amount:'0.25',timestamp:1700000000,txid:'a1'.repeat(32)}],"
                       "'https://stagenet.xmrchain.net/tx/'))" % json.dumps(str(WALLET))],
        capture_output=True, text=True, timeout=30)
    assert rows.returncode == 0, rows.stderr
    got = _measure(width, rows.stdout)
    assert got["display"] == "flex", "the id and its Copy button are not on one line"
    assert got["scrollWidth"] <= got["iw"], "the transaction id widened the page"
    assert got["right"] <= got["iw"] + 1
    assert 0 < got["height"] <= 40, got
