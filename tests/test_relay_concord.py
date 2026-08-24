"""Relay invariants required by Concord private streams (CORD-01)."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "app/services/nostr_relay/server.py").read_text()
STORE = (ROOT / "app/services/nostr_relay/store.py").read_text()


def test_concord_wraps_do_not_use_dm_recipient_wot_gate():
    """A Concord p-tag is cover traffic and its stream author is a derived key."""
    branch = re.search(r"elif kind == 1059:(.*?)(?=\n        elif )", SERVER, re.S)
    assert branch, "kind-1059 needs an explicit Concord-compatible admission branch"
    assert "pass" in branch.group(1)
    assert "is_member" not in branch.group(1)
    assert "_dm_for_operator" not in branch.group(1)


def test_concord_stream_wrap_cannot_be_deleted_by_shared_author():
    """Every member knows the stream key; it must not confer relay-side delete power."""
    assert 'pubkey=? AND kind<>1059 RETURNING id' in STORE


def test_nip11_advertises_cord_family_without_inventing_a_nip_number():
    assert '"concord": {"cords": [1, 2, 3, 4, 5, 6, 7, 8]' in SERVER
    supported = re.search(r'"supported_nips": \[([^]]+)\]', SERVER)
    assert supported and "1059" not in supported.group(1)
