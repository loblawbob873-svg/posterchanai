"""Membership is a name this node granted to the key -- and nothing the profile says changes that.

"the entire point was to display both": a kind-0 holds ONE nip05, and membership used to require it to
be THIS node's address, so anybody with an identity of their own (bob@nostrplebs.com) had to give it up
to keep AI, uploads, Mail, Git, Office, Files. The registry is the authority (written by this node, never
by a profile); the profile is no longer consulted at all -- which also means no relay is asked anything.
"""
import pytest
from fastapi import HTTPException

from app.services.instance_membership import MembershipChecker
from app.services.nostr.event import build_event


@pytest.fixture
def anyio_backend():
    return 'asyncio'


SECRET = bytes.fromhex('01'.zfill(64))
OTHER = bytes.fromhex('02'.zfill(64))
PK = build_event(SECRET, 0, '{}')['pubkey']
OTHER_PK = build_event(OTHER, 0, '{}')['pubkey']


class Fixture:
    def __init__(self, registry=None):
        self.config = [registry if registry is not None else f'alice {PK}', 'example.test', '']
        self.checker = MembershipChecker(configuration=lambda: self.config)


@pytest.fixture(autouse=True)
def nobody_blocked(monkeypatch):
    from app.services import relay_blocklist
    monkeypatch.setattr(relay_blocklist, 'is_blocked', lambda pk: False)


@pytest.mark.anyio
async def test_a_granted_name_is_membership_whatever_the_profile_publishes():
    f = Fixture()
    r = await f.checker.status(PK)
    assert r['qualified'] and r['address'] == 'alice@example.test' and r['reason'] == 'qualified', r


def test_the_profile_is_never_read():
    """Nothing in the module can fetch a profile any more: a bob@nostrplebs.com profile and an empty one
    are the same answer, because neither is looked at (and no relay outage can 503 a member)."""
    import app.services.instance_membership as m
    src = open(m.__file__).read()
    for gone in ('_query_profile', '_read_profile', '_query_upstreams', 'nostr_relay_upstream_relays'):
        assert gone not in src, gone


@pytest.mark.anyio
async def test_every_granted_name_is_reported():
    f = Fixture(f'alice {PK}\nbob {PK}\ncarol {OTHER_PK}')
    r = await f.checker.status(PK)
    assert r['addresses'] == ['alice@example.test', 'bob@example.test'] and r['address'] == 'alice@example.test', r


@pytest.mark.anyio
async def test_unregistered_and_other_owner_cannot_self_claim():
    f = Fixture(f'alice {OTHER_PK}')
    r = await f.checker.status(PK)
    assert not r['qualified'] and r['reason'] == 'unregistered'
    with pytest.raises(HTTPException) as error:
        await f.checker.require_pubkey(PK)
    assert error.value.status_code == 403


@pytest.mark.anyio
async def test_removing_the_name_ends_membership_at_once():
    f = Fixture()
    assert (await f.checker.status(PK))['qualified']
    f.config[0] = ''
    assert not (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_a_blocked_key_is_not_a_member(monkeypatch):
    from app.services import relay_blocklist
    monkeypatch.setattr(relay_blocklist, 'is_blocked', lambda pk: pk == PK)
    r = await Fixture().checker.status(PK)
    assert not r['qualified'] and r['reason'] == 'blocked'


@pytest.mark.anyio
async def test_hex_case_and_npub_are_one_account():
    from app.services.nostr.nostr_service import npub_of
    f = Fixture(f'alice {PK.upper()}')
    assert (await f.checker.status(PK.upper()))['pubkey'] == PK
    assert (await f.checker.status(npub_of(PK)))['qualified']


@pytest.mark.anyio
async def test_missing_domain_is_configuration_unavailable():
    f = Fixture()
    f.config[1] = ''
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503
    f.config[2] = 'https://EXAMPLE.TEST/path'
    assert (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_unhydrated_configuration_is_unavailable(monkeypatch):
    from app.services import instance_membership as membership
    monkeypatch.setattr(membership.settings_store, 'is_hydrated', lambda: False)
    with pytest.raises(HTTPException) as error:
        await MembershipChecker().status(PK)
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_no_account_is_refused():
    with pytest.raises(HTTPException) as error:
        await Fixture().checker.status('')
    assert error.value.status_code == 403
