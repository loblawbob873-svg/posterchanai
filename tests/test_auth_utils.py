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

`get_user_from_api_key` takes the user_id rather than walking `api_key.user`, deliberately — the
docstring says it is to avoid a lazy load on a session that may already be in trouble. That is the
kind of decision a later "simplification" undoes, so it is stated here too.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, APIKey, User
from app.utils import auth_utils


TOKEN = "sk-" + "a" * 40


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, APIKey.__table__])
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, username="alice", password_hash="x"))
    session.add(User(id=2, username="bob", password_hash="x"))
    session.add(APIKey(id=1, user_id=1, key=TOKEN, name="Default", is_active=True))
    session.commit()
    yield session
    session.close()


class Boom:
    """A session that fails a given number of times before behaving. `is_active` is real, because
    the retry checks it before rolling back."""

    def __init__(self, real, fail_times, is_active=True, rollback_raises=False):
        self._real = real
        self._left = fail_times
        self.is_active = is_active
        self.rollbacks = 0
        self.rollback_raises = rollback_raises

    def query(self, *a, **kw):
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("InterfaceError: connection already closed")
        return self._real.query(*a, **kw)

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_raises:
            raise RuntimeError("cannot rollback, connection is gone")


# --------------------------------------------------------------------------- the happy path


def test_a_live_key_resolves_to_its_user(db):
    ak, user_id = auth_utils.query_api_key_with_retry(db, TOKEN)
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


# --------------------------------------------------------------------------- revocation


def test_a_deactivated_key_does_not_authenticate(db):
    """THE REVOCATION SWITCH. Without the `is_active` filter every key an admin ever turned off
    starts working again — no error, no log, just a request that succeeds for a key its owner
    believes is dead."""
    db.query(APIKey).filter(APIKey.id == 1).first().is_active = False
    db.commit()
    assert auth_utils.query_api_key_with_retry(db, TOKEN) == (None, None)


def test_reactivating_a_key_brings_it_back(db):
    """The other direction, so the filter cannot become 'nothing authenticates'."""
    row = db.query(APIKey).filter(APIKey.id == 1).first()
    row.is_active = False
    db.commit()
    assert auth_utils.query_api_key_with_retry(db, TOKEN) == (None, None)
    row.is_active = True
    db.commit()
    assert auth_utils.query_api_key_with_retry(db, TOKEN)[1] == 1


def test_only_the_matching_key_resolves(db):
    """One user's token must never resolve to another's row."""
    other = "sk-" + "b" * 40
    db.add(APIKey(id=2, user_id=2, key=other, name="Bob", is_active=True))
    db.commit()
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


def test_a_persistent_database_error_denies_rather_than_grants(db):
    """THE ONE THAT MATTERS. The retry exists for transient session errors; when it gives up it
    must deny. Anything that let an exception become a truthy result would authenticate on a
    database hiccup — the request succeeds, and the only trace is a warning line."""
    broken = Boom(db, fail_times=99)
    assert auth_utils.query_api_key_with_retry(broken, TOKEN) == (None, None)


def test_a_transient_error_is_retried_and_succeeds(db):
    """The reason the retry is there at all — otherwise one flaky session error is a failed API
    call for a perfectly good key."""
    flaky = Boom(db, fail_times=1)
    ak, user_id = auth_utils.query_api_key_with_retry(flaky, TOKEN)
    assert user_id == 1


def test_the_session_is_rolled_back_before_the_retry(db):
    """A retry on the same poisoned transaction fails identically, which surfaces as "your key is
    invalid" rather than as the session problem it is."""
    flaky = Boom(db, fail_times=1)
    auth_utils.query_api_key_with_retry(flaky, TOKEN)
    assert flaky.rollbacks == 1


def test_the_retry_count_is_bounded(db):
    """`max_retries` has to bound the attempts. An unbounded loop here is a hang INSIDE a request,
    holding a worker, on the path every API call takes."""
    broken = Boom(db, fail_times=99)
    auth_utils.query_api_key_with_retry(broken, TOKEN, max_retries=2)
    assert broken.rollbacks == 3, "expected exactly max_retries + 1 attempts"


def test_zero_retries_means_one_attempt(db):
    broken = Boom(db, fail_times=99)
    assert auth_utils.query_api_key_with_retry(broken, TOKEN, max_retries=0) == (None, None)
    assert broken.rollbacks == 1


def test_a_rollback_that_itself_fails_does_not_escape(db):
    """A closed connection raises on rollback too. That must not turn a denied request into a 500
    — and must still deny."""
    broken = Boom(db, fail_times=99, rollback_raises=True)
    assert auth_utils.query_api_key_with_retry(broken, TOKEN) == (None, None)


def test_an_inactive_session_is_not_rolled_back(db):
    """`db.is_active` is checked first; rolling back a session that is already done is its own
    error, inside the handler for an error."""
    broken = Boom(db, fail_times=99, is_active=False)
    assert auth_utils.query_api_key_with_retry(broken, TOKEN) == (None, None)
    assert broken.rollbacks == 0


def test_the_user_lookup_also_fails_closed(db):
    """Same rule one step later: a user that cannot be read is not a user."""
    broken = Boom(db, fail_times=99)
    assert auth_utils.get_user_from_api_key(broken, 1) is None


def test_the_user_lookup_retries_once(db):
    flaky = Boom(db, fail_times=1)
    assert auth_utils.get_user_from_api_key(flaky, 1).username == "alice"
