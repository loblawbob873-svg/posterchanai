"""Relay invariants required by Concord private streams (CORD-01)."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "app/services/nostr_relay/server.py").read_text()
STORE = (ROOT / "app/services/nostr_relay/store.py").read_text()


def test_concord_wraps_do_not_use_dm_recipient_wot_gate():
    """A Concord p-tag is cover traffic and its stream author is a derived key."""
    branch = re.search(r"elif kind in \(1059, 21059\):(.*?)(?=\n        elif )", SERVER, re.S)
    assert branch, "durable and ephemeral Concord wraps need an explicit admission branch"
    assert "pass" in branch.group(1)
    assert "is_member" not in branch.group(1)
    assert "_dm_for_operator" not in branch.group(1)


def test_concord_stream_wrap_cannot_be_deleted_by_shared_author():
    """Every member knows the stream key; it must not confer relay-side delete power.

    THE PROPERTY, NOT THE SPELLING. This asserted `kind<>1059` verbatim and started failing when
    the same statement grew to exclude kind 5 as well -- i.e. it reported a missing guard about a
    guard that had become STRICTER. The rule is that an author-scoped delete cannot reach a gift
    wrap; how the SQL says so is not the test's business.
    """
    stmt = re.search(r"DELETE FROM events WHERE id=\? AND pubkey=\?[^\"']*", STORE)
    assert stmt, "the author-scoped delete statement is gone entirely"
    assert "1059" in stmt.group(0), (
        "an author-scoped delete can now reach a Concord gift wrap: " + stmt.group(0))


def test_concord_cleanup_lifecycle_matches_cord01_and_cord08():
    """History is durable by default; timers expire it; realtime presence never lands."""
    prunable = re.search(r"_PRUNABLE_KINDS = \(([^)]+)\)", STORE)
    never_expire = re.search(r"_NEVER_EXPIRE_KINDS = ([^\n]+)", STORE)
    assert prunable and "1059" not in prunable.group(1), \
        "ordinary retention/count cleanup must not silently truncate encrypted room history"
    assert never_expire and "1059" not in never_expire.group(1), \
        "CORD-08 NIP-40 expiration must remain able to purge chat wraps"
    assert "expiration IS NOT NULL AND expiration <= ?" in STORE
    assert "if _is_ephemeral(kind):" in SERVER
    assert "20000 <= kind < 30000" in SERVER


def test_nip11_advertises_cord_family_without_inventing_a_nip_number():
    assert '"concord": {"cords": [1, 2, 3, 4, 5, 6, 7, 8]' in SERVER
    supported = re.search(r'"supported_nips": \[([^]]+)\]', SERVER)
    assert supported and "1059" not in supported.group(1)
