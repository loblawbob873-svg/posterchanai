"""Admin -> Identities and the membership check answer the SAME question, and must give the same answer.

"Is this person a member?" is decided twice: nip05_registry.rows() draws "verified" / "not in profile" in
Admin -> Identities (and "Remove all not in profile" acts on it), and instance_membership decides access
-- the 15-minute cleanup revokes on it. They were written separately and drifted: Identities ignored
letter case, membership did not, so DreadPirate (granted 'dreadpirate', profile 'DreadPirate@poster.place')
showed "✓ verified" while the cleanup took AI, Blossom and streaming away. A check of one against fixtures
could never see that; this runs BOTH over one matrix of registries and signed profiles and requires
agreement, case by case, plus the expected answer.
"""
import asyncio
import json

import pytest

from app.services import nip05_registry, relay_blocklist, settings_store
from app.services.instance_membership import MembershipChecker
from app.services.nostr.event import build_event

DOMAIN = "poster.place"
ME, OTHER = b"\x07" * 32, b"\x08" * 32
PK = build_event(ME, 0, "{}")["pubkey"]
OTHER_PK = build_event(OTHER, 0, "{}")["pubkey"]

# (registry lines for PK, what PK's profile publishes, expected member?)
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


@pytest.mark.parametrize("granted,published,member", CASES)
def test_identities_and_membership_agree(monkeypatch, granted, published, member):
    lines = [f"{n} {PK}" for n in granted] + [f"bob {OTHER_PK}"]
    registry = "\n".join(lines)
    vals = {nip05_registry.KEY: registry}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(relay_blocklist, "is_blocked", lambda pk: False)
    profile = build_event(ME, 0, json.dumps({"nip05": published} if published else {}), created_at=100)

    async def profiles(pks):
        return {PK: {"nip05": published}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)

    async def query(pk, port):
        return [profile] if pk == PK else []
    checker = MembershipChecker(query=query, configuration=lambda: [registry, DOMAIN, "", "3052"])
    qualified = asyncio.run(checker.status(PK))["qualified"]
    rows = [r for r in asyncio.run(nip05_registry.rows(DOMAIN))["identities"] if r["pubkey"] == PK]
    verified = bool(rows) and all(r["verified"] for r in rows)

    assert qualified == member, f"membership said {qualified} for {granted} publishing {published!r}"
    assert verified == member, f"Identities said {verified} for {granted} publishing {published!r}"
