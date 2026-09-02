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


def test_a_locked_wallet_lets_the_tip_through_and_says_why(seen):
    """A WALLET THAT CANNOT PAY MUST NOT STAND IN THE WAY OF PAYING.

    Three versions of this. Refusing on spendable balance sent every zap external for the ~20
    minutes after a send (Monero locks the change from a payment for 10 blocks), so the built-in
    wallet appeared to stop working after each use — "you fixed android but broke webui". Keeping
    the built-in sheet fixed that and broke something worse: the sheet opens, refuses the amount,
    and the zap cannot be made at all — "i can't zap again because it says I have to wait 18 min
    despite nothing pending in my wallet". Nothing IS pending; it is the change from their own
    sends, and the number was right while the behaviour was useless.

    Somebody clicking ɱ wants to pay. So a wallet with nothing spendable hands the tip to the
    external flow — which works right now — and says why, so the built-in wallet going quiet for a
    while is explained rather than mysterious."""
    assert seen["lockedKeepsWallet"]["answered"] is False, (
        "a locked wallet still takes the tip, and then refuses the amount — the zap cannot be made")
    assert seen["lockedKeepsWallet"]["opened"] is False, (
        "the built-in send sheet opened for a wallet that cannot send")
    assert seen["lockedKeepsWallet"]["toldWhy"] is True, (
        "the built-in wallet went quiet with no explanation and no unlock time")


def test_a_wallet_holding_nothing_still_hands_the_tip_back(seen):
    """The one case where the built-in wallet genuinely cannot help."""
    assert seen["trulyEmpty"]["answered"] is False
    assert seen["trulyEmpty"]["opened"] is False


def test_a_funded_wallet_still_uses_the_local_path(seen):
    """The feature has to survive its own guard."""
    assert seen["fundedWallet"]["answered"] is True


def test_an_early_failed_probe_does_not_latch(seen):
    """THE ONE THAT KEPT IT BROKEN. `warm()` asks as soon as the module loads, which can be before
    the wallet session is usable — that probe 401s and records `available:false`. A tip that trusts
    the cache at any age then reads that stale failure for the rest of the page's life, and the
    built-in wallet is never used again however reachable and funded it is. Reported as "i open a
    post and click zap, it's still not using the fucking built-in monero wallet".

    A latch set BEFORE the attempt it describes — the exact shape this codebase keeps paying for.
    Only a POSITIVE answer is cached, and only briefly; anything else asks the wallet again."""
    assert seen["recoversAfterEarlyFailure"]["answered"] is True, (
        "a probe that failed before the session was ready still decides every later tip")
    assert seen["recoversAfterEarlyFailure"]["opened"] is True, (
        "the built-in wallet's send sheet never opened")


def test_the_send_sheet_offers_the_same_one_tap_amounts(seen):
    """The built-in wallet's sheet had a bare number box while the URI/QR modal beside it had preset
    chips and remembered the last amount — so using the wallet that is meant to be the SEAMLESS path
    meant typing an amount every time. Reported as "it's missing the pre-filled zap amounts in it".

    The list is passed in from `xmrPresets()` (a synced user setting) rather than owned by the wallet
    module: two lists of "your usual tip" drift the first time somebody edits one."""
    assert seen["presets"]["chips"] == 3, "the send sheet offers no one-tap amounts"
    assert seen["presets"]["prefilled"] is True, "the last amount you sent is not filled in"
    assert seen["presets"]["labelled"] is True, "a preset chip does not show its amount"


def test_no_configured_presets_draws_no_empty_row(seen):
    assert seen["noPresets"]["chips"] == 0
    assert seen["noPresets"]["row"] is False, "an empty preset row is drawn with nothing in it"


# ── THE USER'S OWN WALLET ────────────────────────────────────────────────────────────────────────
#
# Everything above concerns the NODE's wallet, which is admin-only: for anybody who is not the
# operator every call is a 403, `tip()` answers false, and the zap falls through to the external
# flow. Measured on the relay, that left 890 of 900 profiles untippable in one tap.
#
# This is the other half — a wallet the node keeps for the signed-in user. It is CUSTODIAL, it is
# tried after the operator's wallet and before the external flow, and it declines quietly whenever
# it cannot help so the non-custodial path is never taken away.

def test_a_user_with_a_funded_wallet_gets_the_built_in_sheet(seen):
    assert seen["userWallet"]["answered"] is True
    assert seen["userWallet"]["sheet"] is True


def test_the_sheet_says_who_holds_the_money(seen):
    """Custody has to be stated where the person is deciding to use it, not buried in a doc."""
    assert seen["userWallet"]["saysCustodial"] is True, (
        "the sheet does not tell the user this wallet is held by the server")
    assert seen["userWallet"]["offersExternal"] is True, (
        "no way to use their own wallet instead, from the screen where they would want it")


def test_a_locked_user_wallet_hands_the_tip_on(seen):
    """Same rule as the node wallet: a wallet that cannot pay must not stand in the way of paying."""
    assert seen["userWalletLocked"]["answered"] is False
    assert seen["userWalletLocked"]["opened"] is False
    assert seen["userWalletLocked"]["toldWhy"] is True


def test_a_node_without_user_wallets_declines_silently(seen):
    """Most nodes will not offer this. It must cost them nothing and say nothing."""
    assert seen["userWalletOff"]["answered"] is False
    assert seen["userWalletOff"]["opened"] is False


def test_a_wrong_network_address_never_opens_a_sheet(seen):
    assert seen["userWalletWrongNet"]["answered"] is False
    assert seen["userWalletWrongNet"]["opened"] is False


def test_the_three_paths_are_tried_in_the_right_order():
    """Operator wallet, then the user's, then the URI flow. If the user's wallet came first the
    operator would tip from a custodial account instead of their own; if it came after the URI flow
    it would never be reached at all."""
    from pathlib import Path
    app = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")
    at = app.index("async function doXmrTip(")
    block = app[at:app.index("const name=enc(p.name", at)]
    assert block.index("_xmrWallet.tip(_tipOpts)") < block.index("_xmrWallet.meTip(_tipOpts)"), (
        "the user's custodial wallet is tried before the operator's own")
    tail = app[at:at + 6000]
    assert tail.index("_xmrWallet.meTip(_tipOpts)") < tail.index("monero:"), (
        "the user's wallet is never reached — the URI flow runs first")


def test_a_normal_user_never_sees_the_node_wallets_refusal(seen):
    """Reported by the first person who signed up: "i made new user and wallet broken — this account
    cannot open the node wallet, sign in as the node operator".

    That message is correct and addressed to the wrong audience. The node wallet is admin-only, so
    for everybody who is not the operator every call to it is a 403 — showing them that refusal is
    showing them somebody else's error, on a screen called "Monero Wallet" while this node is
    holding a wallet for them."""
    assert seen["userScreen"]["showsAdminRefusal"] is False, (
        "a normal user is still shown the operator's admin-only refusal")
    assert seen["userScreen"]["showsTheirBalance"] is True, "their own balance is not shown"
    assert seen["userScreen"]["showsAddress"] is True, "no receiving address — they cannot be tipped"


def test_the_user_screen_states_who_holds_the_money_and_offers_the_exit(seen):
    """Custody said where the person is looking at the balance, and the way out on the screen
    itself rather than buried in a menu."""
    assert seen["userScreen"]["saysCustodial"] is True
    assert seen["userScreen"]["hasWithdraw"] is True, (
        "no withdraw on the wallet screen — that makes the balance an IOU")


def test_a_node_without_user_wallets_still_explains_itself(seen):
    """Most nodes will not offer this. With nothing of the user's to show, the node wallet's own
    message is the honest one — the fix must not blank the screen for those."""
    assert seen["noUserWallets"]["fallsBackToNodeMessage"] is True


def test_a_wallet_that_never_answers_does_not_eat_the_tip(seen):
    """Reported as "i am trying to zap a post and chose Monero wallet and nothing happens".

    `request()` begins with `ensureAiSession()`, which for a Nostr login mints a bearer THROUGH THE
    SIGNER — that can wait on a phone, on a human approval, or on nothing at all. The tip awaited a
    probe that never resolved, so no dialog opened and the non-custodial QR flow, which needs no
    session whatsoever, was never reached. Reproduced here: with the signer never answering the
    scenario hung outright until the probe was bounded.

    The deadline is on the PROBE, not the caller — bounding the whole flow could let a dialog open
    seconds after the fallback had already drawn one."""
    for key in ("signerHangs", "signerHangsMe"):
        assert seen[key]["answered"] is False, f"{key}: a stuck wallet swallowed the tip"
        assert seen[key]["tookMs"] < 5000, (
            f"{key}: the tip button waited {seen[key]['tookMs']}ms on a wallet that never answered")


def test_a_session_refusal_is_never_painted_as_a_wallet_fault(seen):
    """Reported as "not even the wallet works — sign in with a Nostr account to start an app
    session", by somebody who WAS signed in.

    Warming the probe at module load is what makes the tip decision instant, and the module loads
    during boot or on the first tip — both of which can precede sign-in. `ensureAiSession()` throws
    that sentence when there is no identity yet, and it was being stored as the WALLET's error and
    printed on the wallet screen."""
    assert seen["beforeSignIn"]["showsSessionError"] is False, (
        "the client's own not-signed-in message is painted as a wallet failure")
    assert seen["beforeSignIn"]["showsSpinner"] is True, (
        "with nothing readable yet, a spinner is the honest answer")


def test_the_wallet_works_once_the_session_exists(seen):
    """The refusal must not latch — the next probe has a session and has to answer properly."""
    assert seen["afterSignIn"]["showsBalance"] is True
    assert seen["afterSignIn"]["stillShowsSessionError"] is False


def test_a_dead_signer_extension_is_not_reported_as_a_broken_wallet(seen):
    """Taken verbatim from the console: "could not establish your app session: Could not establish
    connection. Receiving end does not exist." at `__pcNostrProvider`, with "theme sync skipped"
    failing identically in the same session.

    That is a browser-EXTENSION fault — a NIP-07 content script that cannot reach its own background
    worker. Every authenticated call goes through the signer, so the wallet is merely the loudest
    thing about it. Reported as "Monero wallet is not even loading now", which sent us looking at
    monerod, the pool daemon and the wallet RPC — all of which were healthy."""
    assert seen["signerDead"]["namesTheSigner"] is True, (
        "a dead signer extension is still presented as a wallet failure")
    assert seen["signerDead"]["exoneratesTheWallet"] is True, (
        "nothing tells the reader the wallet and the node are fine")
    assert seen["signerDead"]["hasRetry"] is True
    assert seen["signerDead"]["spinner"] is False


def test_the_wallet_screen_never_spins_for_ever(seen):
    """The deadline was on `tip()` and `meTip()` and NOT on `render()`, so opening the screen still
    awaited a probe that begins with `ensureAiSession()`. Same root cause as the tip that did
    nothing, through a different door."""
    assert seen["screenHangs"]["tookMs"] < 5000, (
        f"the wallet screen waited {seen['screenHangs']['tookMs']}ms on a signer that never answered")
    assert seen["screenHangs"]["spinner"] is False, "it is still spinning with no answer"
    assert seen["screenHangs"]["saysSo"] is True and seen["screenHangs"]["hasRetry"] is True


def test_an_amount_that_is_too_big_names_the_limit(seen):
    """Reported as: '"more than one wallet cn spend now" wtf is this!'

    The message told somebody their number was wrong without telling them the right one, so the only
    way to find it was to guess — and the balance on screen is the TOTAL, which is not what a
    transfer can draw on while part of it is still locking. It now names the spendable amount, and
    when the rest is locking it says when that ends."""
    assert seen["overAmount"]["namesTheLimit"] is True, (
        f"the refusal does not say how much can actually be sent: {seen['overAmount']['toast']!r}")
    assert seen["overAmount"]["saysUnlock"] is True, (
        "a locked remainder is not explained, so the number looks arbitrary")


def test_the_service_fee_is_stated_before_anybody_sends(seen):
    """Reported as "when I zap, i see nothing about the fee".

    A cut a payer only discovers afterwards — by noticing the recipient got less than they chose —
    is indistinguishable from the wallet being broken, and it is the kind of surprise that makes
    people stop trusting a tipping button entirely. The percentage on its own is not enough either:
    "2%" of an amount nobody has typed yet is not information, so the sheet does the arithmetic and
    names what the recipient actually receives."""
    fee = seen["userWalletFee"]
    assert fee["saysPercent"] is True, "the sheet does not mention the fee at all"
    assert fee["restated"] is True, "the sheet never says what the recipient will receive"
    assert fee["namesTheNet"] is True, "the recipient's actual amount (0.0098) is not shown"
    assert fee["namesTheCut"] is True, "the operator's cut (0.0002) is not shown"


def test_a_node_with_no_fee_says_nothing_about_one(seen):
    """Most nodes will not charge, and a sheet that talks about a 0% fee invents a concern."""
    assert seen["userWalletNoFee"]["silent"] is True
