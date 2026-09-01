"""THE SEND PATH, DRIVEN — the shipped monero-wallet.js under node, from tip to broadcast.

The other client files cover this module's pure helpers (`uri`, `parsePaymentUri`, `format`) and the
rule that it must claim `#feed` before awaiting. Neither of them ever presses a button, so the part
that actually moves money — tip → send sheet → review → confirm → prepare → confirm — has no test at
all, and it is the part where a mistake is unrecoverable.

What is checked here, and why each one is silent when it breaks:

  * `tip()` is handed an address off SOMEBODY ELSE'S profile. Almost every one in the wild is
    mainnet, and this preview is stagenet-only: it has to decline and let app.js fall through to the
    Feather/GUI handoff. A `tip()` that returned true on a mainnet address would open a stagenet
    sheet pre-filled with an address this wallet cannot pay — and swallow the working flow behind it.
  * Spending is TWO requests. If the client ever posted the transfer in one call, or re-sent the
    amount alongside the confirmation token, the server's parse would stop being the only thing that
    decides what leaves the wallet.
  * "I understand this cannot be reversed" gates the send button. A checkbox that does not actually
    gate anything looks identical on screen.
  * A failed send must leave the dialog usable and must NOT retry. A button stuck on "Sending…"
    after a 503 is the state in which somebody force-quits and pays twice by hand.
  * A success has to invalidate the 8-second probe cache, or the balance and the history behind the
    dialog keep showing the wallet as it was before the payment.

Everything is stubbed: no network, no DOM library, no wallet. The module is loaded exactly as the
browser loads it, and the assertions are on what it asked for.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET = ROOT / "static" / "js" / "client" / "monero-wallet.js"

STAGENET = "5" + "A" * 94
MAINNET = "4" + "A" * 94

#: A DOM stub deep enough to open a modal, read its inputs and press its buttons. Elements are
#: memoised per selector so `r.querySelector('#mw-confirm')` returns the same object the module
#: bound its handler to.
BOOT = r"""
globalThis.window = globalThis;

const el = () => ({
  value:'', textContent:'', innerHTML:'', disabled:false, checked:false, isConnected:true,
  style:{}, dataset:{}, classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  appendChild(){}, setAttribute(){}, addEventListener(){}, focus(){}, remove(){},
  select(){}, setSelectionRange(){}, querySelector(){ return el(); }, querySelectorAll(){ return []; },
});

const feed = { innerHTML:'' };
globalThis.feed = feed;
globalThis.document = {
  createElement: el, body: el(), documentElement: el(),
  getElementById(id){ return id === 'feed' ? feed : null; },
  querySelector(s){ return s === '#feed' ? feed : el(); },
  querySelectorAll(){ return []; }, addEventListener(){},
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };

globalThis.modals = [];
globalThis.toasts = [];
globalThis.closed = 0;
function fakeRoot(html){
  const cache = {};
  return { html, isConnected:true, querySelectorAll(){ return []; },
           querySelector(sel){ return cache[sel] || (cache[sel] = el()); } };
}
globalThis.window.__PC = {
  VIEW: 'social',
  $: (s) => (s === '#feed' ? feed : el()),
  toast(t){ toasts.push(String(t)); },
  closeModal(){ closed++; },
  modal(html, cb){ const r = fakeRoot(html); modals.push(r); if (cb) cb(r); return r; },
};

globalThis.requests = [];
globalThis.replies = {
  '/api/wallet/monero/status': { network:'stagenet', mainnet:false },
  '/api/wallet/monero/balance': { balance:'1000000000000', unlocked_balance:'1000000000000' },
  '/api/wallet/monero/address': { address: %(stagenet)s },
  '/api/wallet/monero/history': { in:[], out:[], pending:[], failed:[] },
  '/api/wallet/monero/transfer/prepare': { confirmation:'tok-'+'z'.repeat(40), expires_at: 0,
                                           address: %(stagenet)s, amount_atomic: 10000000000 },
  '/api/wallet/monero/transfer/confirm': { tx_hash:'deadbeef' },
};
globalThis.failures = {};
globalThis.fetch = async (url, opts) => {
  opts = opts || {};
  const key = String(url).split('?')[0];
  requests.push({ url:String(url), key, method: opts.method || 'GET',
                  body: opts.body ? JSON.parse(opts.body) : null });
  const fail = failures[key];
  if (fail) return new Response(JSON.stringify({ detail: fail.detail }),
                                { status: fail.status, headers:{'Content-Type':'application/json'} });
  return new Response(JSON.stringify(replies[key] || {}),
                      { status:200, headers:{'Content-Type':'application/json'} });
};

const done = (v) => { process.stdout.write(JSON.stringify(v)); };
globalThis.done = done;

require(%(wallet)s);

/** Open the send sheet the way the app does — through the zap/tip entry point. */
globalThis.openSend = async (address) => {
  const ok = await window.PCMoneroWallet.tip({ address, name:'alice' , onSent:(a,t)=>{
    globalThis.sentCallback = { amount:a, txid:t };
  }});
  return { ok, sheet: modals[modals.length - 1] };
};
""" % {"wallet": json.dumps(str(WALLET)), "stagenet": json.dumps(STAGENET)}


def node(script: str):
    done = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60, text=True)
    if done.returncode != 0:
        pytest.fail(f"node failed:\n{done.stderr[-3000:]}")
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


# --------------------------------------------------------------------------- the tip entry point


def test_a_mainnet_profile_address_is_declined_by_a_stagenet_wallet_so_external_flow_runs():
    """THE COMMON CASE. Nostr profiles carry mainnet addresses; this wallet is stagenet-only. `tip()`
    returning anything truthy here would swallow app.js's `return` and leave the user looking at a
    sheet that cannot pay the person they clicked on."""
    got = node("""
      openSend(%s).then(r => done({ ok:r.ok, modals: modals.length, requests: requests.length }));
    """ % json.dumps(MAINNET))
    assert got["ok"] is False
    assert got["modals"] == 0, "a mainnet tip opened the built-in wallet's send sheet"
    assert got["requests"] == 4, "network is learned from the authenticated wallet status probe"


def test_a_stagenet_profile_address_is_declined_by_a_mainnet_wallet():
    got = node("""
      replies['/api/wallet/monero/status']={network:'mainnet',mainnet:true};
      replies['/api/wallet/monero/address']={address:%s};
      openSend(%s).then(r => done({ok:r.ok,modals:modals.length,requests:requests.length}));
    """ % (json.dumps(MAINNET), json.dumps(STAGENET)))
    assert got == {"ok": False, "modals": 0, "requests": 4}


def test_a_mainnet_profile_address_opens_when_wallet_reports_mainnet():
    got = node("""
      replies['/api/wallet/monero/status']={network:'mainnet',mainnet:true};
      replies['/api/wallet/monero/address']={address:%s};
      openSend(%s).then(r => done({ok:r.ok,modals:modals.length}));
    """ % (json.dumps(MAINNET), json.dumps(MAINNET)))
    assert got == {"ok": True, "modals": 1}


def test_a_tip_is_declined_when_the_local_wallet_is_not_answering():
    """The wallet service is off on every node by default. The probe fails, `tip()` says no, and the
    caller falls through to the URI/QR handoff — tipping must never depend on this integration."""
    got = node("""
      globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
      openSend(%s).then(r => done({ ok:r.ok, modals: modals.length, toasts }));
    """ % json.dumps(STAGENET))
    assert got["ok"] is False
    assert got["modals"] == 0


def test_a_stagenet_tip_opens_the_send_sheet_with_the_address_already_filled():
    got = node("""
      openSend(%s).then(r => done({
        ok:r.ok, to: r.sheet.querySelector('#mw-to').html === undefined ? null : null,
        html: r.sheet.html, requests: requests.map(x => x.key),
      }));
    """ % json.dumps(STAGENET))
    assert got["ok"] is True
    assert STAGENET in got["html"]
    assert "Tip alice" in got["html"]
    assert "cannot be reversed" not in got["html"], "the send sheet is not the confirm sheet"
    assert set(got["requests"]) == {"/api/wallet/monero/balance", "/api/wallet/monero/address",
                                    "/api/wallet/monero/history", "/api/wallet/monero/status"}


# --------------------------------------------------------------------------- review


REVIEW = """
  openSend(%s).then(async r => {
    const sheet = r.sheet;
    sheet.querySelector('#mw-to').value = %s;
    sheet.querySelector('#mw-amount').value = %s;
    sheet.querySelector('#mw-note').value = %s;
    const before = requests.length;
    await sheet.querySelector('#mw-review').onclick();
    done({ modals: modals.length, toasts, newRequests: requests.length - before,
           confirmHtml: modals.length > 1 ? modals[1].html : null,
           to: sheet.querySelector('#mw-to').value,
           amount: sheet.querySelector('#mw-amount').value,
           note: sheet.querySelector('#mw-note').value });
  });
"""


def _review(to, amount, note="", address=STAGENET):
    return node(REVIEW % (json.dumps(address), json.dumps(to), json.dumps(amount), json.dumps(note)))


@pytest.mark.parametrize("to,amount,why", [
    (MAINNET, "0.01", "a mainnet address"),
    ("not-an-address", "0.01", "a typo"),
    (STAGENET[:-1], "0.01", "an address one character short"),
    (STAGENET, "", "no amount"),
    (STAGENET, "0", "a zero amount"),
    (STAGENET, "-1", "a negative amount"),
    (STAGENET, "abc", "an unparseable amount"),
])
def test_review_refuses_before_it_opens_a_confirmation(to, amount, why):
    """Everything the review step lets through is shown to the user as a payment about to happen.
    Refusing has to be visible (a toast) and must not reach the confirm dialog, because that dialog's
    only remaining gate is one checkbox."""
    got = _review(to, amount)
    assert got["modals"] == 1, f"{why} reached the confirmation dialog"
    assert got["toasts"], f"{why} was refused silently"
    assert got["newRequests"] == 0


def test_a_pasted_payment_uri_fills_the_form_instead_of_being_treated_as_an_address():
    """Pasting the whole `monero:` URI into the address box is the permission-free alternative to
    the camera. It is parsed as DATA: address, amount and recipient land in their own fields."""
    got = _review("monero:" + STAGENET + "?tx_amount=0.25&recipient_name=Bob", "")
    assert got["to"] == STAGENET
    assert got["amount"] == "0.25"
    assert got["note"] == "Bob"
    assert got["modals"] == 2, "a pasted URI with an amount should reach the confirmation"


def test_a_pasted_uri_for_a_mainnet_address_is_refused_at_the_paste():
    got = _review("monero:" + MAINNET + "?tx_amount=0.25", "")
    assert got["modals"] == 1
    assert got["toasts"]


def test_review_carries_the_typed_decimal_string_through_to_the_confirmation():
    """The amount must never round-trip through a float on the way to the dialog: 0.000000000001 XMR
    is one atomic unit, and `Number` renders it as 1e-12, which the server cannot parse."""
    got = _review(STAGENET, "0.000000000001")
    assert got["modals"] == 2
    assert "0.000000000001" in got["confirmHtml"]
    assert "1e-12" not in got["confirmHtml"]


# --------------------------------------------------------------------------- confirm and send


CONFIRM = r"""
  openSend(%(address)s).then(async r => {
    r.sheet.querySelector('#mw-to').value = %(address)s;
    r.sheet.querySelector('#mw-amount').value = %(amount)s;
    await r.sheet.querySelector('#mw-review').onclick();
    const sheet = modals[1];
    const check = sheet.querySelector('#mw-understand');
    const button = sheet.querySelector('#mw-confirm');
    // The initial lock is markup (`<button ... disabled>`), which a DOM stub cannot reflect onto a
    // property, so it is read where the browser reads it: out of the HTML the module wrote.
    const lockedUntilUnderstood = /id="mw-confirm"[^>]*\sdisabled\b/.test(sheet.html);
    %(mutate)s
    const before = requests.length;
    if (%(press)s) await button.onclick();
    const spend = requests.slice(before);
    done({ lockedUntilUnderstood, disabled: button.disabled, label: button.textContent,
           spend, closed, toasts, sentCallback: globalThis.sentCallback || null,
           html: sheet.html });
  });
"""


def _confirm(mutate="check.checked = true; check.onchange();", press="true", amount="0.01"):
    return node(CONFIRM % {"address": json.dumps(STAGENET), "amount": json.dumps(amount),
                           "mutate": mutate, "press": press})


def test_the_send_button_is_locked_until_the_irreversibility_box_is_ticked():
    """A Monero payment cannot be clawed back and this is the last stop. If the checkbox does not
    actually drive `disabled`, the dialog is a very convincing decoration."""
    still_locked = _confirm(mutate="", press="false")
    assert still_locked["lockedUntilUnderstood"] is True, (
        "the send button ships enabled — the checkbox below it gates nothing")
    assert still_locked["spend"] == []

    unlocked = _confirm(press="false")
    assert unlocked["disabled"] is False
    assert unlocked["spend"] == [], "ticking the box must not itself send anything"


def test_unticking_the_box_locks_the_button_again():
    got = _confirm(mutate="check.checked = true; check.onchange(); "
                          "check.checked = false; check.onchange();", press="false")
    assert got["disabled"] is True


def test_the_confirmation_dialog_states_the_exact_amount_and_destination():
    got = _confirm(press="false", amount="0.05")
    assert "0.05" in got["html"] and STAGENET in got["html"]
    assert "cannot be reversed" in got["html"]
    assert "Open external wallet instead" in got["html"], (
        "the escape hatch to Feather/GUI has to be on the last screen too")


def test_sending_is_prepare_then_confirm_and_the_token_travels_alone():
    """Two calls, in that order. The second carries ONLY the confirmation token — re-sending the
    address or the amount there would make the client, not the server's Decimal parse, the thing
    that decides what leaves the wallet."""
    got = _confirm()
    assert [call["key"] for call in got["spend"]] == [
        "/api/wallet/monero/transfer/prepare", "/api/wallet/monero/transfer/confirm"]
    prepare, confirm = got["spend"]
    assert prepare["method"] == "POST" and confirm["method"] == "POST"
    assert prepare["body"]["address"] == STAGENET
    assert prepare["body"]["amount"] == "0.01"
    assert set(confirm["body"]) == {"confirmation"}, (
        f"the confirm call carried more than the token: {confirm['body']}")
    assert confirm["body"]["confirmation"] == "tok-" + "z" * 40


def test_a_successful_send_closes_the_dialog_and_reports_the_transaction_back_to_the_caller():
    """`onSent` is what posts the public tip note crediting the recipient. Monero payments are
    private, so if this never fires the person tipped is never told, by anything."""
    got = _confirm()
    assert got["closed"] == 1
    assert any("sent" in toast for toast in got["toasts"])
    assert got["sentCallback"] == {"amount": "0.01", "txid": "deadbeef"}


def test_a_refused_prepare_never_reaches_confirm_and_hands_the_dialog_back():
    """A cap refusal or an outage on the first call must not be followed by a blind confirm, and the
    button has to become pressable again — the alternative is a dialog stuck on "Sending…" over a
    payment that did not happen, which is how somebody ends up paying twice by hand."""
    got = _confirm(mutate="""
      check.checked = true; check.onchange();
      failures['/api/wallet/monero/transfer/prepare'] =
        { status:400, detail:'Amount exceeds the daily spending cap' };
    """)
    assert [call["key"] for call in got["spend"]] == ["/api/wallet/monero/transfer/prepare"]
    assert got["closed"] == 0
    assert got["disabled"] is False
    assert got["label"] == "Send now"
    assert any("daily spending cap" in toast for toast in got["toasts"]), (
        f"the reason was not shown to the user: {got['toasts']}")


def test_a_failed_confirm_is_reported_and_not_retried():
    """The token is one-use and the server has already reserved the budget, so a client that retried
    would burn the allowance and could double-send if the first call did in fact broadcast."""
    got = _confirm(mutate="""
      check.checked = true; check.onchange();
      failures['/api/wallet/monero/transfer/confirm'] =
        { status:503, detail:'Local Monero wallet is unavailable' };
    """)
    keys = [call["key"] for call in got["spend"]]
    assert keys == ["/api/wallet/monero/transfer/prepare", "/api/wallet/monero/transfer/confirm"]
    assert keys.count("/api/wallet/monero/transfer/confirm") == 1
    assert got["closed"] == 0 and got["disabled"] is False
    assert any("unavailable" in toast for toast in got["toasts"])


def test_a_send_invalidates_the_probe_cache_so_the_balance_behind_it_is_not_stale():
    """`probe()` caches for 8 seconds. Without the invalidation the wallet screen keeps showing the
    pre-payment balance and an activity list with the payment missing — which reads as a send that
    silently did nothing."""
    got = node("""
      openSend(%s).then(async r => {
        r.sheet.querySelector('#mw-to').value = %s;
        r.sheet.querySelector('#mw-amount').value = '0.01';
        await r.sheet.querySelector('#mw-review').onclick();
        const sheet = modals[1];
        sheet.querySelector('#mw-understand').checked = true;
        sheet.querySelector('#mw-understand').onchange();
        await sheet.querySelector('#mw-confirm').onclick();
        const before = requests.length;
        await window.PCMoneroWallet.probe(false);        // would be served from cache if not busted
        done({ refetched: requests.length - before });
      });
    """ % (json.dumps(STAGENET), json.dumps(STAGENET)))
    assert got["refetched"] == 4, "status/balance/address/history were served from the stale cache"


# --------------------------------------------------------------------------- the wallet screen


def test_a_hostile_transfer_row_cannot_put_markup_into_the_wallet_screen():
    """Every field on this screen comes from a JSON-RPC answer. Amounts skip `esc()` because they
    are supposed to be numeric — so the numeric coercion IS the escaping, and this is the test that
    says so. The date and the address go through `esc()` and are checked with it."""
    got = node("""
      replies['/api/wallet/monero/history'] = { in:[{
        amount_atomic: '<img src=x onerror=alert(1)>', timestamp: '<script>alert(2)</script>' }],
        out:[], pending:[], failed:[] };
      replies['/api/wallet/monero/address'] = { address: '"><script>alert(3)</script>' };
      window.__PC.VIEW = 'wallet';
      window.PCMoneroWallet.render(true).then(() => done({
        html: feed.innerHTML,
        script: /<script|onerror=/.test(feed.innerHTML),
      }));
    """)
    assert got["script"] is False, f"markup survived into #feed: {got['html'][:400]}"
    assert "&lt;script&gt;" in got["html"], "the hostile strings were dropped rather than escaped"


def test_the_wallet_screen_names_the_network_it_is_actually_on():
    """The label is the entire risk disclosure and it is now read from the node rather than being a
    constant, so it can be WRONG rather than merely missing — a mainnet wallet wearing a "testing
    only" badge is the worst outcome this screen has."""
    got = node("""
      window.__PC.VIEW = 'wallet';
      window.PCMoneroWallet.render(true).then(() => done({ html: feed.innerHTML }));
    """)
    assert "STAGENET" in got["html"]
    assert "testing only" in got["html"]
    assert "Small tips only" in got["html"] and "hot spending wallet" in got["html"]


def test_a_mainnet_wallet_screen_shouts_real_funds_and_never_says_testing_only():
    got = node("""
      replies['/api/wallet/monero/status'] = { network:'mainnet', mainnet:true,
        transfer_cap:'0.1', daily_cap:'0.5', warning:'MAINNET hot wallet' };
      window.__PC.VIEW = 'wallet';
      window.PCMoneroWallet.render(true).then(() => done({ html: feed.innerHTML }));
    """)
    assert "MAINNET" in got["html"]
    assert "REAL FUNDS" in got["html"]
    assert "testing only" not in got["html"]
    assert "STAGENET" not in got["html"]


def test_an_unreachable_wallet_paints_external_wallet_mode_rather_than_an_empty_balance():
    """A wallet that cannot be reached must not render as a wallet holding 0 XMR — that is a
    correct-looking screen with a wrong number on it, and the retry is the only way back."""
    got = node("""
      globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
      window.__PC.VIEW = 'wallet';
      window.PCMoneroWallet.render(true).then(() => done({ html: feed.innerHTML }));
    """)
    assert "Local wallet unavailable" in got["html"]
    assert "external-wallet mode" in got["html"]
    assert "Retry local wallet" in got["html"]
    assert "0 <small>XMR</small>" not in got["html"]
    assert "never receives your spend key" in got["html"]


def test_wallet_hydrates_authenticated_session_and_uses_shared_auth_fetch():
    """Extension/Nostr logins need the bearer path; a cookie-only probe falsely returns 401."""
    src = WALLET.read_text(encoding="utf-8")
    request = src[src.index("async function request("):src.index("async function probe(")]
    assert "await PC.ensureAiSession()" in request
    assert "PC&&PC.authFetch ? PC.authFetch : fetch" in request
    assert "credentials:'include'" in request


def test_history_failure_does_not_hide_a_healthy_wallet_and_probe_errors_are_visible():
    src = WALLET.read_text(encoding="utf-8")
    probe = src[src.index("async function probe("):src.index("function qr(")]
    assert "history?limit=50').catch" in probe
    assert "[monero wallet] probe failed" in probe
    assert "fallbackHtml(s.error)" in src
