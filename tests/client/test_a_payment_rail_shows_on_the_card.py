"""A PAYMENT RAIL THE TIP SHEET CAN USE MUST BE VISIBLE ON THE CARD.

Reported about deallen@erybody.com: "i can zap him but no icon that he has monero like the others."

Measured on his real events before writing a line of fix: his kind-0 carries `lud16` and nothing
else, and his NIP-A3 kind-10133 carries exactly one `payto monero`. `doTip` resolves 10133 through
the payment-target resolver, so paying him worked perfectly — while the ɱ mark on the card was
resolved from the author's kind-0 (plus a per-note `monero_address` tag) and could never see it. So
the only way to discover that he takes Monero was to open the tip sheet on the off chance, and the
card said the opposite.

TWO SURFACES ANSWERING THE SAME QUESTION FROM DIFFERENT FACTS is the shape, and this one hid for as
long as it did because the half that handles MONEY was the correct half. Nothing was broken, nothing
logged, and every Monero user whose client writes NIP-A3 rather than a kind-0 field — which is what
NIP-A3 is FOR — looked unpayable to everybody here.

The runtime beside this file drives the SHIPPED app.js: the learn step inside `flushProfiles`, the
mark inside `actsRow`, and the late patch inside `_tipMarks`, against real signed events.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
APP = ROOT / "static/js/client/app.js"
RUNTIME = Path(__file__).with_name("tip_rail_mark_runtime.cjs")


@pytest.mark.skipif(not NODE, reason="node is required to run the shipped client code")
def test_a_payto_rail_reaches_the_card():
    done = subprocess.run([NODE, str(RUNTIME)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr or done.stdout
    assert "payment-rail mark scenarios" in done.stdout, done.stdout


def test_the_rails_ride_the_profile_request_rather_than_their_own():
    """The resolver was deliberately written to discover 10133 only when a tip flow opens, so a
    timeline does not pay one relay query per author. The fix must keep that promise: a second
    FILTER on the batch is free, a second query is not."""
    src = APP.read_text(encoding="utf-8")
    flush = src[src.index("async function flushProfiles(){"):src.index("async function fetchMyProfile(){")]
    queries = re.findall(r"Relay\.query\(", flush)
    assert len(queries) == 1, (
        "flushProfiles now makes %d relay queries; the payment rails must ride the profile REQ as "
        "an extra filter, never as a query of their own" % len(queries))
    assert "10133" in flush, "the profile batch no longer asks for payment targets at all"


def test_a_rail_never_becomes_the_address_a_tip_is_paid_to():
    """`data-xmr` is what `doTip` pays when the note has been evicted from the Store. The rails map
    holds TYPES, not addresses, and is populated from events this client did not necessarily fetch
    itself — so it may raise a mark and must never reach a wallet."""
    src = APP.read_text(encoding="utf-8")
    marks = src[src.index("function _tipMarks(n, p){"):src.index("function decorateProfiles(){")]
    for line in marks.splitlines():
        if "dataset.xmr =" in line or "dataset.xmr=" in line:
            assert "addr" in line, (
                "data-xmr is being set from something other than the resolved address: " + line.strip())
