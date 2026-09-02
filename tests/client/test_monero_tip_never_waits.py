"""TIPPING MUST NOT WAIT ON THE LOCAL WALLET, AND A BUSY WALLET IS NOT AN ABSENT ONE.

Both found reviewing the Monero surface for launch, with the node still syncing — which is the state
every new deployment is in for hours, so these are the launch conditions, not edge cases.

1. `doXmrTip` asks the local micro-wallet first and falls through to the non-custodial URI/QR flow
   when it answers false. That design is right and the `await` undid it: `probe` is allowed 20
   seconds, and monero-wallet-rpc BLOCKS while it scans blocks, so on a catching-up node tapping ɱ
   did nothing whatsoever for twenty seconds before the modal appeared. Measured here: 20,000ms →
   1,205ms, and 0ms with an answer this page already has.

2. A refused connection and a connection that went quiet became the same `WalletError`, so both
   rendered as "This device is in safe external-wallet mode" — a sentence describing an unconfigured
   wallet, shown for hours to somebody whose wallet is fine and merely reading the chain.

Driven against the shipped module, because the questions are "how long did it take" and "what is on
screen", and nothing static can answer either.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCEN = Path(__file__).with_name("monero_tip_scenarios.mjs")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node unavailable")


@pytest.fixture(scope="module")
def seen():
    done = subprocess.run([NODE, str(SCEN)], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_wallet_that_never_answers_does_not_hold_up_the_tip(seen):
    """THE LAUNCH BUG. The fallback exists so tipping never depends on this optional wallet; it has
    to not depend on its latency either."""
    assert seen["slow"]["answered"] is False, (
        "a wallet that never answered was treated as usable, so the URI flow never ran")
    assert seen["slow"]["tookMs"] < 4000, (
        f"tapping the tip button blocked for {seen['slow']['tookMs']}ms waiting on a scanning "
        f"wallet — this was 20s before the fix, with nothing on screen the whole time")


def test_an_answer_this_page_already_has_costs_nothing(seen):
    """The fast path has to stay fast: a fresh probe result is used as-is, with no request."""
    assert seen["cached"]["answered"] is True
    assert seen["cached"]["requests"] == 0, "a cached wallet state still re-asked the server"
    assert seen["cached"]["tookMs"] < 500


def test_a_wallet_on_the_wrong_network_still_refuses(seen):
    """The guard that must survive the speed-up: a stagenet wallet must never be offered for a
    mainnet address. Getting this wrong sends real money with a testnet wallet."""
    assert seen["wrongNetwork"]["answered"] is False


def test_a_busy_wallet_says_it_is_catching_up(seen):
    """It is reading the chain, not missing. This is what every node shows for its first hours."""
    assert seen["busyCard"]["catching"] is True, "a busy wallet does not say it is catching up"
    assert seen["busyCard"]["external"] is False, (
        "a wallet that is merely scanning is still described as 'safe external-wallet mode', which "
        "reads as broken")


def test_an_absent_wallet_still_says_external_wallet_mode(seen):
    """The distinction has to cut both ways, or it is just a nicer word for every failure."""
    assert seen["absentCard"]["external"] is True
    assert seen["absentCard"]["syncing"] is False
