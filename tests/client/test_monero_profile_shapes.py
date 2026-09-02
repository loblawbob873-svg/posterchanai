"""EVERY SHAPE A MONERO ADDRESS ARRIVES IN, AND THE SCREENS THAT HAVE TO SHOW IT.

Reported against a real account: "he added a payment target for xmr but no way to zap him, is there
a new standard we are missing?"

There is no standard — that is the whole problem. Lightning has `lud16`/`lud06` in NIP-01 metadata;
Monero has nothing, so every client that added it picked its own key. Measured on the reported
profile, which carries the address THREE ways at once:

    monero_address           = 85t69QD9…
    xmr                      = 85t69QD9…
    cryptocurrency_addresses = { monero: "85t69QD9…" }

and the address validates on the client AND server-side on mainnet. Nothing was missing from the
reading; the failure was that the PROFILE HEADER is rendered once from whatever the store held at
paint time, and the late-profile patch refreshed avatar, banner, name, about and music but never the
tip row — so on a cold or cache-first open the buttons could not appear for the rest of the visit.
That is `_tipMarks`' bug on a second screen.

This file therefore pins BOTH halves: the reading stays liberal (backwards compatibility with every
client that has shipped one of these keys), and the two surfaces that offer the tip gain it when the
profile arrives late.

Note the address is a SUBADDRESS (starts with 8, 95 chars). Standard addresses start with 4,
integrated ones are 106 — all three must pass, and a stagenet-shaped one must not on a mainnet node.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")

#: The reported account's real address, base58, 95 chars, subaddress form.
REAL = "85t69QD9cxz19xb8pRnw2gHcurQPkLxfKTUgyAS58QMmYFRQ4va1zML2SwvuL8UTyv5LCee3UxvZo2THxxSC7f5u1gPqfyf"
STD = "4" + "A" * 94
INTEGRATED = "4" + "B" * 105


def _fn(name: str) -> str:
    """Lift a function out of the shipped app.js by brace matching."""
    start = APP.index(f"  function {name}(")
    depth, i = 0, APP.index("{", start)
    for j in range(i, len(APP)):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:j + 1]
    raise AssertionError(f"could not lift {name}")


def xmr_of(profile: dict) -> str:
    """Run the SHIPPED xmrOf against a profile object."""
    program = (
        "const _XMR_RX=" + re.search(r"const _XMR_RX=(/[^;]+/);", APP).group(1) + ";\n"
        + _fn("isXmrAddr") + "\n" + _fn("xmrOf") + "\n"
        + f"process.stdout.write(String(xmrOf({json.dumps(profile)})));")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-600:]
    return done.stdout.strip()


# ── the reading, i.e. backwards compatibility ────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["monero_address", "xmr", "monero", "xmr_address"])
def test_every_flat_key_that_has_shipped_is_read(key):
    assert xmr_of({key: REAL}) == REAL


def test_the_garnet_address_map_is_read():
    """Garnet (the Amethyst fork that added Monero tipping) publishes a coin→address MAP. Stock
    Amethyst has no Monero support at all, so for those users this is the only key there is."""
    assert xmr_of({"cryptocurrency_addresses": {"monero": REAL}}) == REAL


@pytest.mark.parametrize("k", ["monero", "xmr", "XMR", "Monero"])
def test_the_map_is_read_case_insensitively_enough(k):
    assert xmr_of({"cryptocurrency_addresses": {k: REAL}}) == REAL


def test_the_reported_profile_exactly_as_it_is_published():
    """All three shapes at once, which is what the reported account actually publishes. It must
    resolve, and it must resolve to the same address whichever key wins."""
    assert xmr_of({
        "name": "Turiz", "lud16": "turiz@walletofsatoshi.com",
        "monero_address": REAL, "xmr": REAL,
        "cryptocurrency_addresses": {"monero": REAL},
        "bch": "q" + "a" * 41,
    }) == REAL


def test_an_unknown_key_still_works_because_the_value_is_recognisable():
    """The last-resort sweep: a client nobody here has heard of, using a key nobody here has heard
    of, is still tippable because an XMR address is recognisable on sight."""
    assert xmr_of({"my_weird_monero_field": REAL}) == REAL


def test_an_address_written_in_the_bio_is_found():
    assert xmr_of({"about": f"tips very welcome: {REAL} thanks!"}) == REAL


@pytest.mark.parametrize("addr", [STD, INTEGRATED, REAL])
def test_standard_subaddress_and_integrated_forms_all_pass(addr):
    """4… standard (95), 8… subaddress (95), 4… integrated (106). Refusing any of them refuses
    somebody's real address."""
    assert xmr_of({"monero_address": addr}) == addr


@pytest.mark.parametrize("junk", ["", "not an address", "4", "8" * 40, None, 12345])
def test_rubbish_is_not_mistaken_for_an_address(junk):
    assert xmr_of({"monero_address": junk}) == ""


def test_a_stagenet_address_is_not_offered_as_mainnet():
    """Stagenet addresses start with 5 or 7. Reading one as tippable sends real money nowhere."""
    assert xmr_of({"monero_address": "5" + "A" * 94}) == ""


def test_an_empty_profile_yields_nothing():
    assert xmr_of({}) == "" and xmr_of({"name": "someone"}) == ""


# ── the screens ──────────────────────────────────────────────────────────────────────────────────

def test_the_profile_header_patch_adds_the_tip_controls():
    """THE ACTUAL BUG. The header renders once from whatever the store held; the patch that runs
    when the kind-0 lands has to add the tip row too, or a cold-opened profile can never offer it."""
    # THE CALL, inside _patchProfileHeader — not merely the string anywhere in app.js, which also
    # matches the function's own DEFINITION. The first version of this assertion did exactly that
    # and passed with the call deleted; the mutation check is what exposed it.
    header = _fn("_patchProfileHeader")
    assert "_patchProfileTips(" in header, (
        "the late-profile patch does not refresh the tip controls — a profile opened before its "
        "kind-0 arrives will never show a Monero button")
    body = _fn("_patchProfileTips")
    for control in ("prof-xmr", "xmrtip-prof", "prof-bch", "prof-ln"):
        assert control in body, f"{control} is not restored when the profile arrives late"


def test_a_late_added_tip_button_is_bound():
    """A button that appears and does nothing is worse than one that never appears."""
    body = _fn("_patchProfileTips")
    assert "doXmrTip(null, pk)" in body
    assert "doBchTip(pk)" in body and "doZap(null, pk)" in body


def test_the_patch_cannot_take_the_header_down():
    body = _fn("_patchProfileTips")
    assert "catch(_)" in body


def test_the_feed_card_keeps_its_own_late_patch():
    """The same defect on the timeline, already fixed — kept honest here so the pair stay together."""
    assert "_tipMarks(n, p)" in APP
