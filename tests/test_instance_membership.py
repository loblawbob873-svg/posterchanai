"""Signed profile/registry authorization; no external relay or monetary operations."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.instance_membership import MembershipChecker
from app.services.nostr.event import build_event

@pytest.fixture
def anyio_backend():
    return 'asyncio'


SECRET = bytes.fromhex('01'.zfill(64))
OTHER = bytes.fromhex('02'.zfill(64))


def profile(address='alice@example.test', timestamp=100, secret=SECRET):
    return build_event(secret, 0, json.dumps({'nip05': address}), created_at=timestamp)


PK = profile()['pubkey']
OTHER_PK = profile(secret=OTHER)['pubkey']


class Fixture:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [profile()]
        self.config = [f'alice {PK}', 'example.test', '', '3052']
        self.calls = 0
        self.time = 0
        self.error = False
        self.hold = None
        self.checker = MembershipChecker(query=self.query, configuration=lambda: self.config, clock=lambda: self.time)

    async def query(self, pk, port):
        self.calls += 1
        assert port == '3052'
        if self.hold:
            await self.hold.wait()
        if self.error:
            raise OSError('relay unavailable')
        return self.rows


@pytest.mark.anyio
async def test_registered_matching_signed_profile_and_any_alias():
    f = Fixture([profile('second@EXAMPLE.TEST')])
    f.config[0] += f'\nsecond {PK}'
    got = await f.checker.require_pubkey(PK)
    assert got['qualified'] and got['address'] == 'second@example.test'


@pytest.mark.anyio
@pytest.mark.parametrize('address', ['alice@evil.test', 'bob@example.test', '', 'alice@example.test.evil', 'Alice@example.test'])
async def test_wrong_profile_never_grants(address):
    f = Fixture([profile(address)])
    with pytest.raises(HTTPException) as error:
        await f.checker.require_pubkey(PK)
    assert error.value.status_code == 403


@pytest.mark.anyio
async def test_trim_and_domain_normalization():
    f = Fixture([profile('  alice@EXAMPLE.TEST  ')])
    assert (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_unregistered_and_other_owner_cannot_self_claim():
    f = Fixture()
    f.config[0] = f'alice {OTHER_PK}'
    assert (await f.checker.status(PK))['reason'] == 'unregistered'
    assert f.calls == 0


@pytest.mark.anyio
async def test_latest_signed_profile_overrules_old_matching_profile():
    f = Fixture([profile(), profile('alice@elsewhere.test', 101)])
    assert (await f.checker.status(PK))['reason'] == 'profile_mismatch'


@pytest.mark.anyio
async def test_equal_timestamp_uses_lowest_id():
    rows = [profile(), profile('alice@elsewhere.test')]
    f = Fixture(rows)
    assert (await f.checker.status(PK))['qualified'] == (min(rows, key=lambda e: e['id'])['content'] == profile()['content'])


@pytest.mark.anyio
async def test_forgery_and_wrong_author_are_not_profiles():
    forged = profile()
    forged['content'] = json.dumps({'nip05': 'bob@example.test'})
    for rows in [[forged], [profile(secret=OTHER)], []]:
        assert (await Fixture(rows).checker.status(PK))['reason'] == 'profile_missing'


@pytest.mark.anyio
async def test_admin_has_no_exception():
    f = Fixture([])
    with pytest.raises(HTTPException) as error:
        await f.checker.require_user(SimpleNamespace(nostr_npub=PK, is_admin=True, id=1))
    assert error.value.status_code == 403


@pytest.mark.anyio
async def test_transient_failure_is_503_not_revocation_or_stale_grant():
    f = Fixture()
    assert (await f.checker.status(PK))['qualified']
    f.time = 31
    f.error = True
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503
    f.error = False
    assert (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_config_changes_invalidate_cached_grant_immediately():
    f = Fixture()
    assert (await f.checker.status(PK))['qualified']
    f.config[1] = 'new.test'
    assert not (await f.checker.status(PK))['qualified']
    f.config[0] = f'alice {OTHER_PK}'
    assert (await f.checker.status(PK))['reason'] == 'unregistered'


@pytest.mark.anyio
async def test_cache_is_per_account_and_coalesces_cancelled_waiter():
    f = Fixture()
    f.hold = asyncio.Event()
    one = asyncio.create_task(f.checker.status(PK))
    two = asyncio.create_task(f.checker.status(PK))
    await asyncio.sleep(.01)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    f.hold.set()
    assert (await two)['qualified']
    assert f.calls == 1
    assert not (await f.checker.status(OTHER_PK))['qualified']
    assert (await f.checker.status(PK))['qualified']
    assert f.calls == 1


@pytest.mark.anyio
async def test_inflight_registry_revocation_cannot_grant():
    f = Fixture()
    f.hold = asyncio.Event()
    job = asyncio.create_task(f.checker.status(PK))
    await asyncio.sleep(.01)
    f.config[0] = ''
    f.hold.set()
    with pytest.raises(HTTPException) as error:
        await job
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_older_relay_response_cannot_restore_revoked_profile():
    f = Fixture([profile('wrong@example.test', 101)])
    assert not (await f.checker.status(PK))['qualified']
    f.time = 6
    f.rows = [profile()]
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503


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
async def test_future_signed_profile_cannot_pin_a_grant():
    f = Fixture([profile(timestamp=1000), profile('wrong@example.test', 100)])
    f.checker.wallclock = lambda: 100
    assert not (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_unhydrated_configuration_is_unavailable(monkeypatch):
    from app.services import instance_membership as membership
    monkeypatch.setattr(membership.settings_store, 'is_hydrated', lambda: False)
    with pytest.raises(HTTPException) as error:
        await MembershipChecker().status(PK)
    assert error.value.status_code == 503


@pytest.mark.anyio
@pytest.mark.parametrize('reply', ['EOSE', 'CLOSED', 'timeout'])
async def test_actual_query_adapter_requires_eose(monkeypatch, reply):
    import websockets
    from app.services import instance_membership as membership
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, payload): self.sub = json.loads(payload)[1]
        async def recv(self):
            if reply == 'timeout': await asyncio.sleep(10)
            return json.dumps([reply, self.sub, 'blocked'])
    def connect(uri, **kwargs):
        assert uri == 'ws://127.0.0.1:3052/relay'
        assert 'proxy' in kwargs and kwargs['proxy'] is None
        return Socket()
    monkeypatch.setattr(websockets, 'connect', connect)
    monkeypatch.setattr(membership, 'QUERY_TIMEOUT', .02)
    f = Fixture()
    f.checker.query = membership._query_profile
    if reply == 'EOSE':
        assert (await f.checker.status(PK))['reason'] == 'profile_missing'
    else:
        with pytest.raises(HTTPException) as error:
            await f.checker.status(PK)
        assert error.value.status_code == 503


@pytest.mark.anyio
async def test_explicit_refresh_bypasses_both_grant_and_denial_ttl():
    f = Fixture()
    assert (await f.checker.status(PK))['qualified']
    f.rows = [profile('', 101)]
    assert not (await f.checker.status(PK, force=True))['qualified']
    f.rows = [profile(timestamp=102)]
    assert (await f.checker.status(PK, force=True))['qualified']
    assert f.calls == 3


@pytest.mark.anyio
async def test_forced_refresh_supersedes_old_read_and_coalesces_waiters():
    f = Fixture()
    pending = []
    async def query(pk, port):
        future = asyncio.get_running_loop().create_future()
        pending.append(future)
        return await future
    f.checker.query = query
    old = asyncio.create_task(f.checker.status(PK))
    await asyncio.sleep(.01)
    fresh = asyncio.create_task(f.checker.status(PK, force=True))
    second = asyncio.create_task(f.checker.status(PK, force=True))
    await asyncio.sleep(.01)
    assert len(pending) == 2
    pending[1].set_result([profile('', 101)])
    assert not (await fresh)['qualified']
    assert not (await second)['qualified']
    pending[0].set_result([profile()])
    with pytest.raises(HTTPException) as error:
        await old
    assert error.value.status_code == 503
    assert not (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_failed_forced_refresh_does_not_reuse_cached_grant():
    f = Fixture()
    assert (await f.checker.status(PK))['pubkey'] == PK
    f.error = True
    for force in [True, False]:
        with pytest.raises(HTTPException) as error:
            await f.checker.status(PK, force=force)
        assert error.value.status_code == 503


@pytest.mark.anyio
async def test_hex_case_and_npub_are_one_account():
    from app.services.nostr.nostr_service import npub_of
    f = Fixture()
    f.config[0] = f'alice {PK.upper()}'
    assert (await f.checker.status(PK.upper()))['pubkey'] == PK
    assert (await f.checker.status(npub_of(PK)))['qualified']
    assert f.calls == 1


def external_fixture(local, remote):
    f = Fixture(local)
    f.config.append('wss://one.example/relay\nwss://two.example/relay\nwss://three.example/relay\nwss://four.example/relay')
    f.external_calls = []
    async def upstream(pk, relays, budget):
        f.external_calls.append((pk, relays))
        assert 0 < budget <= 4
        return remote
    f.checker.upstream_query = upstream
    return f


@pytest.mark.anyio
async def test_external_only_matching_profile_is_accepted_without_republish():
    f = external_fixture([], [profile()])
    assert (await f.checker.status(PK))['qualified']
    assert len(f.external_calls[0][1]) == 3
    assert f.rows == []


@pytest.mark.anyio
async def test_local_matching_normal_check_skips_external_latency():
    f = external_fixture([profile()], [])
    assert (await f.checker.status(PK))['qualified']
    assert f.external_calls == []


@pytest.mark.anyio
async def test_forced_upstream_newer_removal_overrides_old_local_match():
    f = external_fixture([profile()], [profile('', 101)])
    assert (await f.checker.status(PK))['qualified']
    assert not (await f.checker.status(PK, force=True))['qualified']


@pytest.mark.anyio
async def test_external_old_replay_and_forged_newer_profile_do_not_grant():
    forged = profile(timestamp=102)
    forged['sig'] = '00'*64
    f = external_fixture([profile('', 101)], [profile(), forged])
    assert not (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_configured_upstream_outage_is_unknown_not_missing_profile():
    f = external_fixture([], [])
    async def unavailable(*args): raise OSError('offline')
    f.checker.upstream_query = unavailable
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_complete_empty_upstream_leaves_local_latest_authoritative():
    f = external_fixture([profile('', 101)], [])
    assert (await f.checker.status(PK))['reason'] == 'profile_mismatch'


@pytest.mark.anyio
async def test_upstream_adapter_uses_proxy_aware_connect_and_keeps_completed_peer(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services import instance_membership as membership
    seen = []
    @asynccontextmanager
    async def connect(uri, budget):
        seen.append(uri)
        class Socket:
            async def send(self, payload): self.sub = json.loads(payload)[1]
            async def recv(self):
                if uri.endswith('slow'): await asyncio.sleep(10)
                return json.dumps(['EOSE', self.sub])
        yield Socket()
    monkeypatch.setattr(membership, '_connect_profile', connect)
    assert await membership._query_upstreams(PK, ['wss://configured/ok','wss://configured/slow'], .02) == []
    assert seen == ['wss://configured/ok','wss://configured/slow']


@pytest.mark.anyio
async def test_external_newer_profile_is_rechecked_when_local_remains_stale():
    f = external_fixture([profile()], [profile('', 101)])
    assert not (await f.checker.status(PK, force=True))['qualified']
    f.time = 6
    assert not (await f.checker.status(PK))['qualified']
    assert len(f.external_calls) == 2


@pytest.mark.anyio
async def test_registry_change_does_not_forget_observed_profile_watermark():
    f = Fixture([profile('', 101)])
    assert not (await f.checker.status(PK))['qualified']
    f.config[0] += f'\nsecond {PK}'
    f.rows = [profile()]
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_local_connection_failure_can_use_verified_configured_profile():
    f = external_fixture([], [profile()])
    f.error = True
    assert (await f.checker.status(PK))['qualified']


@pytest.mark.anyio
async def test_local_outage_and_empty_upstream_are_unknown():
    f = external_fixture([], [])
    f.error = True
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_local_outage_does_not_accept_older_upstream_grant():
    f = external_fixture([profile('', 101)], [profile()])
    assert not (await f.checker.status(PK))['qualified']
    f.time = 6
    f.error = True
    with pytest.raises(HTTPException) as error:
        await f.checker.status(PK)
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_profile_transport_gives_direct_fallback_time_within_deadline(monkeypatch):
    from app.services import instance_membership as membership
    import websockets
    seen=[]
    class Socket:
        async def close(self): pass
    async def proxy(uri, base, timeout, options):
        seen.append(timeout)
        raise TimeoutError('proxy unavailable')
    async def direct(uri, **options):
        seen.append(options)
        return Socket()
    monkeypatch.setattr(membership.nostr_service.relay, '_conn_kw', lambda uri,direct:{'proxy':'http://configured-proxy'})
    monkeypatch.setattr(membership, '_open_bounded_proxy', proxy)
    monkeypatch.setattr(websockets, 'connect', direct)
    async with membership._connect_profile('wss://configured.example', 3):
        pass
    assert seen[0]==1 and seen[1]['open_timeout']==1
    assert seen[1]['proxy'] is None


@pytest.mark.anyio
@pytest.mark.parametrize('cancel', [False, True])
async def test_bounded_http_proxy_closes_socket_on_timeout_or_cancellation(cancel):
    from app.services import instance_membership as membership
    from python_socks import ProxyTimeoutError
    connected, closed = asyncio.Event(), asyncio.Event()
    async def proxy(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            connected.set()
            assert await reader.read() == b''
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()
    server = await asyncio.start_server(proxy, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        task = asyncio.create_task(membership._open_bounded_proxy(
            'wss://profile.example', {'proxy':f'http://127.0.0.1:{port}'}, .05, {}))
        await asyncio.wait_for(connected.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            with pytest.raises(ProxyTimeoutError): await task
        await asyncio.wait_for(closed.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()
