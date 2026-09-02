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


def test_the_same_click_gives_the_same_answer(seen):
    """A PAYMENT UI MUST NOT DECIDE ON A STOPWATCH.

    Fixing the twenty-second freeze introduced something worse: the tip raced the probe against a
    1200ms timer, so an identical click returned the built-in wallet or the external flow depending
    on which won. Reported as "the monero chooser is like different each time! sometimes it lets it
    from local wallet, sometimes not".

    The answer now comes from what the wallet SAID. The module asks once as it loads, so the state
    is almost always already there when somebody clicks; when it is not, the tip waits for it rather
    than guessing. Driven at five different wallet latencies here — the decision must not move."""
    assert seen["deterministic"]["allSame"] is True, (
        f"identical clicks gave different answers at different latencies: "
        f"{seen['deterministic']['answers']} — the chooser is deciding on a timer")
    assert seen["deterministic"]["answers"][0] is True, (
        "a reachable, funded wallet was not used at any latency")


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


def test_a_client_side_abort_is_read_as_busy_too(seen):
    """WHOEVER'S CLOCK RAN OUT FIRST. The node answers 503 with its own wording when its RPC budget
    (8s by default) expires — the usual case — but that budget is an operator setting allowed up to
    30s and this client aborts at 20. Above 20 the abort wins and the message is OURS. Matching only
    the node's wording would make the "catching up" card quietly stop appearing on exactly the nodes
    whose wallet is slowest, which is the population it exists for."""
    assert seen["abortCard"]["syncing"] is True, (
        "a wallet that blew the client's own timeout is described as absent again")
    assert seen["abortCard"]["external"] is False


def test_the_sync_question_is_asked_once_per_visit_not_once_per_paint(seen):
    """`bind()` runs from EVERY paint, `render` paints twice (cached, then fresh) and `_watch`
    repaints behind that. Each ask makes the node call `refresh` — real work on the very wallet
    being reported as too busy to answer. Four paints here; one request."""
    assert seen["syncCalls"]["calls"] == 1, (
        f"the wallet was asked to refresh {seen['syncCalls']['calls']} times for one visit")
    assert seen["syncCalls"]["banner"] is True, (
        "de-duplicating the request also stopped the banner appearing, which defeats the point")


def test_an_empty_wallet_hands_the_tip_back_instead_of_offering_to_spend(seen):
    """Reported as "monero can't even zap", then "monero rejected request?" — which is exactly what
    happened. The wallet ANSWERS, so this path was taken, the send dialog opened, and
    monero-wallet-rpc refused the transfer because there was nothing to send. Measured on the node:
    balance 0, because its daemon was 284,871 blocks behind and still catching up.

    Answering false hands the tip to the non-custodial URI/QR flow, which needs no local wallet and
    is the thing that actually works. The local wallet takes over again on its own once it has
    spendable funds."""
    assert seen["emptyWallet"]["answered"] is False, (
        "an empty wallet still offers to spend, and the transfer is refused after the user has "
        "typed an amount")
    assert seen["emptyWallet"]["opened"] is False, "the send dialog was opened anyway"


def test_a_locked_balance_keeps_the_built_in_wallet_and_explains_the_wait(seen):
    """EMPTY AND LOCKED ARE DIFFERENT ANSWERS, and conflating them broke the web UI.

    Monero locks the CHANGE from every send for 10 blocks, so a successful tip drops the spendable
    balance to zero for about twenty minutes. Refusing on spendable balance therefore meant the
    built-in wallet worked once and then quietly handed every following zap to the external flow —
    reported as "you fixed android but broke webui". Measured on the real wallet at the time:
    balance 0.00788058, unlocked 0, blocks_to_unlock 4.

    So a locked wallet keeps the built-in path and the dialog states the wait, instead of accepting
    a payment the daemon will refuse."""
    assert seen["lockedKeepsWallet"]["answered"] is True, (
        "a wallet whose balance is merely locking hands the tip away again — it stops working for "
        "twenty minutes after every successful tip")
    assert seen["lockedKeepsWallet"]["saysLocking"] is True, "nothing tells the user why"
    assert seen["lockedKeepsWallet"]["saysMinutes"] is True, (
        "the wait is not quantified, so 'locked' reads as 'broken'")


def test_a_wallet_holding_nothing_still_hands_the_tip_back(seen):
    """The one case where the built-in wallet genuinely cannot help."""
    assert seen["trulyEmpty"]["answered"] is False
    assert seen["trulyEmpty"]["opened"] is False


def test_a_funded_wallet_still_uses_the_local_path(seen):
    """The feature has to survive its own guard."""
    assert seen["fundedWallet"]["answered"] is True
