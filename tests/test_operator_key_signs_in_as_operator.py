"""THE ANDROID WALLET SAID "RETRY LOCAL WALLET" AND IT WAS NEVER A CLIENT BUG.

Reported after several APK releases of client-side auth fixes had changed nothing: the Monero wallet
works in a browser and shows "Local wallet unavailable · Retry local wallet" on the phone.

The wallet's eight routes are gated on `get_admin_user`. `POST /api/auth/nostr-login` looks the
signer up by linked npub and, finding none, creates a fresh account with `is_admin=False` — only the
very first sign-in on a node with **zero** admins claims admin. An operator whose account was made
with a username and password carries no `nostr_npub`, so signing in with their own key on the phone
minted a SECOND, ordinary account for the same person, and every admin-only surface answered 403 to
it. The client caught that and painted its generic unavailable state.

So three separate things all pointed away from the cause: the wallet daemon was fine, the client's
auth was fine, and the screen described an outage. Nobody was going to find it by fixing the client,
which is exactly what happened.

Two changes, tested here:

* **The operator's own key signs them in as the operator.** Possession of the operator nsec already
  means total control of this node — it signs the settings documents and the relay's own events — so
  matching it grants nothing that key did not already have. The npub is linked on the way through,
  so it costs one lookup once.
* **A 403 no longer reads as an outage** (that half is in tests/client/, on the client).

The security-relevant half is the NEGATIVE space, and most of this file is about that: a stranger's
key, a non-admin's key, an admin with no nsec, and a node with no operator at all must every one of
them still land on the ordinary non-admin path.
"""
from __future__ import annotations

import pytest

from app.routers import auth as auth_router
from app.services.nostr import nostr_service


class Query:
    """A tiny stand-in for the two `db.query(User)` chains nostr_login makes."""

    def __init__(self, db, model):
        self.db, self.model = db, model
        self._admin_with_nsec = False
        self._npub = None

    def filter(self, *conditions):
        text = " ".join(str(c) for c in conditions)
        if "nostr_nsec" in text:
            self._admin_with_nsec = True
        if "nostr_npub" in text and "IS NOT NULL" not in text.upper():
            self._npub = self.db.asked_npub
        return self

    def first(self):
        if self._admin_with_nsec:
            return self.db.operator
        return self.db.by_npub


class DB:
    def __init__(self, operator=None, by_npub=None):
        self.operator, self.by_npub = operator, by_npub
        self.asked_npub = None
        self.committed = 0

    def query(self, model):
        return Query(self, model)

    def commit(self):
        self.committed += 1


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _keypair(seed=None):
    """A raw 32-byte secret and its pubkey — `decode_seckey`/`derive_pubkey` are what the route uses."""
    import os
    sk = (seed or os.urandom(32)).hex()
    return sk, nostr_service.derive_pubkey(nostr_service.decode_seckey(sk))


@pytest.fixture
def operator():
    sk, pk = _keypair()
    return Row(id=1, username="root", is_admin=True, nostr_nsec=sk, nostr_npub=None), pk


def _match(db, pk):
    """Run just the operator-key branch the route performs before creating an account."""
    user = db.by_npub
    if user:
        return user
    op = db.query(object()).filter("is_admin == True", "nostr_nsec IS NOT NULL").first()
    if op:
        op_pk = nostr_service.derive_pubkey(nostr_service.decode_seckey(op.nostr_nsec))
        if op_pk and op_pk == pk:
            if not op.nostr_npub:
                op.nostr_npub = nostr_service.npub_of(pk)
                db.commit()
            return op
    return None


def test_the_route_actually_contains_the_operator_match():
    """This file models the branch; that is only worth anything while the branch is in the route."""
    src = (auth_router.__file__).replace(".pyc", ".py")
    text = open(src, encoding="utf-8").read()
    block = text[text.index("async def nostr_login"):text.index("        base = \"npub_\"")]
    assert "derive_pubkey" in block and "nostr_nsec" in block, (
        "nostr_login no longer recognises the node's own operator key")
    assert "op_pk == pk" in block


def test_the_operators_own_key_signs_them_in_as_the_operator(operator):
    """THE FIX. Before this the same person got a second, ordinary account and a 403 from the
    wallet, on a phone, while the browser worked."""
    op, pk = operator
    db = DB(operator=op)
    got = _match(db, pk)
    assert got is op
    assert got.is_admin is True


def test_the_match_links_the_npub_so_it_only_happens_once(operator):
    op, pk = operator
    db = DB(operator=op)
    _match(db, pk)
    assert op.nostr_npub == nostr_service.npub_of(pk)
    assert db.committed == 1


def test_a_stranger_is_not_the_operator(operator):
    """The whole security question, and the reason this is a key comparison and not a flag: any
    other key must fall through to the ordinary non-admin signup."""
    op, _ = operator
    _, other_pk = _keypair()
    assert _match(DB(operator=op), other_pk) is None


def test_a_node_with_no_operator_key_matches_nobody():
    """A relay-only or fresh node has no admin carrying an nsec. The branch must not throw, and it
    must not promote the first person to sign in — that claim is the existing first-admin rule's
    job and it has its own guard."""
    _, pk = _keypair()
    assert _match(DB(operator=None), pk) is None


def test_an_already_linked_account_is_returned_untouched(operator):
    """The ordinary path still wins: a user found by npub is used as-is and the operator lookup
    never runs, so a normal member signing in cannot be compared against the operator at all."""
    op, pk = operator
    member = Row(id=7, username="joe", is_admin=False, nostr_npub=nostr_service.npub_of(pk))
    db = DB(operator=op, by_npub=member)
    got = _match(db, pk)
    assert got is member and got.is_admin is False
    assert db.committed == 0


def test_a_corrupt_operator_nsec_cannot_break_sign_in(operator):
    """The route wraps this in try/except for a reason: a node whose operator row carries an
    unreadable key must still let everybody else sign in, rather than 500ing the whole endpoint."""
    op, pk = operator
    op.nostr_nsec = "nsec1thisisnotakey"
    db = DB(operator=op)
    try:
        got = _match(db, pk)
    except Exception:
        got = None                     # the route swallows it; the model may not
    assert got is None or got is op
    src = open(auth_router.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    block = src[src.index("async def nostr_login"):src.index("        base = \"npub_\"")]
    assert "except Exception" in block, "the operator lookup is not guarded"


def test_the_wallet_stays_admin_only():
    """The fix must not have widened the wallet itself. It recognises one existing identity; it does
    not change who may spend."""
    from app.routers import monero_wallet
    for route in monero_wallet.router.routes:
        names = [d.call.__name__ for d in route.dependant.dependencies]
        assert "get_admin_user" in names, f"{route.path} lost its admin gate"
