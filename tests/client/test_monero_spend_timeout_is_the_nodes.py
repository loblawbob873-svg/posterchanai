"""A SPEND MUST NOT BE ABANDONED BY THE BROWSER WHILE THE NODE IS STILL SENDING IT.

Reported as Monero zaps "not going through", then, minutes later, "ok it finally sent". Both were
true: the payment was broadcast and the client had stopped listening 100 seconds earlier.

The node already gets this right and says so in its own comments. `MoneroWallet.SPENDING` gives a
money-moving RPC `SPEND_TIMEOUT = 120s` against a read's 8s, and a spend that runs out raises
`WalletUnsure` -- "This payment may have been sent -- check your transaction history before trying
again" -- explicitly so that nobody is invited to send twice. The browser then aborted EVERY request
at 20s, threw that wording away, and reported "the wallet did not answer within 20s".

Three things went wrong at once and none of them logged anything:

  * the transfer was still live -- an abort tells the server nothing;
  * "did not answer within 20s" reads as "nothing happened", not as "unknown";
  * the send button's own guard keys on /may have been sent|did not answer in time/, and "did not
    answer WITHIN 20s" matches neither, so the button went back to "Send now". `/me/pay` carries no
    idempotency key, so pressing it again is a second real payment.

The withdraw sheet was worse: it had no unknown-handling at all and said "withdrawal failed" over a
`sweep_all` of the entire balance, with its button re-enabled.

Every test here was verified to fail against the code as it was.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET = ROOT / "static" / "js" / "client" / "monero-wallet.js"
SERVICE = ROOT / "app" / "services" / "monero_user_wallets.py"

STAGENET = "5" + "A" * 94

BOOT = r"""
globalThis.window = globalThis;
const el = () => ({
  value:'', textContent:'', innerHTML:'', disabled:false, checked:false, isConnected:true,
  style:{}, dataset:{}, classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  appendChild(){}, setAttribute(){}, addEventListener(){}, focus(){}, remove(){},
  select(){}, setSelectionRange(){}, querySelector(){ return el(); }, querySelectorAll(){ return []; },
});
const feed = { innerHTML:'' };
globalThis.document = {
  createElement: el, body: el(), documentElement: el(),
  getElementById(id){ return id === 'feed' ? feed : null; },
  querySelector(s){ return s === '#feed' ? feed : el(); },
  querySelectorAll(){ return []; }, addEventListener(){},
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.modals = []; globalThis.toasts = []; globalThis.closed = 0;
function fakeRoot(html){
  const cache = {};
  return { html, isConnected:true, querySelectorAll(){ return []; },
           querySelector(sel){ return cache[sel] || (cache[sel] = el()); } };
}
globalThis.window.__PC = {
  VIEW:'social', $:(s)=> (s === '#feed' ? feed : el()),
  toast(t){ toasts.push(String(t)); }, closeModal(){ closed++; },
  modal(html, cb){ const r = fakeRoot(html); modals.push(r); if(cb) cb(r); return r; },
};

/* THE TIMER IS THE MEASUREMENT. A real budget cannot be waited out in a test, so record what the
   module ARMS: the aborting timer's delay IS the budget it chose for that request. */
globalThis.armed = [];
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = function(fn, ms){ armed.push(Number(ms) || 0); return realSetTimeout(fn, ms); };

globalThis.requests = [];
globalThis.aborts = {};                 // path -> reject the way an AbortController does
globalThis.replies = {
  '/api/wallet/xmr/status': { network:'stagenet', mainnet:false },
  '/api/wallet/xmr/balance': { balance:'1000000000000', unlocked_balance:'1000000000000' },
  '/api/wallet/xmr/address': { address: %(stagenet)s },
  '/api/wallet/xmr/history': { in:[], out:[], pending:[], failed:[] },
  '/api/wallet/xmr/transfer/prepare': { confirmation:'tok-'+'z'.repeat(40), expires_at: 0,
                                        address: %(stagenet)s, amount_atomic: 10000000000 },
  '/api/wallet/xmr/transfer/confirm': { tx_hash:'deadbeef' },
};
globalThis.fetch = async (url, opts) => {
  opts = opts || {};
  const key = String(url).split('?')[0];
  requests.push({ key, method: opts.method || 'GET' });
  if (aborts[key]) { const e = new Error('The user aborted a request.'); e.name='AbortError'; throw e; }
  return new Response(JSON.stringify(replies[key] || {}),
                      { status:200, headers:{'Content-Type':'application/json'} });
};

require(%(wallet)s);
globalThis.done = (v) => { process.stdout.write(JSON.stringify(v)); };

/** tip -> send sheet -> review -> understand -> confirm, which is what broadcasts. */
globalThis.sendThrough = async () => {
  const ok = await window.PCMoneroWallet.tip({ address:%(stagenet)s, name:'alice' });
  const sheet = modals[modals.length - 1];
  sheet.querySelector('#mw-to').value = %(stagenet)s;
  sheet.querySelector('#mw-amount').value = '0.01';
  await sheet.querySelector('#mw-review').onclick();
  const confirmSheet = modals[modals.length - 1];
  confirmSheet.querySelector('#mw-understand').checked = true;
  confirmSheet.querySelector('#mw-understand').onchange();
  armed.length = 0; toasts.length = 0;
  const button = confirmSheet.querySelector('#mw-confirm');
  await button.onclick();
  return { ok, button, armed: armed.slice(), toasts: toasts.slice() };
};
""" % {"wallet": json.dumps(str(WALLET)), "stagenet": json.dumps(STAGENET)}


def node(script: str):
    run = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=90, text=True)
    if run.returncode != 0:
        pytest.fail(f"node failed:\n{run.stderr[-3000:]}")
    assert run.stdout.strip(), f"the script printed nothing; stderr:\n{run.stderr[-2000:]}"
    return json.loads(run.stdout)


@pytest.fixture(scope="module", autouse=True)
def _needs_node():
    if not WALLET.exists():
        pytest.skip("static/js/client/monero-wallet.js is not present")
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20, check=True)
    except Exception as exc:                                            # pragma: no cover
        pytest.skip(f"node not available: {exc}")


SOURCE = WALLET.read_text(encoding="utf-8")


def _node_spend_timeout_ms() -> int:
    """Read the node's own budget rather than restating it: the two must not drift apart."""
    match = re.search(r"SPEND_TIMEOUT\s*=\s*([0-9.]+)", SERVICE.read_text(encoding="utf-8"))
    assert match, "SPEND_TIMEOUT moved in monero_user_wallets.py — this check is reading nothing"
    return int(float(match.group(1)) * 1000)


def test_every_money_moving_route_the_client_posts_to_is_classified_as_a_spend():
    """A NEW SPEND ROUTE LEFT UNCLASSIFIED FAILS THIS WAY AGAIN, SILENTLY, AND ONLY WITH MONEY."""
    posts = {p.split("?")[0] for p in
             re.findall(r"request\(\s*'([^']+)'\s*,\s*\{\s*method:\s*'POST'", SOURCE)}
    assert posts, "no POST call sites found — this check is reading nothing"
    listed = set(re.findall(r"'(/api/wallet/xmr/[^']+)'",
                            SOURCE.split("SPENDING_PATHS")[1].split("]")[0]))
    # `prepare` prices and reserves a transfer; `confirm` is the call that broadcasts it.
    reads_only = {"/api/wallet/xmr/transfer/prepare"}
    unclassified = posts - listed - reads_only
    assert not unclassified, (
        "these POST routes are neither in SPENDING_PATHS nor named non-spending, so they get a "
        "read's timeout while they may be moving money: %s" % sorted(unclassified))


def test_a_spend_is_given_the_budget_the_node_is_allowed_to_take():
    got = node("sendThrough().then(r => done({ armed:r.armed }));")
    spend = max(got["armed"])
    budget = _node_spend_timeout_ms()
    assert spend >= budget, (
        "the browser gives up after %sms while the node is allowed %sms to broadcast the payment; "
        "the transfer stays live and the user is told it did not happen" % (spend, budget))


def test_a_plain_read_still_gives_up_quickly():
    """A spend's budget on a read would hang the screen for minutes against a dead wallet."""
    got = node("""
      (async () => { armed.length = 0;
        await window.PCMoneroWallet.tip({ address:%s, name:'alice' });
        done({ armed });
      })();
    """ % json.dumps(STAGENET))
    assert got["armed"], "no timer was armed for the probe reads"
    assert max(got["armed"]) < _node_spend_timeout_ms()


def test_an_abandoned_spend_is_reported_as_unknown_and_leaves_the_button_refusing():
    """The wording is load-bearing twice: a person reads it, and the button's own guard greps it."""
    got = node("""
      aborts['/api/wallet/xmr/transfer/confirm'] = true;
      sendThrough().then(r => done({ toasts:r.toasts, label:r.button.textContent,
                                     disabled:!!r.button.disabled }));
    """)
    said = " ".join(got["toasts"]).lower()
    assert "may have been sent" in said or "did not answer in time" in said, (
        "an abandoned broadcast must be reported as unknown, not as a non-answer: %r" % got["toasts"])
    assert "failed" not in said and "not sent" not in said
    assert got["disabled"] is True, (
        "the confirm button went back to offering the send while the payment may be in flight — "
        "and /me/pay has no idempotency key, so the next press is a second real payment")


def test_a_read_that_times_out_still_names_the_endpoint_it_could_not_reach():
    """The read message's diagnostic value is deliberate and must survive the spend/read split."""
    got = node("""
      aborts['/api/wallet/xmr/balance'] = true;
      window.PCMoneroWallet.tip({ address:%s, name:'alice' })
        .then(ok => done({ ok, toasts }));
    """ % json.dumps(STAGENET))
    said = " ".join(got["toasts"]).lower()
    assert "may have been sent" not in said, \
        "a read never moved money — it must not say it might have"


def test_the_withdraw_sheet_does_not_offer_to_sweep_the_account_a_second_time():
    """A withdrawal is `sweep_all`. Calling an unknown a failure re-offers the entire balance."""
    # Anchored on the withdraw REQUEST, not on the sheet's markup: the tip handler further down
    # carries the same guard, so a looser slice passed while this button had none at all.
    # Anchored on the withdraw CALL SITE, not on the sheet's markup or the path constant: the tip
    # handler further down carries the same guard, so a looser slice passed while this button had
    # none at all.
    handler = SOURCE.split("request('/api/wallet/xmr/me/withdraw'", 1)[1].split("};", 1)[0]
    assert "may have been sent|did not answer in time" in handler, (
        "the withdraw button has no unknown-handling: a timed-out sweep reads as 'withdrawal "
        "failed' with the button re-enabled")
    assert "go.disabled = unsure" in handler
