"""TWO FEDIVERSE ACCOUNTS MUST NEVER CLAIM ONE NIP-05 NAME — and nothing was checking.

`fedi_bridge_identity.py` had ZERO test references. That matters more here than the line count
suggests, because three of its functions decide *who a puppet is*, and every one of them fails
silently when it goes wrong: the bridge keeps running, posts keep appearing, and the only symptom is
that somebody else's verified name — or somebody else's KEY — is now serving your posts.

The hijack this pins is not hypothetical. From the docstring of `nip05_name_for`:

    _sanitize drops disallowed characters AND strips leading/trailing "._-", so `alice`, `_alice_`
    and `_alice` all collapse to `alice`; the [:64] truncation collides long handles too. Live data
    had three distinct accounts sharing one name. Since the relay's NIP-05 map is last-write-wins,
    that let anyone who could register `_victim_` on the same instance take over the victim's
    verified name.

Fifty-four rows were repaired. The fix — append a digest of the FULL handle whenever sanitising is
lossy — shipped with no test, so the next person to "simplify" that function reintroduces a name
takeover and every test in this repo still passes.

`test_the_pre_fix_rule_still_collides` re-runs the OLD implementation over the SAME corpus and
asserts it DOES collide. Without it this file would be a set of assertions nobody has ever seen
fail, which is the failure mode this repo keeps rediscovering.
"""
import re

import pytest

from app.services import fedi_bridge_identity as ident


# Handles that all collapse to the same sanitized base. This is the shape that was live: an attacker
# registers a decorated variant of a victim's handle on the same instance.
HIJACK_CORPUS = [
    "alice@mastodon.social",
    "_alice@mastodon.social",
    "alice_@mastodon.social",
    "_alice_@mastodon.social",
    ".alice.@mastodon.social",
    "-alice-@mastodon.social",
    "__alice__@mastodon.social",
]

# Lossy for a different reason: characters outside the NIP-05 local-part charset.
CHARSET_CORPUS = [
    "al!ce@mastodon.social",
    "al ce@mastodon.social",
    "al+ce@mastodon.social",
    "alçe@mastodon.social",
]

# Lossy for a third reason: the [:64] truncation. Two long handles sharing a prefix.
TRUNCATION_CORPUS = [
    "a" * 60 + "one@mastodon.social",
    "a" * 60 + "two@mastodon.social",
    "b" * 80 + "@mastodon.social",
    "b" * 81 + "@mastodon.social",
]

# Same account on two different servers. Nothing about the local part distinguishes these.
CROSS_INSTANCE_CORPUS = [
    "alice@mastodon.social",
    "alice@pleroma.example",
    "alice@poster.place",
]

ALL = HIJACK_CORPUS + CHARSET_CORPUS + TRUNCATION_CORPUS + CROSS_INSTANCE_CORPUS


def _old_rule(acct: str) -> str:
    """The pre-fix implementation: the sanitized form alone, no digest. Kept here so the corpus
    above is proven to be a corpus that actually distinguishes the two rules."""
    raw = (acct or "").strip().lower()
    local, _, host = raw.partition("@")
    base = ident._sanitize(local) or "user"
    h = ident._sanitize(host)
    return (f"{base}_{h}" if h else base)[:64].strip("._-")


# --------------------------------------------------------------------------- the hijack itself


def test_no_two_distinct_handles_ever_share_a_nip05_name():
    """The whole point. Last-write-wins on the relay's NIP-05 map means a collision IS a takeover."""
    seen = {}
    for acct in ALL:
        name = ident.nip05_name_for(acct)
        if name in seen and seen[name] != acct:
            pytest.fail(
                "NIP-05 name collision — %r and %r both resolve to %r.\n"
                "The relay's NIP-05 map is last-write-wins, so whichever publishes second "
                "takes over the other's verified name." % (seen[name], acct, name)
            )
        seen[name] = acct


def test_the_pre_fix_rule_still_collides():
    """Proves this file can fail. If the corpus above stops distinguishing the two rules, the test
    above becomes decorative and would keep passing against a reintroduced hijack."""
    seen = {}
    collisions = []
    for acct in ALL:
        name = _old_rule(acct)
        if name in seen and seen[name] != acct:
            collisions.append((seen[name], acct, name))
        seen[name] = acct
    assert collisions, (
        "the pre-fix rule no longer collides on this corpus, so the guard above is no longer "
        "being exercised — extend the corpus rather than deleting this test"
    )


def test_a_decorated_variant_cannot_impersonate_the_plain_handle():
    """Named separately from the sweep because this is the exact reported attack."""
    victim = ident.nip05_name_for("victim@mastodon.social")
    for attacker in ("_victim@mastodon.social", "victim_@mastodon.social",
                     "_victim_@mastodon.social", ".victim.@mastodon.social"):
        assert ident.nip05_name_for(attacker) != victim, \
            f"{attacker} can take over {victim}'s verified NIP-05 name"


def test_the_same_handle_on_two_instances_gets_two_names():
    names = {ident.nip05_name_for(a) for a in CROSS_INSTANCE_CORPUS}
    assert len(names) == len(CROSS_INSTANCE_CORPUS)


# --------------------------------------------------------------------------- back-compat


def test_a_clean_handle_keeps_its_pretty_name():
    """The fix is deliberately narrow: handles that sanitise cleanly are UNCHANGED, so existing
    puppets keep the name they were provisioned under. A digest on everything would silently
    re-name every puppet on this instance the day it shipped."""
    assert ident.nip05_name_for("alice@mastodon.social") == "alice_mastodon.social"
    assert ident.nip05_name_for("bob.smith@pleroma.example") == "bob.smith_pleroma.example"
    assert ident.nip05_name_for("a_b-c@x.y") == "a_b-c_x.y"


def test_a_lossy_handle_is_suffixed_rather_than_rejected():
    """It must still PRODUCE a name — refusing would leave the puppet with no NIP-05 at all."""
    name = ident.nip05_name_for("_alice_@mastodon.social")
    assert name.startswith("alice_mastodon.social_")
    assert re.fullmatch(r"alice_mastodon\.social_[0-9a-f]{6}", name)


# --------------------------------------------------------------------------- the name is servable


@pytest.mark.parametrize("acct", ALL + ["", "@host", "alice@", "bob", "  ALICE@Mastodon.Social  "])
def test_every_name_is_a_legal_nip05_local_part(acct):
    """A name outside the charset, or over 64 characters, is one the relay cannot serve — which is
    the same user-visible outcome as the collision (no verified name), arrived at differently."""
    name = ident.nip05_name_for(acct)
    assert name, f"{acct!r} produced no name at all"
    assert len(name) <= 64, f"{acct!r} -> {len(name)} characters"
    assert re.fullmatch(r"[a-z0-9_.\-]+", name), f"{acct!r} -> {name!r} is outside the charset"
    assert name[0] not in "._-" and name[-1] not in "._-", \
        f"{acct!r} -> {name!r} has a leading/trailing separator"


def test_case_and_whitespace_are_the_same_account():
    """A puppet that re-provisions under a second name every time the instance reports the handle
    with different capitalisation would collide with itself."""
    canonical = ident.nip05_name_for("alice@mastodon.social")
    for variant in ("ALICE@MASTODON.SOCIAL", "  alice@mastodon.social  ", "Alice@Mastodon.Social"):
        assert ident.nip05_name_for(variant) == canonical


def test_it_is_deterministic():
    """The name is re-derived on every refresh, not stored, so instability means a new NIP-05 row
    per poll and a puppet whose verified name keeps moving."""
    for acct in ALL:
        assert ident.nip05_name_for(acct) == ident.nip05_name_for(acct)


# --------------------------------------------------------------------------- acct_of / actor_uri_of


def test_a_local_handle_is_qualified_with_the_instance_it_was_read_from():
    """Mastodon/Pleroma report a BARE `acct` for local users. Unqualified, `alice` on two different
    instances produces one name for both — the cross-instance half of the same hijack, and the one
    the digest cannot save you from because neither handle is lossy."""
    a = ident.acct_of({"acct": "alice"}, "mastodon.social")
    b = ident.acct_of({"acct": "alice"}, "pleroma.example")
    assert a == "alice@mastodon.social" and b == "alice@pleroma.example"
    assert ident.nip05_name_for(a) != ident.nip05_name_for(b)


def test_an_already_qualified_handle_is_left_alone():
    assert ident.acct_of({"acct": "alice@elsewhere.example"}, "mastodon.social") \
        == "alice@elsewhere.example"


def test_a_leading_at_is_stripped():
    assert ident.acct_of({"acct": "@alice@elsewhere.example"}) == "alice@elsewhere.example"


def test_username_is_the_fallback_when_there_is_no_acct():
    assert ident.acct_of({"username": "alice"}, "mastodon.social") == "alice@mastodon.social"


def test_the_actor_uri_prefers_uri_over_url():
    """`actor_uri` is the KEY the puppet's secret is derived from (bridge_keys.derive_seckey), so
    this preference decides every puppet's identity. Flip it and every puppet on the instance is
    re-keyed: new npubs, orphaned history, and no error anywhere."""
    account = {"uri": "https://mastodon.social/users/alice",
               "url": "https://mastodon.social/@alice"}
    assert ident.actor_uri_of(account) == "https://mastodon.social/users/alice"


def test_the_actor_uri_falls_back_to_url():
    """Some servers only send `url`. Returning "" there would derive every such puppet from the
    SAME empty key — one shared identity for every account on that instance."""
    assert ident.actor_uri_of({"url": "https://pleroma.example/users/bob"}) \
        == "https://pleroma.example/users/bob"


def test_a_puppet_with_no_actor_uri_is_reported_as_empty_not_invented():
    assert ident.actor_uri_of({}) == ""
