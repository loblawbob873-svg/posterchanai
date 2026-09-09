"""GRASP-08: a private repository's EVENTS are private too.

The git HTTP side has long refused unauthorised clones of a private repo, 401ing before
git-http-backend runs so refs never leak. The Nostr half was never covered: the kind-30617
announcement and kind-30618 state were served to anyone who queried this relay. A private repo was
therefore private in its bytes and public in its name, structure, maintainer set and activity —
which is not what anybody means by private.

ngit v3 / GRASP-08 makes the relay half normative. These run the shipped `_can_serve_event`.
"""
from __future__ import annotations

import pytest

from app.services.nostr_relay.server import RelayServer


OWNER = "a" * 64
MAINT = "b" * 64
STRANGER = "c" * 64


class FakeConn:
    pass


def server_with(authed):
    s = object.__new__(RelayServer)
    conn = FakeConn()
    s._auth_pubkeys = {conn: set(authed)}
    return s, conn


def repo_event(kind=30617, private=True, maintainers=(MAINT,)):
    tags = [["d", "myrepo"]]
    if private:
        tags.append(["private", "true"])
    if maintainers:
        tags.append(["maintainers", *maintainers])
    return {"kind": kind, "pubkey": OWNER, "tags": tags, "content": ""}


def test_a_stranger_is_not_served_a_private_repo_announcement():
    s, conn = server_with([STRANGER])
    assert s._can_serve_event(conn, repo_event()) is False


def test_an_unauthenticated_connection_is_not_served_it():
    s, conn = server_with([])
    assert s._can_serve_event(conn, repo_event()) is False


def test_the_owner_is_served_it():
    s, conn = server_with([OWNER])
    assert s._can_serve_event(conn, repo_event()) is True


def test_a_declared_maintainer_is_served_it():
    """The same access set the git side enforces over HTTP: owner ∪ 30617 maintainers."""
    s, conn = server_with([MAINT])
    assert s._can_serve_event(conn, repo_event()) is True


def test_repo_state_is_covered_not_just_the_announcement():
    """30618 carries the refs. Serving it while refusing 30617 would leak exactly what a clone
    is refused."""
    s, conn = server_with([STRANGER])
    assert s._can_serve_event(conn, repo_event(kind=30618)) is False


def test_a_public_repo_is_still_served_to_everyone():
    """The overwhelming majority of repos are public and must not need AUTH to be discovered."""
    s, conn = server_with([])
    assert s._can_serve_event(conn, repo_event(private=False)) is True


def test_privacy_is_read_off_the_event_not_a_local_flag():
    """An announcement replicated from another GRASP service carries its own privacy; a
    server-side flag would say nothing about it."""
    ev = {"kind": 30617, "pubkey": OWNER, "tags": [["d", "r"], ["private", "true"]]}
    s, conn = server_with([STRANGER])
    assert s._can_serve_event(conn, ev) is False


@pytest.mark.parametrize("value", ["TRUE", "True"])
def test_the_private_marker_is_not_case_sensitive(value):
    ev = {"kind": 30617, "pubkey": OWNER, "tags": [["private", value]]}
    s, conn = server_with([STRANGER])
    assert s._can_serve_event(conn, ev) is False


def test_a_non_git_kind_is_untouched_by_this_rule():
    s, conn = server_with([])
    assert s._can_serve_event(conn, {"kind": 1, "pubkey": OWNER, "tags": [["private", "true"]]}) is True


def test_nip78_privacy_still_works():
    """The existing owner gate must not be weakened by adding the git one beside it."""
    s, conn = server_with([STRANGER])
    assert s._can_serve_event(conn, {"kind": 30078, "pubkey": OWNER, "tags": []}) is False
    s2, c2 = server_with([OWNER])
    assert s2._can_serve_event(c2, {"kind": 30078, "pubkey": OWNER, "tags": []}) is True


# --------------------------------------------------------------------------------------------
# GRASP-08 DISCOVERY: kind 10318, the private-repository list.
#
# A NIP-51 list whose `g` tags are ALL private items — each a JSON array NIP-44-encrypted to the
# author — naming the relays that person's PRIVATE repositories live on. Replaceable, so there is
# exactly one per person and the newest wins. It is the answer to the one thing our own model of
# private repos (never announce them at all) does not give you: discoverability. Losing it orphans
# every private repo its owner has, and a private repo is exactly the one nothing else on the
# network points at.
#
# tests/test_relay_prune.py::test_the_private_repo_list_survives_a_stray_expiration_tag drives the
# real store. These two are the cheap invariants that run with no Postgres, so a refactor of the
# kind tuples can never quietly drop it.

def test_kind_10318_can_never_be_deleted_by_an_expiration_tag():
    """NIP-37 recommends stamping `expiration: now + 90 days`; a client following that convention on
    a list would take somebody's private repos off the map three months later."""
    from app.services.nostr_relay import store
    assert 10318 in store._NEVER_EXPIRE_KINDS


def test_kind_10318_is_reachable_by_no_cleaner():
    """Every prune rule in store.py is gated on _PRUNABLE_SQL or _RETIRED_SQL — including the paid
    tier, the only thing here that can delete an origin='direct' event. Being outside both tuples is
    what keeps the list safe from all of them at once."""
    from app.services.nostr_relay import store
    assert 10318 not in store._PRUNABLE_KINDS
    assert 10318 not in store._RETIRED_KINDS
    assert store.retired_kind_reason(10318) is None, "a retired kind is refused at ingest"
    assert store._REPLACEABLE(10318), "10318 is a NIP-51 replaceable list — newest wins, one per author"
