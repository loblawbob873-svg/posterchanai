"""THE DESKTOP WIDGET MUST NOT PROMISE MONEY THE TIP SHEET WILL REFUSE.

Photographed on the live desktop, in one widget, two lines apart:

    0.04928414   XMR available
    0 unlocked

Both numbers were correct and the word between them was not. The large figure is the wallet's
BALANCE; what can be spent is the unlocked part, and Monero locks a received output — and every
transaction's change — for 10 blocks. So the desktop announced that there was money, every attempt
to send it was refused, and the only available reading was that the wallet is broken. It is the same
contradiction behind "LOCAL WALLET UNLOCKS IN 14 MIN! WTF": the balance was real, visible, and
unspendable, and nothing on screen said which.

These run the SHIPPED widget code, because the bug is what the element says after a render.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
WALLET_JS = (ROOT / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")


def _fill_branch() -> str:
    """The lines that write the widget's two balance rows."""
    start = OS_JS.index("$('.wgt-xmr-bal strong'")
    return OS_JS[start:OS_JS.index("$('[data-rpc]'", start)]


def test_the_big_number_is_not_labelled_available():
    """THE BUG. It is the balance; calling it 'available' is a claim about spendability that the
    number beside it contradicts."""
    assert '<span>XMR balance</span>' in OS_JS, "the widget still labels the balance 'available'"
    assert '<span>XMR available</span>' not in OS_JS


def test_every_balance_labelled_available_actually_shows_the_spendable_number():
    """THE RULE, rather than a banned word. `monero-wallet.js` renders two balance sections and only
    ONE of them was wrong — the user's own wallet shows `unlocked_balance` under "Available balance",
    which is exactly right, while the node wallet showed the full `balance` under the same label.
    A blanket ban on the word failed the correct one, so the property is what gets asserted: a
    section may call itself available only if the figure beneath it is the unlocked figure."""
    sections = re.findall(r'<section class="mw-balance"><span>([^<]*)</span><strong>\'\s*\n?\s*\+([^\n]*)',
                          WALLET_JS)
    assert sections, "could not find the balance sections — re-read this test"
    for label, expr in sections:
        if "available" in label.lower():
            assert "unlocked_balance" in expr, (
                f"a section labelled {label!r} renders {expr.strip()[:60]!r}, which is not the "
                f"spendable amount — that is the widget's bug on another screen")
        else:
            assert "unlocked_balance" not in expr or "Balance" in label, label


def _run(node_expr: str) -> str:
    done = subprocess.run(["node", "-e", node_expr], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout.strip()


#: The widget's own formatter, lifted so the branch can run without the rest of the client.
def _branch_program(balance, unlocked) -> str:
    branch = _fill_branch()
    # Keep only the second row's logic; the first row is a plain assignment.
    branch = branch[branch.index("const _unl"):]
    branch = branch.replace("$('.wgt-xmr-bal small',el).textContent", "let out")
    return f"""
      const _xmrDisplay = v => String(v);
      const n = {{ balance: {balance!r}, unlocked_balance: {unlocked!r} }};
      {branch}
      console.log(out);
    """


@pytest.mark.parametrize("balance,unlocked,expect", [
    # The photographed state: real balance, nothing spendable.
    ("0.04928414", "0", "none of it can be sent yet"),
    # Funds usable — say how much, because that is the number the tip sheet will enforce.
    ("0.04928414", "0.0122", "can be sent now"),
    # An empty wallet must not claim funds are merely "locked" — there are none.
    ("0", "0", "0 unlocked"),
])
def test_the_second_line_says_whether_anything_can_be_sent(balance, unlocked, expect):
    assert expect in _run(_branch_program(balance, unlocked))


def test_a_locked_wallet_does_not_report_a_spendable_amount():
    """The precise failure: with nothing unlocked the widget must not print a number that reads as
    spendable money."""
    out = _run(_branch_program("0.04928414", "0"))
    assert "can be sent now" not in out
    assert "0.04928414" not in out, "the locked balance is being repeated as if it were spendable"


def test_a_comma_grouped_balance_is_still_understood():
    """`toLocaleString` output reaches this code, so '1,234.5' must not parse as 1."""
    assert "can be sent now" in _run(_branch_program("2,000", "1,234.5"))
