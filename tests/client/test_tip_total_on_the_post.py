"""THE TIP TOTAL A POST SHOWS — Lightning and Monero side by side.

Asked as "can monero zaps show the total on the post like lightning does?". It already did: address
tips are tallied per unit in `buildCounts` and `tipCountLabel` renders them beside the sats. Driving
the shipped functions:

    xmr only      "ɱ0.0002"
    xmr + sats    "21k ɱ0.01"
    bch too       "ɱ0.01 🟢0.5"

They are never summed — a sats figure and an XMR figure added together is a number that means
nothing.

One case genuinely did show nothing: a tip note published with NO `amount_xmr` tag. `tipN` counted
it and the button lit up, so the post read as tipped for a blank amount. The mark is shown without a
number now; inventing an amount nobody told us is the only worse option.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _lift(name: str) -> str:
    i = APP.index(f"  function {name}(")
    depth, start = 0, APP.index("{", i)
    for j in range(start, len(APP)):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                return APP[i:j + 1]
    raise AssertionError(name)


def label(counts: dict) -> str:
    src = "\n".join(_lift(n) for n in ("fmtTipAmt", "fmtSats", "tipCountLabel"))
    program = src + f"\nprocess.stdout.write(String(tipCountLabel({json.dumps(counts)})));"
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-600:]
    return done.stdout


def test_a_monero_total_is_shown_on_the_post():
    assert label({"zaps": 0, "tips": {"XMR": 0.0002}}) == "ɱ0.0002"


def test_lightning_and_monero_appear_together():
    got = label({"zaps": 21000, "tips": {"XMR": 0.01}})
    assert "ɱ0.01" in got and "21k" in got


def test_they_are_never_summed():
    """Sats and XMR are different currencies; one number for both is a lie."""
    got = label({"zaps": 21000, "tips": {"XMR": 0.01}})
    assert got.count("ɱ") == 1 and "21.01" not in got


def test_bitcoin_cash_sits_beside_them():
    assert label({"zaps": 0, "tips": {"XMR": 0.01, "BCH": 0.5}}) == "ɱ0.01 🟢0.5"


def test_a_tip_with_no_amount_still_marks_the_post():
    """THE GAP. `amount_xmr` is optional on the note, and keying the label on a truthy amount made
    those tips invisible while the button still lit up."""
    assert label({"zaps": 0, "tips": {"XMR": 0}, "tipN": 1}) == "ɱ"
    assert label({"zaps": 0, "tips": {"BCH": 0}, "tipN": 1}) == "🟢"


def test_it_does_not_invent_an_amount():
    assert "0" not in label({"zaps": 0, "tips": {"XMR": 0}, "tipN": 1})


def test_an_untipped_post_says_nothing():
    assert label({"zaps": 0, "tips": None}) == ""
    assert label({"zaps": 0, "tips": {"XMR": 0}}) == ""      # no tips recorded at all


def test_small_amounts_are_not_rounded_away():
    """A 0.0002 XMR tip must not render as 0."""
    assert label({"zaps": 0, "tips": {"XMR": 0.00000001}}) not in ("ɱ0", "")


def test_the_counter_still_separates_tips_from_replies():
    """The tally this label reads: an address tip is a kind 1, and counting it as a reply is how it
    was invisible in the first place."""
    assert "c.tipN[id]=(c.tipN[id]||0)+1;" in APP
    assert "else c.replies[id]=(c.replies[id]||0)+1;" in APP
