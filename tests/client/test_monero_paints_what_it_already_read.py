"""ALT-TAB BACK TO THE WALLET MUST NOT BLANK IT — "black screen with circle".

Reported as: "on desktop I alt-tab to monero then monero wallet black screen with circle".

`render()` opened by claiming the shared `#feed` with a spinner and then awaiting `probe()`. That
claim was itself a fix — without it, opening the wallet from Texts left "Search messages · Loading
messages…" on screen under the wallet's nav item for the whole probe — so the spinner cannot simply
be deleted. But it made re-entry worse than the problem it solved: probe() is allowed 20 seconds
(the node is allowed 8 for the RPC alone, up to 30 by configuration) and takes most of them while a
just-restarted monerod reconnects, which is exactly the state this user was in. So switching back to
a wallet this device had already read and painted showed a spinner on black, every time.

The client's own rule answers it: paint what you already hold before the first network await. The
last probe result is in `state`, so alt-tab repaints the balance that was on screen and updates it
in place when the answer lands. The spinner stays for the ONE case it is honest about — a wallet
this device has never read, where there is genuinely nothing to show.

The whole question is what is on screen DURING an await that has not returned, so these drive the
SHIPPED module under node against a fetch that is held open, and read the feed at that instant. No
static read of the file can see it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = Path(__file__).with_name("monero_paint_scenarios.mjs")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node unavailable")


@pytest.fixture(scope="module")
def seen():
    done = subprocess.run([NODE, str(SCENARIOS)], cwd=ROOT, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_wallet_it_has_already_read_comes_back_instead_of_a_spinner(seen):
    """THE BUG. Coming back to a wallet this page has read paints the balance it read, not a hole
    with a spinner in it, no matter how long the daemon takes to answer."""
    assert seen["known"]["balance"], (
        "re-entering the wallet does not repaint the balance it already holds")
    assert not seen["known"]["spinner"], (
        "alt-tab still blanks a known wallet to a spinner — this is the black screen with the "
        "circle in it")


def test_a_wallet_it_has_never_read_still_says_so_honestly(seen):
    """The early paint must not become a lie. With nothing read there is nothing to show, and a
    spinner is the correct answer — the split is on HAVING something real, never on a timeout."""
    assert seen["unread"]["spinner"], (
        "a wallet this device has never read paints no spinner, so there is nothing on screen "
        "while it is being read")


def test_a_wallet_last_seen_as_unavailable_repaints_its_own_card(seen):
    """An unavailable wallet is still an ANSWER this device has. Repainting the card keeps the
    Retry button reachable through the wait, instead of taking it away for twenty seconds."""
    assert seen["unavailable"]["card"], "the unavailable card is not repainted on re-entry"
    assert not seen["unavailable"]["spinner"]


@pytest.mark.parametrize("case", ["known", "unread", "unavailable"])
def test_the_previous_view_is_still_cleared_before_the_first_await(seen, case):
    """The regression this must not reintroduce, in every branch: `#feed` is shared, and the reason
    the spinner was there at all is that awaiting first left the LAST screen's DOM under the
    wallet's nav item. Whatever is painted, the old view is gone."""
    assert not seen[case]["previousView"], (
        f"in the {case} case the previous screen survives the first await again")


def test_what_it_paints_first_is_only_a_head_start(seen):
    """A cached paint that never updates is a stale balance presented as a current one. When the
    probe answers, the answer replaces it."""
    assert seen["refresh"]["fresh"], "the fresh balance never reached the screen"
    assert not seen["refresh"]["stale"], "the stale balance is still on screen after a new answer"
