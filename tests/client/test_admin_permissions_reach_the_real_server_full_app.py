"""User management from the client, end to end: the shipped Permissions sheet against the node's REAL client router.

tests/client/test_profile_bch_tip_and_permissions_save_full_app.py presses the same Save against fixture endpoints,
and tests/test_every_admin_route_refuses_non_admins.py signs proofs in Python. Neither crosses the seam: does the
proof the SHIPPED client signs pass the SERVER's verifier, and does the server -- not the hidden menu item -- keep a
member out? Here the profile ⋯ → Permissions → '#caps-save' goes through app/routers/client.py itself
(`_verify_admin_auth`, `_find_or_create_user`, the whitelist edit), so:

  an_admin_grants_permissions_and_the_account_changes
      The admin ticks AI access, Live streaming, Blossom uploads and the Video cap and presses Save: "permissions
      saved", and the ACCOUNT the server holds now has can_ai / can_stream / can_video and the Blossom whitelist
      names them. Reopening the sheet shows the stored state (read back through the same routes).
  a_member_who_forces_the_sheet_open_is_refused_by_the_server
      A member whose client is made to BELIEVE it is an admin (its /client/config claims so -- the hidden button,
      revealed) presses the same Save with valid signatures of its own: the server answers 403 to every change,
      the sheet says "some changes failed", and the account is exactly as it was.

The account table, the settings value, the DM and the account sync are in-memory fixtures (they are relay
documents on a node); everything between the browser and them is the shipped code. Harness:
tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/profile.js=<copy>".
"""
import json
from types import SimpleNamespace

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FIRST_FILL = 230
SCENARIOS = {}
COST = {}
CAPS = ('can_image', 'can_music', 'can_video', 'can_torrent', 'can_media')


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _npub(pk):
    from app.services.nostr import nostr_service
    return nostr_service.npub_of(pk)


def _account(uid, pk, admin=False):
    return SimpleNamespace(id=uid, username='u%d' % uid, email=None, nostr_npub=_npub(pk), nostr_nsec=None,
                           is_admin=admin, can_ai=False, can_stream=False, can_blossom=False, access_revoked=False,
                           can_image=True, can_music=False, can_video=False, can_torrent=False, can_media=False,
                           token_version=0, is_blocked=False)


class Ctx:
    """Per scenario (base fill b): b = the admin, b+1 = the person being managed, b+2 = a member (not an admin)."""

    def __init__(self, n):
        from app.routers import client
        from app.services import access_notify_service, blossom_service, settings_store, user_table, users_store
        self.mp = mp = pytest.MonkeyPatch()
        self.accounts, self.whitelist, self.notified = {}, set(), []
        users = {}
        for i, name in enumerate(SCENARIOS):
            b = FIRST_FILL + 4 * i
            admin, target, member = (_account(b + k, R.pubkey(b + k), admin=(k == 0)) for k in range(3))
            for a in (admin, target, member):
                self.accounts[a.nostr_npub] = a
                users[R.pubkey(a.id)] = a
        self.saved = []

        async def aby_npub(db, npub):
            return self.accounts.get(npub)

        async def asave(db, u):
            self.saved.append((u.nostr_npub, {c: getattr(u, c) for c in ('can_ai', 'can_stream') + CAPS}))
            self.accounts[u.nostr_npub] = u
        mp.setattr(user_table, 'aby_npub', aby_npub)
        mp.setattr(user_table, 'asave', asave)

        async def nothing(*a, **k):
            return None

        async def notify(db, who, what):
            self.notified.append((getattr(who, 'nostr_npub', who), what))
        mp.setattr(access_notify_service, 'notify_access_granted', notify)
        mp.setattr(users_store, 'sync_user', nothing)

        # The shared Blossom whitelist is a relay-backed setting on a node; here a value held in memory.
        get = settings_store.get
        mp.setattr(settings_store, 'get', lambda k, *a, **kw: (
            '\n'.join(sorted(_npub(h) for h in self.whitelist)) if k == 'blossom_whitelist' else get(k, *a, **kw)))

        def put(k, v):
            assert k == 'blossom_whitelist', ('an unexpected setting was written', k)
            from app.services.nostr import nostr_service
            self.whitelist = {nostr_service.to_pubkey_hex(t) for t in v.split() if t.strip()}
        mp.setattr(settings_store, 'put', put)
        mp.setattr(settings_store, 'hydrate_from_db', lambda db: None)
        mp.setattr(settings_store, 'is_hydrated', lambda: True)

        async def allowed(db, pk):
            return pk in self.whitelist
        mp.setattr(blossom_service, 'is_pubkey_allowed_async', allowed)

        # What the server's verifier was shown and what it decided: (signer, action, admitted).
        self.verdicts = []
        real_verify = client._verify_admin_auth

        async def verify(db, auth_b64, target_hex, purpose=None):
            who = await real_verify(db, auth_b64, target_hex, purpose)
            try:
                import base64 as _b
                signer = json.loads(_b.b64decode(auth_b64)).get('pubkey')
            except Exception:
                signer = None
            self.verdicts.append((signer, purpose, bool(who)))
            return who
        mp.setattr(client, '_verify_admin_auth', verify)

        self.admins = [_npub(R.pubkey(FIRST_FILL + 4 * i)) for i in range(len(SCENARIOS))]
        self.config_before = dict(R.NODE['config'])
        R.NODE['config']['admin_npubs'] = list(self.admins)
        self.api = F.Routers(mp, [(client.router, '')], users,
                             paths=['/client/user-caps', '/client/ai-access', '/client/stream-access',
                                    '/client/blossom-access', '/client/admin-nip05'])

    def stop(self):
        self.api.stop()
        R.NODE['config'].clear()
        R.NODE['config'].update(self.config_before)
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


def _state(S, pk):
    a = S.ctx.accounts[_npub(pk)]
    return {'ai': a.can_ai, 'stream': a.can_stream, 'video': a.can_video, 'blossom': pk in S.ctx.whitelist}


ITEM = "[...document.querySelectorAll('.menu-pop button, .menu-pop [role=menuitem]')].find(x=>/Permissions/.test(x.textContent))"


async def open_permissions(d, pk):
    await d.b.js("__PC.openProfile(%s);true" % json.dumps(pk))
    await F.wait(d, "!!document.querySelector('#prof-menu')", 30, 'the profile ⋯ button')
    await d.b.js("document.querySelector('#prof-menu').click();true")
    await F.wait(d, "!!" + ITEM, 15, 'Permissions in the ⋯ menu')
    await d.b.js(ITEM + ".click();true")
    await F.wait(d, "!!document.querySelector('#caps-save')", 30, 'the Permissions sheet')


CHECKED = ("[document.querySelector('#perm-ai').checked,document.querySelector('#perm-stream').checked,"
           "document.querySelector('#perm-blossom').checked,document.querySelector('[data-cap=can_video]').checked]")
TOGGLE = ("document.querySelector('#perm-ai').click();document.querySelector('#perm-stream').click();"
          "document.querySelector('#perm-blossom').click();document.querySelector('[data-cap=can_video]').click();true")


def toast(text):
    return "(window.__toastLog||[]).some(t=>String(t).includes(%s))" % json.dumps(text)


@scenario
async def an_admin_grants_permissions_and_the_account_changes(S):
    target = S.pk(1)
    assert _state(S, target) == {'ai': False, 'stream': False, 'video': False, 'blossom': False}
    d = await S.device(0)
    await open_permissions(d, target)
    assert await d.b.js(CHECKED) == [False, False, False, False]
    await d.b.js(TOGGLE)
    await d.b.js("document.querySelector('#caps-save').click();true")
    await F.wait(d, toast('permissions saved'), 30, '"permissions saved"')
    assert _state(S, target) == {'ai': True, 'stream': True, 'video': True, 'blossom': True}, _state(S, target)
    assert (_npub(target), 'ai') in S.ctx.notified, 'the person was not told AI access was granted'
    assert {(v[1], v[2]) for v in S.ctx.verdicts if v[0] == S.pk(0)} == {
        ('ai-access', True), ('stream-access', True), ('blossom', True), ('user-caps', True)}
    # Read back through the same routes: the sheet opens on what the server now holds.
    await F.wait(d, "!document.querySelector('#caps-save')", 15, 'the sheet closed')
    await open_permissions(d, target)
    assert await d.b.js(CHECKED) == [True, True, True, True]


@CHROME
def test_an_admin_grants_permissions_and_the_account_changes(ran):
    ran('an_admin_grants_permissions_and_the_account_changes')


# The member's client is told it is an admin -- the hidden button, revealed. Only the server stands in the way.
CLAIMS_ADMIN = r'''
(()=>{const f=window.fetch;window.fetch=async function(u,o){const r=await f.apply(this,arguments);
  try{if(String(u).includes('/client/config')&&r.ok){const d=await r.clone().json();const me=window.__pcMeNpub;
    return new Response(JSON.stringify({...d,admin_npubs:[...(d.admin_npubs||[]),me]}),{status:200,headers:{'Content-Type':'application/json'}});}}catch(_){}
  return r;};})();
'''


@scenario
async def a_member_who_forces_the_sheet_open_is_refused_by_the_server(S):
    target = S.pk(1)
    before = _state(S, target)
    pre = "window.__pcMeNpub=%s;" % json.dumps(_npub(S.pk(2))) + CLAIMS_ADMIN
    d = await S.device(2, pre_init=pre)
    assert await d.b.js("__PC.isAdmin()"), 'the fixture did not make the client believe it is an admin'
    await open_permissions(d, target)
    await d.b.js(TOGGLE)
    await d.b.js("document.querySelector('#caps-save').click();true")
    await F.wait(d, toast('some changes failed'), 30, '"some changes failed"')
    assert not await d.b.js(toast('permissions saved'))
    assert _state(S, target) == before, ('a member changed another account', _state(S, target))
    mine = [v for v in S.ctx.verdicts if v[0] == S.pk(2)]
    # Every change reached the server's verifier signed by the member, and every one was refused there.
    assert {v[1] for v in mine} == {'ai-access', 'stream-access', 'blossom', 'user-caps'}, mine
    assert not any(v[2] for v in mine), mine
    assert not [x for x in S.ctx.saved if x[0] == _npub(target)], 'the account was written'


@CHROME
def test_a_member_who_forces_the_sheet_open_is_refused_by_the_server(ran):
    ran('a_member_who_forces_the_sheet_open_is_refused_by_the_server')
