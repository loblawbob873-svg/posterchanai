"""THE BUILT-IN WALLET HAS TO BE LOADED BEFORE IT CAN BE OFFERED.

Reported as "monero android app not using built-in wallet! desktop works but not android".

Nothing platform-specific was wrong. `doXmrTip` tested `window.PCMoneroWallet` directly, and that
global is set by monero-wallet.js — which is LAZY-LOADED, only when the Wallet screen is opened. So
the built-in wallet was offered exactly when the user happened to have visited Wallet in that
session, and silently skipped otherwise. On the desktop that screen had been opened; on the phone it
had not. Both bundles ship the file (`cp static/js/client/*.js`), so it was always there to load —
nothing ever asked for it.

The fallback is unchanged and still fail-closed: `_withModule` answers null where the file is
absent, and `tip()` answers false whenever the wallet is unavailable, has no spendable balance, or
the address is for another network — every one of those continues into the non-custodial URI/QR
flow, which needs no wallet at all.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _tip_block() -> str:
    at = APP.index("async function doXmrTip(")
    return APP[at:APP.index("const name=enc(p.name", at)]


def test_the_wallet_module_is_loaded_on_demand():
    """THE BUG. Testing the global alone makes the feature depend on which screens you happened to
    open, which is indistinguishable from it being broken on one platform."""
    block = _tip_block()
    assert "_withModule('monero-wallet.js', 'PCMoneroWallet')" in block, (
        "the built-in wallet is only offered when its screen has already been opened this session")


def test_an_already_loaded_module_is_used_directly():
    """No second script load, and no await, once it is there."""
    block = _tip_block()
    assert "window.PCMoneroWallet\n        || await _withModule(" in block


def test_the_fallback_is_still_fail_closed():
    """A missing module, an unavailable wallet or a refused tip must all reach the URI/QR flow —
    tipping cannot depend on this optional integration."""
    block = _tip_block()
    assert "catch(_){}" in block, "a failure here would now break tipping entirely"
    assert "if(_xmrWallet && await _xmrWallet.tip(" in block, (
        "a null module would be dereferenced instead of falling through")


def test_the_address_is_still_checked_before_anything_is_loaded():
    """No point loading a wallet for a post with no Monero address on it."""
    block = _tip_block()
    assert block.index("no Monero address on this post or profile") < block.index("_withModule(")
