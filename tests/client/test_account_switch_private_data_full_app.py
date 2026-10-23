"""TWO ACCOUNTS ON ONE DEVICE: what belongs to account A is never shown, used or published under account B.

Every switch here is the shipped one -- the account card -> "＋ Add an account" -> the login screen, then the card ->
the other account's row (or __PC.accountAct, the same call PosterChanOS's account window makes) -- on ONE Chrome
profile, against a real node (realnode_harness: the shipped relay; the node's own /client/drafts route mounted
in-process over it, so the drafts a device syncs are read back here from the relay, decrypted with the account's
storage key exactly as the route stores them).

  signer    A's paired apps are not B's: B's Signer screen lists none of them, an app paired with A is never
            answered with B's key, and switching back gives A its pairing (and the app its signatures) again.
  drafts    a post typed under A and left in the composer when the account is switched is A's draft: it is not in
            B's Drafts, not in B's synced drafts on the relay, never published under B -- and it is still A's when A
            comes back.
  (contacts: tests/client/test_contacts_flows_full_app.py; the meme project: test_meme_screen_flows_full_app.py.)

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/<file>=...".
"""
import asyncio
import hashlib
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client import test_signer_flows_full_app as SG

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('signer', 'drafts')
FIRST_FILL = 160            # 160 .. (account keys are one byte; each module has its own node)
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def storage_sk(u):
    return hashlib.sha256(b'pc-test-storage:' + u.nostr_npub.encode()).digest()


class Ctx:
    def __init__(self, n):
        from app.routers import client as client_router
        from app.services import nostr_store
        from app.services.nostr import nostr_service
        self.n = n
        self.mp = mp = pytest.MonkeyPatch()
        self.users = F.members(FIRST_FILL, SCENARIOS)
        by_npub = {nostr_service.npub_of(pk): u for pk, u in self.users.items()}

        async def aby_npub(_db, npub):
            return by_npub.get(npub)
        mp.setattr(client_router.user_table, 'aby_npub', aby_npub)
        mp.setattr(nostr_store, 'user_storage_seckey', lambda _db, u: storage_sk(u), raising=False)
        real_setting = client_router._setting
        mp.setattr(client_router, '_setting', lambda db, key, default='': (
            str(n.home.port) if key == 'nostr_relay_port' else real_setting(db, key, default)))
        self.api = F.Routers(mp, [(client_router.router, '')], self.users, paths=['/client/drafts'])

    def synced_drafts(self, pk):
        """The account's drafts document on the relay, as /client/drafts stores it."""
        from app.services import nostr_store
        doc = asyncio.run_coroutine_threadsafe(
            nostr_store.get_doc(self.n.home.port, 'pcai:drafts', seckey=storage_sk(self.users[pk]), strict=True),
            self.api.loop).result(30)
        return [d.get('text', '') for d in (doc or {}).get('drafts', []) if isinstance(d, dict) and not d.get('del')]

    def stop(self):
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


DRAFTS = "[...document.querySelectorAll('#feed .draft-card .draft-body')].map(e=>e.textContent)"


async def drafts_view(d):
    await d.b.js("__PC.switchView('drafts');true")
    await F.wait(d, "__PC.isView && __PC.isView('drafts') && !!document.querySelector('#feed')", 15, 'Drafts')


@scenario
async def a_pairing_made_under_one_account_is_never_answered_by_another(S):
    sig, app = await SG.paired(S)
    await F.post_note(app, 'signed while A is here %d' % S.base)
    await F.add_account(sig, S.fill(1))
    await SG.open_signer(sig)
    await F.wait(sig, "/No apps are signed in/.test(document.querySelector('#signer-apps').innerText)", 15,
                 "B's own (empty) pairings")
    assert await sig.b.js(SG.APPS) == []
    text = 'asked while B is signed in %d' % S.base
    await SG.compose(app, text)
    await F.wait(app, "(window.__toastLog||[]).some(t=>/waiting for your signer/.test(t))", 10, 'the app waiting')
    loop = asyncio.get_running_loop()
    end = loop.time() + 4
    while loop.time() < end:
        assert not [e for e in S.n.home.query({'kinds': [1], 'authors': [S.pk(0), S.pk(1)]}) if e['content'] == text], \
            "signed while the device was B's"
        await asyncio.sleep(.5)
    # Back to A: the pairing is A's again, and A's key answers it.
    await F.switch_account(sig, S.fill(0))
    await SG.open_signer(sig)
    await F.wait(sig, "%s.length===1" % SG.APPS, 15, "A's pairing back")
    ev = await F.post_note(app, 'signed after A came back %d' % S.base)
    assert ev['pubkey'] == S.pk(0)


@CHROME
def test_a_pairing_made_under_one_account_is_never_answered_by_another(ran):
    ran('a_pairing_made_under_one_account_is_never_answered_by_another')


@scenario
async def a_draft_left_in_the_composer_at_a_switch_stays_with_its_account(S):
    ctx = S.ctx
    d = await S.device(0)
    await F.add_account(d, S.fill(1))
    await F.switch_account(d, S.fill(0))                      # A, with B remembered on this device
    text = 'half a thought from A %d' % S.base
    await F.view(d, 'home', "!!document.querySelector('#tl-fab')")
    await d.b.js("document.querySelector('#tl-fab').click();true")
    await F.wait(d, "!!document.querySelector('#cmp')", 20, 'the composer')
    await d.b.js("(t=>{const c=document.querySelector('#cmp');c.focus();c.value=t;c.dispatchEvent(new Event('input',{bubbles:true}));return true})(%s)"
                 % json.dumps(text))
    # The switch happens with the text still in the composer (the account popup PosterChanOS uses).
    await d.b.js("window.__pcBeforeSwitch=true;__PC.accountAct(%s);true" % json.dumps(S.pk(1)))
    await F.wait(d, "!window.__pcBeforeSwitch && !!window.__PC && !!__PC.me() && __PC.me().pubkey===%s && Relay.status==='ok'"
                 % json.dumps(S.pk(1)), 60, 'signed in as B')
    await drafts_view(d)
    await asyncio.sleep(1.5)                                  # B's own drafts pull has had its chance to land
    assert not [t for t in await d.b.js(DRAFTS) if 'half a thought' in t], "A's draft under B"
    assert 'half a thought' not in await d.b.js("document.querySelector('#feed').innerText")
    assert not [t for t in ctx.synced_drafts(S.pk(1)) if 'half a thought' in t], "A's draft synced as B's"
    assert not [e for e in S.n.home.query({'kinds': [1], 'authors': [S.pk(1)]}) if 'half a thought' in e['content']]
    # A again: the draft is A's.
    await F.switch_account(d, S.fill(0))
    await drafts_view(d)
    await F.wait(d, "%s.some(t=>t.includes(%s))" % (DRAFTS, json.dumps(text)), 15, "A's draft back under A")
    await F.until(lambda: [t for t in ctx.synced_drafts(S.pk(0)) if t == text], 20, "A's draft synced as A's", d)


@CHROME
def test_a_draft_left_in_the_composer_at_a_switch_stays_with_its_account(ran):
    ran('a_draft_left_in_the_composer_at_a_switch_stays_with_its_account')
