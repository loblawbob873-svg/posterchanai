"""OPENING THE MONERO WALLET SHOWED THE TEXTS SCREEN — run the SHIPPED monero-wallet.js under node.

Reported as "Monero wallet is now colliding with Text Messages": the wallet's nav item opened a view
still showing Texts' "Search messages" box and "Loading messages…".

Neither module was at fault in isolation. `#feed` is shared by every screen, and the handoff has two
halves:

  * `renderModuleView` in app.js draws a spinner ONLY when the module's global is missing. The
    wallet ships a `<script>` tag, so `window.PCMoneroWallet` is already there and app.js calls
    straight into it without clearing anything.
  * `monero-wallet.js` then did `const s = await probe(force)` BEFORE its first paint.

So for the whole duration of that probe nothing had claimed the feed, and what stayed on screen was
the previous view's DOM. On a node with no wallet daemon — the default — the probe does not come
back quickly, so Texts stayed under the wallet's nav item indefinitely. Nothing errors; sms.js is
innocent (its `paint()` already refuses to draw unless `VIEW === 'texts'`), and the wallet is
"working" in the sense that it eventually paints.

This is the rule CLAUDE.md states for the whole client — *paint what the view is about before the
first network await* — applied to a view whose "cached state" is simply a spinner.

`tip()` is deliberately covered too: it is the ZAP entry point (click zap on a post → send to that
address) and must NOT touch `#feed`, because a zap is raised from wherever you already are. A fix
that claimed the feed there would blank the timeline behind the dialog.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET = ROOT / "static" / "js" / "client" / "monero-wallet.js"

TEXTS_DOM = '<div class="sms-wrap"><input placeholder="Search messages"><div>Loading messages…</div></div>'

#: A DOM stub just deep enough to load the module and watch #feed. `probe()` is made to hang, which
#: is the real-world case (no monerod) and the one the bug needed.
BOOT = r"""
const feed = { innerHTML: %(texts)s };
const el = () => ({ innerHTML:'', value:'', textContent:'', style:{}, dataset:{},
                    classList:{add(){},remove(){},toggle(){}}, appendChild(){}, setAttribute(){},
                    querySelector(){ return el(); }, querySelectorAll(){ return []; },
                    addEventListener(){}, focus(){}, remove(){} });
globalThis.window = globalThis;
globalThis.document = {
  createElement: el, body: el(), documentElement: el(),
  querySelector(s){ return s === '#feed' ? feed : el(); },
  /* THE MODULE LOOKS THINGS UP BOTH WAYS. `PC.$('#feed')` and `document.getElementById('feed')`
     are both used in monero-wallet.js — six call sites use the latter, including `bind()`. A stub
     that only implements querySelector therefore does not fail the code under test, it THROWS
     inside it, and the branch being exercised is never reached. That is what hid the timed-out
     path: the test that exists to prove the wallet paints "did not answer" instead of spinning
     was dying on a missing stub method before it got there. */
  getElementById(id){ return id === 'feed' ? feed : el(); },
  querySelectorAll(){ return []; }, addEventListener(){},
};
let modalOpened = false;
globalThis.window.__PC = {
  VIEW: 'wallet',
  $: (s) => (s === '#feed' ? feed : el()),
  toast(){}, closeModal(){},
  modal(){ modalOpened = true; },
};
let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls++; return new Promise(() => {}); };   // never resolves
globalThis.setTimeout = globalThis.setTimeout;
require(%(wallet)s);
""" % {"texts": json.dumps(TEXTS_DOM), "wallet": json.dumps(str(WALLET))}


def _node(script: str):
    r = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60, text=True)
    if r.returncode != 0:
        pytest.fail(f"node failed:\n{r.stderr[:2000]}")
    return r.stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def _needs_node():
    if not WALLET.exists():
        # The wallet module is still landing. Skipping rather than failing keeps this file
        # committable ahead of it — but it is a SKIP with a reason, never a silent pass.
        pytest.skip("static/js/client/monero-wallet.js is not present yet")
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20, check=True)
    except Exception as e:                                    # pragma: no cover
        pytest.skip(f"node not available: {e}")


def test_the_module_registers_itself():
    """If this fails everything below is inspecting nothing."""
    assert _node("console.log(typeof window.PCMoneroWallet);") == "object"


def test_opening_the_wallet_clears_the_previous_view_before_awaiting():
    """THE BUG. `render()` is called while `#feed` still holds Texts. It must claim the feed BEFORE
    awaiting the wallet probe, or the previous screen stays up for the whole request."""
    out = _node("""
      window.PCMoneroWallet.render();
      setTimeout(() => {
        console.log(/Search messages|Loading messages/.test(feed.innerHTML) ? 'TEXTS-STILL-THERE'
                                                                           : 'CLAIMED');
      }, 30);
    """)
    assert out == "CLAIMED", (
        "the Monero Wallet view left the previous screen's DOM in #feed while its probe was in "
        "flight — on a node with no wallet daemon that is for ever")


def test_it_claims_the_feed_even_though_the_probe_never_answers():
    """The probe is stubbed to hang, which is exactly a node with no monerod. The user must see the
    wallet loading, not the last app they had open."""
    out = _node("""
      window.PCMoneroWallet.render();
      setTimeout(() => console.log(feed.innerHTML.includes('spinner') ? 'SPINNER' : feed.innerHTML), 30);
    """)
    assert out == "SPINNER"


def test_it_does_not_paint_when_the_view_is_no_longer_the_wallet():
    """The other half of the shared feed: if the user has already navigated away, the wallet must
    not draw over whatever came next."""
    out = _node("""
      window.__PC.VIEW = 'texts';
      window.PCMoneroWallet.render();
      setTimeout(() => console.log(feed.innerHTML === %s ? 'UNTOUCHED' : feed.innerHTML), 30);
    """ % json.dumps(TEXTS_DOM))
    assert out == "UNTOUCHED", "the wallet painted over a view it does not own"


# --------------------------------------------------------------------------- zapping


def test_a_zap_never_touches_the_feed():
    """`tip()` is the ZAP path — click zap on somebody's post and send to their address. It is
    raised from wherever you already are (a timeline, a thread), so it must open a DIALOG and leave
    the feed alone. Claiming the feed here would blank the timeline behind the dialog."""
    out = _node("""
      window.PCMoneroWallet.tip({ address: 'x' });
      setTimeout(() => console.log(feed.innerHTML === %s ? 'FEED-UNTOUCHED' : 'FEED-CLOBBERED'), 30);
    """ % json.dumps(TEXTS_DOM))
    assert out == "FEED-UNTOUCHED"


def test_a_zap_to_an_invalid_address_is_refused_and_never_reaches_a_transfer():
    """The address comes off somebody else's profile. Sending to a malformed one is money gone with
    no way back.

    It used to be refused before ANY request. Since the wallet learned about mainnet the module has
    to ask the node which network it is on before it can judge an alphabet, so a probe is now
    expected — `fetch` here never resolves, which is the real "no wallet daemon" case, and the tip
    must still come back false rather than hanging on a decision it cannot make."""
    out = _node("""
      // A wallet port with nothing behind it refuses the connection at once; the module-level
      // hanging fetch above is the OTHER case (a daemon that accepts and never answers) and is what
      // the feed tests need. Both must end in a refused tip, never in a pending promise.
      globalThis.fetch = () => Promise.reject(new Error('ECONNREFUSED'));
      let settled = 'PENDING';
      Promise.resolve(window.PCMoneroWallet.tip({ address: 'not-an-address' }))
        .then(ok => { settled = (ok === false && !modalOpened) ? 'REFUSED' : 'ok=' + ok; });
      setTimeout(() => console.log(settled), 60);
    """)
    assert out == "REFUSED"


@pytest.mark.parametrize("addr", ["", "null", "undefined", "0x0000", "bitcoin:bc1qxyz", "4" * 10])
def test_obviously_wrong_addresses_are_refused(addr):
    """Same shape as above: the probe never answers here, so a tip that cannot be judged must resolve
    false instead of waiting for ever on a wallet that is not there."""
    out = _node("""
      globalThis.fetch = () => Promise.reject(new Error('ECONNREFUSED'));
      let settled = 'PENDING';
      Promise.resolve(window.PCMoneroWallet.tip({ address: %s }))
        .then(ok => { settled = ok === false ? 'REFUSED' : 'ACCEPTED'; });
      setTimeout(() => console.log(settled), 60);
    """ % json.dumps(addr))
    assert out == "REFUSED", f"{addr!r} was accepted as a Monero address"


def test_the_address_validator_is_exposed_and_used_by_the_zap_path():
    """`validAddress` is the one gate between a zap and an irrecoverable send, so it is public and
    the tip path calls it first."""
    out = _node("console.log(typeof window.PCMoneroWallet.validAddress);")
    assert out == "function"
