"""One person, several NIP-05 names on this node ("add support for user having multiple nip05 addresses").

The registry always allowed several "<name> <npub>" lines for one key, and nostr.json serves them all --
but every way of WRITING it assumed one: an admin grant dropped the person's other names, "remove" took
them all, and the Identities list judged each name against the ONE address a kind-0 can carry, so every
extra name read "not in profile" and the "Remove all not in profile" button would have revoked them.
Driven through the real router with a real signed admin event (the instance-welcome harness).
"""
import asyncio
import base64
import json

import httpx
import pytest

from app.models import User
from app.services import nip05_registry, relay_blocklist, settings_store
from app.services.nostr.event import build_event
from tests import test_instance_welcome as welcome
from tests.test_instance_welcome import proof


@pytest.fixture
def setup(monkeypatch):
    # The instance-welcome harness: a real app, a database with an admin, settings in a dict.
    yield from welcome.setup.__wrapped__(monkeypatch)


def _client(app, values, monkeypatch, db):
    from app.routers.client import router
    from app.services.nostr import nostr_service
    from app.services.nostr_relay import thread
    app.include_router(router)
    monkeypatch.setattr(settings_store, 'put', lambda key, value: values.__setitem__(key, value))
    monkeypatch.setattr(settings_store, 'hydrate_from_db', lambda db: None)
    monkeypatch.setattr(thread, 'trigger_nip05_reload', lambda: None)
    admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
    admin.nostr_npub = nostr_service.npub_of(proof(key='22')['pubkey']); db.commit()

    def post(target, **body):
        ev = build_event(bytes.fromhex('22' * 32), 27235, 'nip05', [['p', target]])
        body = {'target': target, 'auth': base64.b64encode(json.dumps(ev).encode()).decode(), **body}

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://example.test') as c:
                return await c.post('/client/admin-nip05', json=body)
        return asyncio.run(go())

    def get(target):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://example.test') as c:
                return await c.get('/client/admin-nip05', params={'pubkey': target})
        return asyncio.run(go()).json()
    return post, get


def _names_of(values, pk):
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(values.get('nostr_relay_nip05_names', ''), '')
    return sorted(n for n, k in names.items() if k == pk)


def test_an_admin_can_give_one_person_several_names_and_take_one_away(setup, monkeypatch):
    app, db, _sent, values = setup
    post, get = _client(app, values, monkeypatch, db)
    pk, other = proof()['pubkey'], proof(key='33')['pubkey']
    values['nostr_relay_nip05_names'] = f'bob {other}'

    assert post(pk, name='alice').json()['names'] == ['alice']
    r = post(pk, name='ally', add=True).json()
    assert r['ok'] and r['names'] == ['alice', 'ally'] and r['nip05'] == 'ally@example.test'
    assert _names_of(values, pk) == ['alice', 'ally'], "an added name replaced the one they had"
    assert post(pk, name='ally', add=True).json().get('existing') is True
    assert _names_of(values, pk) == ['alice', 'ally'] and values['nostr_relay_nip05_names'].count('ally') == 1
    assert post(pk, name='bob', add=True).status_code == 409, "somebody else's name was handed over"
    assert _names_of(values, other) == ['bob']

    g = get(pk)
    assert g['names'] == ['alice', 'ally'] and g['addresses'] == ['alice@example.test', 'ally@example.test']
    assert g['name'] == 'alice', "clients written for one name read `name`"

    r = post(pk, name='ally', remove=True)
    assert r.status_code == 200 and r.json()['names'] == ['alice']
    assert _names_of(values, pk) == ['alice'], "removing one name took the others"
    assert post(pk, name='nope', remove=True).status_code == 404
    assert _names_of(values, other) == ['bob']


def test_what_older_clients_send_still_means_what_it_meant(setup, monkeypatch):
    app, db, _sent, values = setup
    post, _get = _client(app, values, monkeypatch, db)
    pk = proof()['pubkey']
    post(pk, name='alice'); post(pk, name='ally', add=True)
    assert _names_of(values, pk) == ['alice', 'ally']
    post(pk, name='carol')                           # a plain grant replaces
    assert _names_of(values, pk) == ['carol']
    post(pk, name='dave', add=True)
    post(pk, remove=True)                            # a plain remove clears them all
    assert _names_of(values, pk) == []


def test_an_extra_name_verifies_through_the_one_the_profile_publishes(monkeypatch):
    from app.services.nostr import bip340
    pk = bip340.pubkey_from_seckey(b'\x05' * 32).hex()
    stranger = bip340.pubkey_from_seckey(b'\x06' * 32).hex()
    vals = {nip05_registry.KEY: f"alice {pk}\nally {pk}\nghost {stranger}"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    import app.services.nostr_relay.thread as thread
    monkeypatch.setattr(thread, "trigger_nip05_reload", lambda: {})

    async def profiles(pks):
        return {pk: {"name": "Alice", "nip05": "alice@poster.place"}, stranger: {"nip05": "x@elsewhere"}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    rows = {r["name"]: r for r in asyncio.run(nip05_registry.rows("poster.place"))["identities"]}
    assert rows["alice"]["verified"] and rows["alice"]["via"] == ""
    assert rows["ally"]["verified"] and rows["ally"]["via"] == "alice@poster.place", rows["ally"]
    assert rows["ally"]["others"] == ["alice"] and not rows["ghost"]["verified"]


def test_a_member_with_two_names_has_one_handle_everywhere(monkeypatch):
    """Community stats named a member by whichever name a dict iterated LAST, the fediverse by the first
    alphabetically -- one person, two handles. Both now use the same one."""
    from app.services import community_stats
    from app.services.activitypub import actors, config
    pk = 'ab' * 32
    vals = {"nostr_relay_nip05_names": f"alpha {pk}\nzed {pk}"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(config, "domain", lambda: "poster.place")
    actors._names_cache.update(raw=None)
    assert community_stats.members() == {pk: "@alpha@poster.place"}
    assert actors._registry()[1][pk] == "alpha"
