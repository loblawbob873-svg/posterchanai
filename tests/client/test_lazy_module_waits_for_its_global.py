"""A SCRIPT THAT HAS LOADED HAS NOT NECESSARILY PUBLISHED ITSELF.

`_withModule` is how every lazily-loaded app is reached — Notes, SMS, the phone shell, and the
Monero wallet. It read the module's global ONCE, on the script's `onload`, and answered null if it
was not there yet.

Several modules deliberately defer their own boot until the client exists: monero-wallet.js retries
every 40ms until `window.__PC` is set, and only then publishes `PCMoneroWallet`. So the script can
be fully loaded while its global is still undefined, and the caller is told the module is not
available. For a tip that means silently using the external wallet flow on a page where the built-in
wallet was present the whole time — indistinguishable from the wallet being broken.

Found while checking the live bundle in a browser: the script loaded and `PCMoneroWallet` was
undefined, because that page had no client for the module to attach to. The isolated case was not a
bug, but the race it exposed is real.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _with_module() -> str:
    at = APP.index("  function _withModule(file, global, fn){")
    return APP[at:APP.index("  const _withPhoneShell", at)]


def test_it_waits_for_the_global_to_appear():
    body = _with_module()
    assert "!window[global]" in body and "setTimeout(r, 40)" in body, (
        "the module's global is read once on load — a module that defers its own boot is reported "
        "as absent, and the caller silently falls back")


def test_the_wait_is_bounded():
    """A module that has not published quickly is not going to; every caller treats null as
    'not available' and must not be held indefinitely."""
    body = _with_module()
    assert "i < 25" in body, "the wait for a module's global is unbounded"


def test_an_already_loaded_module_is_still_returned_immediately():
    """The fast path must not grow a wait — this runs on every call."""
    body = _with_module()
    assert body.index("if(window[global]) {") < body.index("_lateLoad[file]")


def test_the_deferred_boot_this_exists_for_is_still_there():
    """monero-wallet.js publishes itself only once `__PC` exists. If that ever stops being true this
    test's reasoning changes, and the wait can be reconsidered."""
    wallet = (ROOT / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")
    flat = wallet.replace(" ", "").replace("\n", "")
    assert "PC=root.__PC;if(!PC)returnsetTimeout(boot,40)" in flat
