"""GRASP-01's three NIP-11 MUSTs, verified against the SHIPPED `nip11_doc`.

    ### NIP-11
    MUST serve a NIP-11 document:
    1. MUST list each supported GRASP under `supported_grasps` in format `GRASP-XX` eg `GRASP-01`
       as a string array
    2. MUST list repository acceptance criteria under `repo_acceptance_criteria` as a human
       readable string
    3. MUST list brief summary of curation policy under `curation` if events are curated beyond
       generic SPAM prevention; otherwise `curation` MUST be ommitted
                                                        -- grasp.git 01.md, commit f35b4f9a4ed2

All three were absent from the live document, which is why an ngit v3 client could not tell we were
a GRASP service at all: `relay_information.rs` reads `supported_grasps` and nothing else.

These parse the bytes `nip11_doc` actually emits rather than reading the dict literal, because
there is one producer for both `poster.place/git` and `relay.poster.place` and the failure this
guards against is a key that is computed but never reaches the JSON.
"""
from __future__ import annotations

import json

import pytest

from app.services import git_acceptance
from app.services.nostr_relay.server import RelayServer, _supported_grasps


def doc(**cfg):
    s = object.__new__(RelayServer)
    s.cfg = {"wot_enabled": False, **cfg}
    return json.loads(s.nip11_doc("relay.example"))


# --- MUST 1: supported_grasps ------------------------------------------------------------------

def test_supported_grasps_is_always_present_as_an_array():
    """"as a string array" is part of the MUST. A client that expects the key and finds it missing
    cannot distinguish "supports none" from "is not a GRASP service"."""
    d = doc()
    assert isinstance(d["supported_grasps"], list)
    assert all(isinstance(x, str) for x in d["supported_grasps"])


def test_an_operator_claim_is_advertised_in_the_spec_format():
    assert doc(supported_grasps="GRASP-01 GRASP-08")["supported_grasps"] == ["GRASP-01", "GRASP-08"]


@pytest.mark.parametrize("raw,out", [
    ("1", ["GRASP-01"]),                    # a bare number is what an operator types
    ("grasp-8", ["GRASP-08"]),              # zero-padded per the spec's `GRASP-XX`
    ("1,8", ["GRASP-01", "GRASP-08"]),      # commas as well as spaces
    ("GRASP-01 grasp1 1", ["GRASP-01"]),    # de-duplicated
    ("", []),
    ("yes please", []),                     # a value naming no GRASP is dropped, not passed through
])
def test_the_claim_is_normalised_rather_than_echoed(raw, out):
    """A malformed entry in a capability array is worse than a missing one — a client that cannot
    parse the array may discard the whole document."""
    assert _supported_grasps(raw) == out


def test_nothing_is_claimed_by_default():
    """Claiming a GRASP claims every MUST in it, and GRASP-01 still has outstanding ones here
    (refs/nostr pushes, HEAD from the signed 30618, the recursive maintainer set). An advertised
    capability that is not there costs a client a failed operation and a wrong diagnosis."""
    assert doc()["supported_grasps"] == []


# --- MUST 2: repo_acceptance_criteria ----------------------------------------------------------

def test_the_acceptance_criteria_is_a_human_readable_string():
    d = doc()
    assert isinstance(d["repo_acceptance_criteria"], str)
    assert len(d["repo_acceptance_criteria"]) > 20


def test_the_criteria_is_rendered_from_the_policy_the_gate_enforces():
    """The whole point of git_acceptance.SETTING: two copies of one rule go stale silently, leaving
    the document promising a policy nobody implements while a client is refused for a reason it
    says does not apply. There must be nowhere to type a criteria string."""
    for policy, text in git_acceptance.POLICIES.items():
        assert doc(repo_acceptance=policy)["repo_acceptance_criteria"] == text


def test_the_default_policy_is_the_agreed_one():
    assert git_acceptance.DEFAULT == "account_or_wot"
    d = doc()["repo_acceptance_criteria"]
    assert "account on this node" in d and "web of trust" in d


def test_an_unknown_stored_policy_falls_back_to_the_default_not_to_open():
    """A typo in a settings box must never widen who may allocate disk here."""
    assert git_acceptance.normalize("opne") == git_acceptance.DEFAULT
    assert doc(repo_acceptance="opne")["repo_acceptance_criteria"] == \
        git_acceptance.POLICIES[git_acceptance.DEFAULT]


@pytest.mark.parametrize("policy,has_account,in_wot,expect", [
    ("account_or_wot", True,  False, True),
    ("account_or_wot", False, True,  True),
    ("account_or_wot", False, False, False),
    ("wot",            True,  False, False),
    ("wot",            False, True,  True),
    ("account",        True,  False, True),
    ("account",        False, True,  False),
    ("allowlist",      True,  True,  False),
    ("open",           False, False, True),
    ("closed",         True,  True,  False),
])
def test_the_gate_matches_the_sentence_it_advertises(policy, has_account, in_wot, expect):
    assert git_acceptance.accepts(policy, has_account=has_account, in_wot=in_wot,
                                  allowlisted=False) is expect


def test_the_operator_allowlist_is_additive_under_every_policy_but_closed():
    """It exists to name an operator key that is in no social graph — which is exactly the key a
    node needs to provision its own repositories."""
    for p in git_acceptance.POLICIES:
        got = git_acceptance.accepts(p, has_account=False, in_wot=False, allowlisted=True)
        assert got is (p != "closed"), p


# --- MUST 3: curation --------------------------------------------------------------------------

def test_curation_is_omitted_when_nothing_is_curated():
    """"otherwise `curation` MUST be ommitted" is normative too, so this key cannot be a constant."""
    assert "curation" not in doc(wot_enabled=False)


def test_the_web_of_trust_gate_is_declared_as_curation():
    """It is not generic spam prevention: it refuses a stranger's post on the strength of who
    follows them. Silence about it is what makes the refusal unexplainable from outside."""
    c = doc(wot_enabled=True)["curation"]
    assert "web of trust" in c
    # ...and it must not mislead a git client, whose events are exempt from that gate.
    assert "git repository announcements" in c


@pytest.mark.parametrize("key,needle", [
    ("blocked_words", "words"),
    ("blocked_langs", "languages"),
    ("block_bridged", "bridged"),
    ("blocked_relays", "bridge relays"),
])
def test_each_configured_filter_is_summarised(key, needle):
    """Built from the filters actually configured, because the sentence is what a person reads
    before deciding whether their events survive here, and every one is runtime-toggleable."""
    assert needle in doc(**{key: ["x"]})["curation"]
    assert needle not in (doc().get("curation") or "")


def test_the_rest_of_the_document_is_unchanged():
    """The three additions must not disturb what clients already read."""
    d = doc()
    assert d["name"] == "PosterChanAI Relay"
    assert 1 in d["supported_nips"] and 42 in d["supported_nips"]
    assert d["limitation"]["max_subscriptions"] == 20
