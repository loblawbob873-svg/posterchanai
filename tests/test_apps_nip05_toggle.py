"""Admin can switch off the NIP-05 requirement for PosterChan apps ("make it a toggle that we can do in admin").

Through the REAL routes: with `apps_require_nip05` off, a signed-in account with no name on this node
opens Mail, News, Files-side apps, Web Search...; on (the default, and a blank row) it is refused as
before; a relay-blocked account is refused either way; the wallets stay members-only regardless; the
client's app list (/api/instance-welcome/access) follows the switch; and the real membership answer
(`status`, which grants AI/image/music/Blossom) never changes with it."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.routers import news, mail, git, torrent, office, media_center, websearch, monero_user_wallet, instance_welcome
from app.services import instance_membership as membership
from app.services import relay_blocklist
from app.services.nostr.event import build_event

MEMBER_SECRET = bytes.fromhex('01'.zfill(64))
MEMBER = build_event(MEMBER_SECRET, 0, json.dumps({'nip05': 'alice@example.test'}), created_at=100)['pubkey']
STRANGER = build_event(bytes.fromhex('02'.zfill(64)), 0, '{}', created_at=100)['pubkey']
APPS = ['/api/news/sources', '/api/mail/accounts', '/api/torrent/catalog', '/api/git/status',
        '/client/office/blank/text', '/api/media-center', '/api/websearch/search?q=hello']
WALLET = '/api/test-user-wallet/balance'


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture
def setup(monkeypatch):
    state = {'switch': None, 'blocked': set(), 'who': STRANGER}
    async def query(pk, port): return []
    checker = membership.MembershipChecker(query=query, configuration=lambda: (f'alice {MEMBER}', 'example.test', '', '3052'),
                                           clock=lambda: 0)
    monkeypatch.setattr(membership, '_checker', checker)
    # ONE settings stub: media_center.settings_store IS membership.settings_store (the same module).
    monkeypatch.setattr(membership.settings_store, 'is_hydrated', lambda: True)
    monkeypatch.setattr(membership.settings_store, 'get',
                        lambda k, d=None: state['switch'] if k == 'apps_require_nip05' else d)
    monkeypatch.setattr(relay_blocklist, 'is_blocked', lambda pk: pk in state['blocked'])
    user = SimpleNamespace(id=7, nostr_npub='', is_admin=False, can_media=True, can_torrent=True, news_sources='')
    def current():
        user.nostr_npub = state['who']
        return user
    app = FastAPI()
    for module in [news, mail, git, torrent, office, media_center, websearch]:
        app.include_router(module.router)
        if hasattr(module, 'get_current_user'):
            app.dependency_overrides[module.get_current_user] = current
        if hasattr(module, 'get_db'):
            app.dependency_overrides[module.get_db] = lambda: None
    app.dependency_overrides[media_center.media_user_optional] = current
    monkeypatch.setattr(torrent, 'get_current_user', lambda *a: current())
    monkeypatch.setattr(torrent.lb_auth, 'is_internal', lambda request: False)
    monkeypatch.setattr(mail, 'get_user_mail_accounts', lambda *a: [])
    async def empty(*a): return []
    monkeypatch.setattr(torrent, 'scrape_torrents', empty)
    monkeypatch.setattr(git, '_enabled', lambda: True)
    monkeypatch.setattr(git.git_proxy, 'proxy_enabled', lambda: True)
    monkeypatch.setattr(media_center.media, 'libraries', empty)
    class SearchStub:
        async def search_page(self, *a, **k): return {'results': []}
    monkeypatch.setattr(websearch, 'get_search_service', lambda db: SearchStub())
    app.include_router(monero_user_wallet.router, prefix='/api/test-user-wallet')
    app.dependency_overrides[monero_user_wallet.auth.get_current_user] = current
    async def balance(pk): return {'balance': '12'}
    monkeypatch.setattr(monero_user_wallet.user_wallets, 'balance', balance)
    async def limits(): return {'viewer_kbps': 10000}
    monkeypatch.setattr(media_center.media, 'limits', limits)
    app.include_router(instance_welcome.router)
    app.dependency_overrides[instance_welcome.get_current_user] = current
    return app, state


async def _get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        return await c.get(path)


@pytest.mark.anyio
@pytest.mark.parametrize('path', APPS)
async def test_switched_off_a_signed_in_account_without_a_name_opens_the_apps(setup, path):
    app, state = setup
    state['switch'] = 'false'
    r = await _get(app, path)
    assert r.status_code == 200, r.text


@pytest.mark.anyio
@pytest.mark.parametrize('switch', [None, '', 'true'])
@pytest.mark.parametrize('path', APPS)
async def test_on_by_default_and_when_blank_the_same_account_is_refused(setup, path, switch):
    app, state = setup
    state['switch'] = switch
    assert (await _get(app, path)).status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize('path', APPS)
async def test_a_relay_blocked_account_is_refused_even_switched_off(setup, path):
    app, state = setup
    state['switch'] = 'false'
    state['blocked'].add(STRANGER)
    assert (await _get(app, path)).status_code == 403


@pytest.mark.anyio
async def test_the_wallets_stay_members_only_whatever_the_switch(setup):
    app, state = setup
    state['switch'] = 'false'
    assert (await _get(app, WALLET)).status_code == 403


@pytest.mark.anyio
async def test_the_clients_app_list_follows_the_switch(setup):
    app, state = setup
    r = await _get(app, '/api/instance-welcome/access')
    assert r.status_code == 200 and r.json()['qualified'] is False
    state['switch'] = 'false'
    r = await _get(app, '/api/instance-welcome/access')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['qualified'] is True and body['pubkey'] == STRANGER


@pytest.mark.anyio
async def test_real_membership_is_unchanged_by_the_switch(setup):
    """status() also grants AI/image/music/Blossom (nip05_access): it must not open with the apps."""
    app, state = setup
    state['switch'] = 'false'
    assert (await membership.status(STRANGER))['qualified'] is False
