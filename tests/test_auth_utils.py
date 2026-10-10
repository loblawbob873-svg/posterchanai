"""API KEY RESOLUTION — THE AUTHENTICATION STEP, WITH NO TESTS BEHIND IT.

`app/utils/auth_utils.py` had ZERO test references. It is what turns a bearer token into a user for
the OpenAI-compatible `/v1/` surface, so everything it decides is an access decision.

Two of its properties are security properties rather than correctness ones, and both are quiet when
they break:

  * **`is_active` is the revocation switch.** Drop that filter and every key an admin ever
    deactivated starts working again. Nothing errors; the request simply succeeds, for a key its
    owner believes is dead.
  * **AN ERROR MUST FAIL CLOSED.** The whole function is wrapped in a retry that exists for
    transient session errors, and the branch that gives up returns `(None, None)`. A version that
    returned a partially-built tuple, or swallowed the exception into a truthy value, would
    authenticate on a database hiccup.

The retry itself is worth pinning because it is easy to make worse in either direction: retry
forever and a broken session becomes a hang inside a request; do not roll back first and the retry
runs on the same poisoned transaction and fails identically, which reads as "the key is invalid".

#161: the keys moved from the SQL `api_keys` table to the `api_keys` DocTable (operator-encrypted
documents on this node's relay). The SQL session retry is gone with SQL; what it protected is pinned
here in its new form -- a table that cannot be read RAISES `Unavailable` (callers answer 503), it never
resolves a key and never reads as "no such key", and the next call after the relay is back resolves.

`get_user_from_api_key` takes the user_id rather than walking `api_key.user`, deliberately — the
docstring says it is to avoid a lazy load on a session that may already be in trouble. That is the
kind of decision a later "simplification" undoes, so it is stated here too.
"""
import asyncio

import pytest

from app.database import NoSQL
from app.services import api_key_store, user_table
from app.services.relay_reader import Unavailable
from app.utils import auth_utils
from tests.doc_table_mem import mem_tables


TOKEN = "sk-" + "a" * 40


def _key(kid, user_id, key, name="Default", active=True):
    return {"id": kid, "user_id": user_id, "key": key, "name": name, "created_at": None,
            "last_used_at": None, "is_active": active}


def put_key(m, kid, user_id, key, **kw):
    """A key as the store holds it: the row, and its index entry (what a lookup before the load reads)."""
    m.put("api_keys", str(kid), _key(kid, user_id, key, **kw))
    m.put("api_key_index", api_key_store.key_hash(key), {"id": kid})


@pytest.fixture
def mem(monkeypatch):
    m = mem_tables(monkeypatch)
    put_key(m, 1, 1, TOKEN)
    for uid, name in ((1, "alice"), (2, "bob")):
        m.put(user_table.TABLE, str(uid), user_table.UserRow(uid, {"username": name, "password_hash": "x"}).row())
    return m


@pytest.fixture
def db(mem):
    return NoSQL()          # the `db` the app hands around: there is no SQL behind it (#161)


# --------------------------------------------------------------------------- the happy path


def test_a_live_key_resolves_to_its_user(db):
    ak, user_id = auth_utils.query_api_key_with_retry(db, TOKEN)
    assert ak is not None and user_id == 1


def test_the_async_lookup_agrees(db):
    ak, user_id = asyncio.run(auth_utils.aquery_api_key(TOKEN))
    assert ak is not None and user_id == 1


def test_the_user_id_is_returned_eagerly(db):
    """"Returns a tuple of (api_key, user_id) to avoid lazy loading issues. The user_id is eagerly
    fetched while the session is still valid." Returning only the key and letting the caller walk
    `.user` is the failure this signature exists to prevent."""
    _ak, user_id = auth_utils.query_api_key_with_retry(db, TOKEN)
    assert isinstance(user_id, int)


def test_the_user_is_looked_up_by_id(db):
    user = auth_utils.get_user_from_api_key(db, 1)
    assert user is not None and user.username == "alice"


def test_the_two_halves_compose(db):
    ak, user_id = auth_utils.query_api_key_with_retry(db, TOKEN)
    assert auth_utils.get_user_from_api_key(db, user_id).username == "alice"


def test_the_key_row_reads_like_the_old_orm_row(db):
    ak, _ = auth_utils.query_api_key_with_retry(db, TOKEN)
    assert ak.id == 1 and ak.key == TOKEN and ak.is_active is True and ak.name == "Default"


# --------------------------------------------------------------------------- revocation


def test_a_deactivated_key_does_not_authenticate(db, mem):
    """THE REVOCATION SWITCH. Without the `is_active` filter every key an admin ever turned off
    starts working again — no error, no log, just a request that succeeds for a key its owner
    believes is dead."""
    from app.services import api_key_store
    api_key_store.set_active(1, False)
    assert auth_utils.query_api_key_with_retry(db, TOKEN) == (None, None)
    assert mem.table("api_keys")["1"]["is_active"] is False, "the revocation must be ON THE RELAY"


def test_reactivating_a_key_brings_it_back(db):
    """The other direction, so the filter cannot become 'nothing authenticates'."""
    from app.services import api_key_store
    api_key_store.set_active(1, False)
    assert auth_utils.query_api_key_with_retry(db, TOKEN) == (None, None)
    api_key_store.set_active(1, True)
    assert auth_utils.query_api_key_with_retry(db, TOKEN)[1] == 1


def test_a_row_with_no_active_flag_is_not_active(db, mem):
    """`is_active == True` never matched a NULL in SQL; a migrated NULL must not turn into a live key."""
    mem.put("api_keys", "3", {**_key(3, 2, "sk-" + "n" * 40), "is_active": None})
    assert auth_utils.query_api_key_with_retry(db, "sk-" + "n" * 40) == (None, None)


def test_only_the_matching_key_resolves(db, mem):
    """One user's token must never resolve to another's row."""
    other = "sk-" + "b" * 40
    put_key(mem, 2, 2, other, name="Bob")
    assert auth_utils.query_api_key_with_retry(db, TOKEN)[1] == 1
    assert auth_utils.query_api_key_with_retry(db, other)[1] == 2


# --------------------------------------------------------------------------- non-matches


@pytest.mark.parametrize("token", [
    "sk-" + "z" * 40,                 # simply unknown
    "",                               # empty
    "sk-",                            # the prefix alone
    TOKEN[:-1],                       # one character short
    TOKEN + "x",                      # one character long
    TOKEN.upper(),                    # different case
    " " + TOKEN,                      # leading space
    TOKEN + " ",                      # trailing space
])
def test_a_token_that_is_not_exactly_right_does_not_authenticate(db, token):
    """Exact equality, not a prefix or a LIKE. A prefix match would make `sk-` itself a master key,
    and whitespace tolerance would let a mangled copy-paste authenticate as somebody."""
    assert auth_utils.query_api_key_with_retry(db, token) == (None, None)


def test_an_unknown_key_returns_a_pair_of_nones(db):
    """Callers unpack two values. A bare `None` would raise on unpack, inside the auth path."""
    result = auth_utils.query_api_key_with_retry(db, "sk-nope")
    assert result == (None, None)
    ak, user_id = result                       # must unpack
    assert ak is None and user_id is None


def test_an_unknown_user_id_is_none(db):
    assert auth_utils.get_user_from_api_key(db, 999) is None


# --------------------------------------------------------------------------- failing closed


def test_an_unreadable_key_table_denies_rather_than_grants(db, mem):
    """THE ONE THAT MATTERS. A table that cannot be read must never authenticate -- and must not
    read as "no such key" either (that is a 401 for a perfectly good key, i.e. an outage that looks
    like a revocation). It raises, and the routes answer 503."""
    mem.down = True
    with pytest.raises(Unavailable):
        auth_utils.query_api_key_with_retry(db, TOKEN)
    with pytest.raises(Unavailable):
        asyncio.run(auth_utils.aquery_api_key(TOKEN))


def test_a_transient_outage_recovers_on_the_next_call(db, mem):
    """The reason the old retry was there at all -- one flaky read must not leave a good key failing."""
    mem.down = True
    with pytest.raises(Unavailable):
        auth_utils.query_api_key_with_retry(db, TOKEN)
    mem.down = False
    assert auth_utils.query_api_key_with_retry(db, TOKEN)[1] == 1


def test_an_unreadable_account_is_unavailable_never_an_unknown_user(db, mem):
    """Same rule one step later: an account that could not be read is neither a user nor "no such user" -- the
    latter is a 401 that logs a perfectly good key out during an outage."""
    mem.down = True
    with pytest.raises(Unavailable):
        auth_utils.get_user_from_api_key(db, 1)
    mem.down = False
    assert auth_utils.get_user_from_api_key(db, 1).username == "alice"
