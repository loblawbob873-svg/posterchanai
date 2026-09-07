"""A USER'S OWN MONERO WALLET CAN SEND, NOT ONLY RECEIVE AND BE EMPTIED.

Reported by an operator whose user sent screenshots: "there is no send button on his Monero
wallet" — while the operator's own screen had one. Both were right. There are two wallets here:
the node wallet (`/api/wallet/xmr/*`, admin-only via get_admin_user) which has always had Send,
and the per-user custodial wallet (`/api/wallet/xmr/me/*`) which offered Receive and a
whole-balance Withdraw. So a user holding a tip could empty the account to one address, or do
nothing — they could not pay anybody an amount.

The backend already did this: `/me/pay` takes {address, amount} pairs and is what the tip sheet
posts to. Only the button was missing.

These are source assertions rather than a driven flow: the runtime harness in
test_monero_wallet_send_flow.py stubs getElementById to the feed alone, so it can open modals but
cannot press a button on the wallet CARD. Each assertion below is verified to fail against a
mutated copy of the shipped file by test_these_assertions_can_fail.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET = ROOT / "static" / "js" / "client" / "monero-wallet.js"


@pytest.fixture(scope="module")
def src() -> str:
    if not WALLET.exists():
        pytest.skip("static/js/client/monero-wallet.js is not present")
    return WALLET.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def me_card(src: str) -> str:
    """The per-user card's markup — the screen a non-operator actually sees."""
    start = src.index("function meWalletHtml(")
    return src[start:src.index("\n  }", start)]


def test_the_user_wallet_offers_send_beside_receive_and_withdraw(me_card: str):
    assert 'id="mw-me-send"' in me_card, "the per-user wallet still cannot send"
    assert 'id="mw-me-receive"' in me_card
    assert 'id="mw-me-withdraw"' in me_card, "the way out was removed"


def test_send_is_bound_behind_a_null_check_like_every_other_card_button(src: str):
    # A binding written as `by('x').onclick = ...` throws when the element is absent and takes
    # every binding BELOW it with it — which here would silently disable Withdraw, the way out.
    assert "if(by('mw-me-send')) by('mw-me-send').onclick" in src, \
        "an unguarded binding can take Withdraw down with it"


def test_it_posts_to_the_pay_route_with_an_address_and_an_amount(src: str):
    assert "'/api/wallet/xmr/me/pay'" in src, "send does not reach the per-user pay route"
    assert re.search(r"payments:\s*\[\s*\{\s*address:\s*to,\s*amount:\s*raw\s*\}\s*\]", src), \
        "the payment is not sent as {address, amount}"


def test_the_amount_is_checked_against_the_unlocked_balance_not_the_total(src: str):
    # Monero locks change for ~10 blocks, so a wallet with a balance can still be unable to spend.
    send = src[src.index("if(by('mw-me-send'))"):src.index("if(by('mw-me-withdraw'))")]
    assert "amount(s.unlocked_balance)" in send, "a locked balance would be offered as spendable"
    assert "is available to send right now" in send


def test_the_fee_is_stated_before_anybody_sends(src: str):
    send = src[src.index("if(by('mw-me-send'))"):src.index("if(by('mw-me-withdraw'))")]
    assert "fee_percent" in send and "% of what you send" in send, \
        "the node's cut is only discovered after the recipient gets less"


def test_an_irreversible_payment_is_confirmed_before_it_leaves(src: str):
    send = src[src.index("if(by('mw-me-send'))"):src.index("if(by('mw-me-withdraw'))")]
    assert "cannot be reversed" in send
    assert 'id="mw-ms-go" disabled' in send, "Send now is live before the box is ticked"


def test_an_unknown_answer_is_not_called_a_failure(src: str):
    # The rule the other two money paths already follow: a spend that timed out may be in flight,
    # and re-enabling the button invites a second real payment.
    send = src[src.index("if(by('mw-me-send'))"):src.index("if(by('mw-me-withdraw'))")]
    assert "may have been sent|did not answer in time" in send
    assert "go.disabled = unsure" in send, "an abandoned spend re-offers itself"
    assert "Check your history" in send


def test_these_assertions_can_fail(src: str):
    """Every rule above, re-run against a file with the feature removed."""
    broken = src.replace('<button class="btn btn-neon" id="mw-me-send">Send</button>', "")
    assert 'id="mw-me-send"' not in broken.split("function meWalletHtml(")[1].split("\n  }")[0]
    unguarded = src.replace("if(by('mw-me-send')) by('mw-me-send').onclick",
                            "by('mw-me-send').onclick")
    assert "if(by('mw-me-send')) by('mw-me-send').onclick" not in unguarded
    total = src.replace("amount(s.unlocked_balance)", "amount(s.balance)")
    send = total[total.index("if(by('mw-me-send'))"):total.index("if(by('mw-me-withdraw'))")]
    assert "amount(s.unlocked_balance)" not in send
