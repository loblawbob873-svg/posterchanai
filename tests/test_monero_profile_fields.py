"""Where a Monero address lives in a kind-0, and who reads which key.

Reported by a user: "I added a Monero address to my profile with poster.place and it doesn't show up
in Amethyst, so I also pasted it in my bio." Their profile was written CORRECTLY — checked against
their live kind-0, which carried both `xmr` and `monero_address` with the same address. The address
was fine; the READER was looking somewhere else.

Stock Amethyst has no Monero support at all. The clients that do are forks — Garnet is the Amethyst
one — and they use a THIRD shape: `cryptocurrency_addresses`, a map of coin name to address. So this
client now writes that alongside the two flat keys, and reads it, because a user whose address only
appears in the map is untippable here for the mirror-image reason.

The rules these tests hold to are the ones the surrounding code already had for the flat aliases,
and each has cost somebody money or privacy somewhere:

  * READ liberally — an address under any known key is still that person's address.
  * WRITE every alias, so no client is left out.
  * CLEARING must reach every alias, or a stale address stays live under a key we no longer write
    and the money goes to the wrong place.
  * Never rewrite the parts of a profile we do not manage: other coins in that map belong to
    whatever client put them there.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ADDR = ("47ik6ZUx9MkfTyt9sZRbJk8SJAXCcj44t2vhenUJSAPiB1SJxZSRysvRbMQLVR26"
        "yHcF6UHcgFUTsigJ4DHDMyGaAkgEuzi")


def _lift(name):
    """The real function out of app.js — never a restatement of it here."""
    src = open(APP, encoding="utf-8").read()
    m = re.search(r"\n  function " + re.escape(name) + r"\(.*?\n  \}", src, re.S)
    assert m, f"{name} is gone from app.js"
    return m.group(0)


def xmr_of(profile):
    body = _lift("xmrOf") + _lift("isXmrAddr")
    # The regex constant only if the lifted text did not already bring it — isXmrAddr sits beside it,
    # so a naive prepend redeclares a const and node refuses the whole file.
    pre = ""
    if "_XMR_RX" not in body.split("function isXmrAddr")[0] or "const _XMR_RX" not in body:
        m = re.search(r"const _XMR_RX\s*=\s*[^;]+;", open(APP, encoding="utf-8").read())
        assert m, "_XMR_RX is gone"
        if "const _XMR_RX" not in body:
            pre = m.group(0)
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "x.js")
        open(f, "w", encoding="utf-8").write(
            pre + body
            + "\nprocess.stdout.write(JSON.stringify(xmrOf(%s)));" % json.dumps(profile))
        r = subprocess.run(["node", f], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


def test_the_flat_keys_still_win():
    for key in ("monero_address", "xmr", "monero", "xmr_address"):
        assert xmr_of({key: ADDR}) == ADDR, key


def test_the_map_form_is_read():
    """Garnet's shape. Without this, a user who set their address in that client is untippable
    here — the mirror image of the report that started this."""
    assert xmr_of({"cryptocurrency_addresses": {"monero": ADDR}}) == ADDR
    assert xmr_of({"cryptocurrency_addresses": {"xmr": ADDR}}) == ADDR


def test_a_map_of_other_coins_is_not_mistaken_for_one():
    assert xmr_of({"cryptocurrency_addresses": {"bitcoin": "bc1qexamplenotmonero"}}) == ""
    assert xmr_of({"cryptocurrency_addresses": {}}) == ""
    assert xmr_of({}) == ""


def test_a_malformed_map_cannot_throw():
    """A profile is other people's JSON. One bad field must not take the tip button — or the whole
    profile render — down with it."""
    for bad in (None, "not a map", 42, []):
        assert xmr_of({"cryptocurrency_addresses": bad}) == ""


# ---- the write side ----------------------------------------------------------------------------

def test_setting_an_address_writes_every_alias_including_the_map():
    src = open(APP, encoding="utf-8").read()
    assert "meta.xmr=_xmr; meta.monero_address=_xmr;" in src, "the flat aliases must still be written"
    assert "_m.monero = _xmr;" in src and "meta.cryptocurrency_addresses = _m;" in src, \
        "the map form is what Garnet/Amethyst-fork readers look at"
    # …merged, not replaced: the other coins in there are not ours to manage.
    assert "Object.assign({}, p.cryptocurrency_addresses)" in src, \
        "the map must be merged — replacing it deletes coins another client wrote"


def test_clearing_reaches_the_map_too():
    """A stale address left under a key we no longer write is money going to the wrong place."""
    src = open(APP, encoding="utf-8").read()
    i = src.index("} else if(_xmrWas){")
    block = src[i:i + 900]
    assert "delete meta.xmr" in block and "delete meta.monero_address" in block
    assert "cryptocurrency_addresses" in block, "clearing must reach the map form as well"
    # …and only drop the map when Monero was the last thing in it.
    assert "if(Object.keys(_m).length) meta.cryptocurrency_addresses = _m;" in block, \
        "clearing Monero must not delete another client's coins"
