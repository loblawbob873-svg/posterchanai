"""A fediverse person's wallet address reaches their puppet profile, so they can be tipped here.

Reported: "On a few momostr profiles I can see XMR and ETH addresses on Primal but they don't show up
on PosterChan." This node blocks the momostr bridge, and does not need it — it is an ActivityPub
server and federates with those people directly. But the puppet profile it built for them carried the
BIO only. Fediverse people put a wallet address in a profile FIELD (a Mastodon `fields` entry, an
ActivityPub `attachment` PropertyValue), and momostr shows it because it flattens the fields into the
bio. Two places dropped it here, and both are tested:

  * `convert.account_from_actor` never copied `attachment` out of the actor document at all;
  * `fedi_bridge_identity` built the kind-0 from `note` alone.

Fediverse people mostly have no Lightning address, so for them an address in a field is the only way
anybody here can pay them. The real case is the actor this was reported on (clew.lol/users/Tony), in
the shape that server actually serves.

MEASURED, 2026-10-06, over the 329 fediverse actors this node federates with (17 unreachable): 166
have profile fields; 2 carry a Lightning address under a ⚡ label (`⚡️ => c00p@fountain.fm`,
`⚡️ => strike@feld.me`), 3 carry on-chain addresses (Monero ×2, ETH, BTC). Rare, real, and every one
of them was untippable here. The Lightning cases are the reason `_LN_LABEL` exists: a user@domain
value is a payment target ONLY under a Lightning label, because the same shape in a "contact" field is
an e-mail address and would send a zap to whatever that domain's LNURL endpoint answers.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from app.services.activitypub import convert
from app.services import fedi_bridge_identity as fbi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XMR = "4Avre3zku9fLTQoaZThJaZQhsvt9cvBPR4xCVnufGqsTZCcQ4bAj7Qx5up7oeQwMjwaFUD2BxRmkSdVMYh4MkT77DqfsVUY"
ETH = "0x52908400098527886E0F7030069857D2E4169EE7"


def _actor(attachment, summary="<p>hello</p>"):
    return {"id": "https://clew.lol/users/Tony", "type": "Person", "preferredUsername": "Tony",
            "name": "Tony", "summary": summary, "attachment": attachment}


def _kind0(actor):
    acc = convert.account_from_actor(actor)
    p = {"display_name": acc["display_name"], "nip05_name": "tony", "acct": acc["acct"],
         "about": fbi._strip_html(acc["note"]), "fields": fbi.profile_fields(acc), "avatar_url": ""}
    return fbi._profile_content(p), acc


def test_the_reported_actor_becomes_tippable_in_monero():
    k0, _ = _kind0(_actor([{"type": "PropertyValue", "name": "Monero Wallet: ", "value": XMR,
                            "verified_at": None}]))
    assert k0["monero_address"] == XMR and k0["xmr"] == XMR
    assert f"Monero Wallet: {XMR}" in k0["about"], "the field is not visible on the profile either"


def test_an_ethereum_field_becomes_the_ethereum_key():
    k0, _ = _kind0(_actor([{"type": "PropertyValue", "name": "ETH",
                            "value": f'<a href="https://etherscan.io/address/{ETH}">{ETH}</a>'}]))
    assert k0["ethereum"] == ETH, "an HTML-wrapped value (the usual Mastodon shape) was not flattened"


def test_a_label_is_never_an_address():
    """A field CALLED "XMR" whose value is not an address must produce nothing — only a token that is
    itself an address counts, so a mislabelled field cannot route money anywhere."""
    k0, _ = _kind0(_actor([{"type": "PropertyValue", "name": "XMR", "value": "ask me in DMs"},
                           {"type": "PropertyValue", "name": "ETH", "value": "0x1234"}]))
    assert "monero_address" not in k0 and "ethereum" not in k0


def test_the_mastodon_api_shape_is_read_too():
    acc = {"display_name": "x", "acct": "x@y.z", "note": "", "fields": [{"name": "XMR", "value": XMR}]}
    assert fbi.profile_fields(acc) == [("XMR", XMR)]


def test_fields_are_bounded():
    many = [{"type": "PropertyValue", "name": f"n{i}", "value": "v" * 5000} for i in range(50)]
    fields = fbi.profile_fields(convert.account_from_actor(_actor(many)))
    assert len(fields) == fbi._MAX_FIELDS and all(len(v) <= 1000 for _, v in fields)


def test_a_new_address_republishes_and_nothing_else_does():
    """The publish is gated on a signature, so the fields have to be in it — the bio part is cut to 200
    characters and the fields are appended at the END, so without this a puppet seen before the deploy
    would never gain its address. And a puppet with NO fields must keep its old signature exactly, or
    the deploy republishes every fediverse profile this node has ever mirrored."""
    with_xmr = convert.account_from_actor(_actor([{"type": "PropertyValue", "name": "XMR", "value": XMR}]))
    without = convert.account_from_actor(_actor([]))
    assert fbi._account_profile_sig(with_xmr) != fbi._account_profile_sig(without)
    legacy = fbi._profile_sig_from("Tony", "", "hello", [])
    assert fbi._account_profile_sig(without) == legacy


def _eth_of():
    """The SHIPPED ethOf, lifted out of app.js and run under node."""
    if shutil.which("node") is None:
        pytest.skip("no node")
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    rx = re.search(r"const _ETH_ADDR=.*?\n  function ethOf\(p\)\{.*?\n  \}\n", src, re.S)
    assert rx, "ethOf is gone from app.js"
    return rx.group(0)


def _run_eth(profile):
    code = _eth_of() + "\nprocess.stdout.write(JSON.stringify(ethOf(%s)));" % json.dumps(profile)
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_client_reads_the_key_the_bridge_writes():
    assert _run_eth({"ethereum": ETH}) == ETH
    assert _run_eth({"cryptocurrency_addresses": {"ethereum": ETH}}) == ETH


def test_the_client_never_scrapes_eth_out_of_a_bio():
    """`0x` + 40 hex is also a contract, a token, any account somebody MENTIONED. A bio is not a
    payment instruction for this shape — unlike a Monero address, which is self-describing."""
    assert _run_eth({"about": f"my favourite contract is {ETH}"}) == ""
    assert _run_eth({"ethereum": "0x1234"}) == "" and _run_eth({"ethereum": 42}) == ""


def test_the_pay_sheet_offers_it():
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    body = src[src.index("async function _paymentChoices(pk)"):]
    body = body[:body.index("\n  }\n")]
    assert "['ethereum',ethOf(p)]" in body


# ---- Lightning: the zap path ----------------------------------------------------------------------

def _pv(name, value):
    return {"type": "PropertyValue", "name": name, "value": value}


def test_a_lightning_field_becomes_a_zappable_lud16():
    """The real shape from c00p@noauthority.social — the emoji label with its variation selector."""
    k0, _ = _kind0(_actor([_pv("⚡️", "c00p@fountain.fm")]))
    assert k0["lud16"] == "c00p@fountain.fm"


def test_the_label_can_be_a_word():
    for label in ("Lightning", "LN", "zaps", "Tips", "lud16", "⚡ sats"):
        k0, _ = _kind0(_actor([_pv(label, "<p>me@getalby.com</p>")]))
        assert k0.get("lud16") == "me@getalby.com", label


def test_a_contact_address_is_never_a_zap_target():
    """THE ONE THAT WOULD SEND MONEY TO THE WRONG PERSON. A mail address in an ordinary field is the
    same shape as a Lightning address; zapping it pays whoever `example.com/.well-known/lnurlp/me`
    belongs to."""
    for label in ("Contact", "Email", "Matrix", "XMPP", "", "Website"):
        k0, _ = _kind0(_actor([_pv(label, "me@example.com")]))
        assert "lud16" not in k0, label


def test_prose_under_a_lightning_label_is_not_an_address():
    k0, _ = _kind0(_actor([_pv("⚡", "ask me for an invoice"), _pv("Lightning", "me@getalby.com and more")]))
    assert "lud16" not in k0


def test_an_lnurl_counts_wherever_it_is():
    lnurl = "LNURL1DP68GURN8GHJ7MRWW4EXCTNXD9SHG6NPVCHXXMMD9AKXUATJDSKHQCTE8AEK2UMND9HKU0TZV4JNJV34"
    k0, _ = _kind0(_actor([_pv("pay me", lnurl)]))
    assert k0["lud06"] == lnurl.lower()


def test_a_lightning_address_in_the_bio_alone_is_not_read():
    """Bios are prose — "say hi at me@example.com" is the common case there — so only a labelled field
    counts for Lightning."""
    k0, _ = _kind0(_actor([], summary="<p>⚡ zap me: me@getalby.com</p>"))
    assert "lud16" not in k0


def test_every_rail_from_one_profile():
    """luke@social.lukepreston.xyz carries Monero and BTC side by side; nothing may be lost or swapped
    when several addresses share a profile."""
    k0, _ = _kind0(_actor([_pv("XMR", XMR), _pv("ETH", ETH), _pv("⚡", "me@getalby.com")]))
    assert (k0["monero_address"], k0["ethereum"], k0["lud16"]) == (XMR, ETH, "me@getalby.com")
