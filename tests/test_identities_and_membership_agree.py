"""Admin -> Identities' ✓ and the membership check answer DIFFERENT questions now, each exactly.

Membership is a name this node granted -- whatever the profile shows ("the entire point was to display
both": somebody with an identity of their own keeps it AND is a member here). The ✓ in Admin ->
Identities is information only: does their profile ALSO publish this address. It was the membership
rule, and the two drifted once on letter case (DreadPirate: granted 'dreadpirate', profile
'DreadPirate@poster.place', "✓ verified" while the cleanup took AI, Blossom and streaming away), so the
matrix below still pins letter case and the second-of-two-names for the ✓ -- and pins that NO profile
can make a granted key a non-member, or an ungranted one a member.
"""
import asyncio

import pytest

from app.services import nip05_registry, relay_blocklist, settings_store
from app.services.instance_membership import MembershipChecker
from app.services.nostr.event import build_event

DOMAIN = "poster.place"
ME, OTHER = b"\x07" * 32, b"\x08" * 32
PK = build_event(ME, 0, "{}")["pubkey"]
OTHER_PK = build_event(OTHER, 0, "{}")["pubkey"]

# (registry lines for PK, what PK's profile publishes, does Identities show ✓?)
CASES = [
    (["alice"], "alice@poster.place", True),
    (["alice"], "Alice@poster.place", True),            # letter case (DreadPirate)
    (["alice"], "ALICE@POSTER.PLACE", True),
    (["alice", "ally"], "ally@poster.place", True),      # the SECOND of two names
    (["alice", "ally"], "Ally@poster.place", True),
    (["alice"], "alice@evil.example", False),            # another domain
    (["alice"], "alice@poster.place.evil", False),
    (["alice"], "bob@poster.place", False),              # somebody else's name (see OTHER below)
    (["alice"], "", False),                              # nothing published
    ([], "alice@poster.place", False),                   # granted nothing here at all
]


@pytest.mark.parametrize("granted,published,shows_it", CASES)
def test_membership_is_the_grant_and_the_tick_is_the_profile(monkeypatch, granted, published, shows_it):
    lines = [f"{n} {PK}" for n in granted] + [f"bob {OTHER_PK}"]
    registry = "\n".join(lines)
    vals = {nip05_registry.KEY: registry}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(relay_blocklist, "is_blocked", lambda pk: False)

    async def profiles(pks):
        return {PK: {"nip05": published}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)

    checker = MembershipChecker(configuration=lambda: [registry, DOMAIN, ""])
    qualified = asyncio.run(checker.status(PK))["qualified"]
    rows = [r for r in asyncio.run(nip05_registry.rows(DOMAIN))["identities"] if r["pubkey"] == PK]
    verified = bool(rows) and all(r["verified"] for r in rows)

    assert qualified == bool(granted), f"membership said {qualified} for {granted} publishing {published!r}"
    assert verified == shows_it, f"Identities said {verified} for {granted} publishing {published!r}"
