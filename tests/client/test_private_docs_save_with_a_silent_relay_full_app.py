"""Every private document SAVES with a relay in your own list that never answers -- and comes back.

Operator: "you always screw up hydration and setting persistence". The member who reported the desktop
(de1039bdf) has "use my own relays" on and two relays in his list that never answer a private read, so
every `Relay.query()` of his own documents came back `complete:false` for ever. Each of these modules
gated its write on `complete` (or, the other way round, on ANY resolved read), so for him a client pref,
a vault, a budget, a VM host, a community folder, a go-live template, the auto-mute switch, the read
markers, a music-share answer and the DM cache key were each blocked, held or lost -- while this node's
relay, which holds every one of them, had answered in milliseconds.

Each scenario runs the SHIPPED web client in real Chrome (tests/client/wallpaper_harness.py: a REAL
relay as this node's, a SilentRelay in the pool that never answers a kind-30078 read, a real instance)
and asserts what a person gets:

  * persist  device A (own relays: home + the silent one) makes the change through the app; the HOME
             relay ends up holding it; device B -- a FRESH Chrome profile, same account, same pool --
             loads and SHOWS it;
  * no read  the home relay is silent for that document too (nobody answers): nothing is published,
             and what device A did is still on its screen (kept on the device, not thrown away).

The rule half -- every module that writes such a document, discovered from the source and driven
under node -- is tests/client/test_private_doc_write_gates_hear_the_home_relay.py.
"""
import asyncio
import os
import json
from pathlib import Path

import pytest

from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')
OTHER_PK_FILL = 7          # a second account: the author of a mention, a sender to stop, a VM host
HOST = 'ef' * 32


# EVERY SCENARIO IS ITS OWN ACCOUNT, AND THEY RUN AT THE SAME TIME. Nearly all of this file's time is the CLIENT
# waiting on a relay that never answers (its own 6-8 s read timeouts, retried) -- ~360 s run one after another. Each
# scenario signs in with its own key, holds only ITS account's reads (hold_docs (pubkey, d) pairs), counts only ITS
# account's publishes, and waits for only ITS reads to be given up (gave_up(author=)), so scenarios share the relay
# and the instance without seeing each other; LANES of them run concurrently, each still one device or two.
LANES = 3
# Measured seconds when run alone: the longest start first, so the lanes finish together.
COST = {'a_community_folder_saves_and_shows_on_a_fresh_device': 36, 'client_prefs_save_and_reach_a_fresh_device': 28,
        'a_budget_bill_saves_and_reaches_a_fresh_device': 28, 'client_prefs_publish_nothing_when_nobody_answers': 25,
        'the_dm_cache_key_is_made_once_and_a_fresh_device_reads_with_it': 23,
        'read_markers_publish_nothing_when_nobody_answers': 22, 'notifications_read_here_are_read_on_a_fresh_device': 21,
        'a_budget_publishes_nothing_when_nobody_answers': 21, 'no_dm_cache_key_is_minted_when_nobody_answers': 18}
SCENARIOS = {}
FIRST_FILL = 40            # account keys 40, 41, ... -- never 1 (other suites' account) nor OTHER_PK_FILL


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


class Scenario:
    """One scenario's account: `await S.device()` brings up a device signed in to it (the first is device A, each
    later one a FRESH Chrome profile); docs/pubs/hold/quiet_for are this account's alone."""

    def __init__(self, name, fill):
        self.name, self.fill, self.pk = name, fill, _pubkey(fill)
        self.open = []

    async def device(self):
        d = H.Device(sk_fill=self.fill, own_relays=True)
        b = await d.__aenter__()
        self.open.append(d)
        assert await b.js('__PC.me().pubkey') == self.pk
        # Every device HAS FOCUS, as a window somebody is reading does. Notifications are marked read only while the
        # page is focused, and headless Chrome hands focus to one window at most with scenarios running three at a
        # time -- two read-marker scenarios failed in CI on it ("focus: False", desktop builds of 2026-10-23).
        await b.call('Emulation.setFocusEmulationEnabled', {'enabled': True})
        return b

    async def close(self):
        for d in reversed(self.open):
            await d.__aexit__()

    def hold(self, *tags):
        """Nobody answers THIS account's reads of these documents (before any device boots)."""
        H.STATE['hold_docs'] |= {(self.pk, t) for t in tags}

    def docs(self, d):
        return [e for e in H.docs(d) if e.get('pubkey') == self.pk]

    def pubs(self, d):
        return [e for e in H.pubs(d) if e.get('pubkey') == self.pk]

    async def quiet_for(self, seconds):
        """Long enough for a wrongly-armed writer to publish: until the client gave up on this account's silent reads
        and a margin after it, never more than `seconds` (H.gave_up)."""
        await H.gave_up(seconds, author=self.pk)

    async def until(self, b, fn, seconds, what):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            got = fn()
            if got:
                return got
            await asyncio.sleep(.3)
        # The read-marker's own state, so a CI-only failure says whether the mark was made and is waiting for a relay
        # that "answered" (pending:true) or was never made at all -- it failed only on GitHub's runners (2026-10-17).
        seen = await b.js("(()=>{try{return localStorage.getItem('pc_seen:'+((__PC.me&&__PC.me())||{}).pubkey)}catch(e){return String(e)}})()")
        raise AssertionError({'waiting for': what, 'toasts': await b.js('__toasts'), 'errors': await b.js('__errors'),
                              'published 30078': [H._d(e) for e in H.STATE['pubs'] if e.get('pubkey') == self.pk],
                              'pc_seen': seen, 'visibility': await b.js('document.visibilityState'),
                              'focus': await b.js('document.hasFocus()')})


@pytest.fixture(scope='module')
def ran(request):
    """Runs every SELECTED scenario of this module (LANES at a time) once, and hands each test its own outcome."""
    return run_scenarios(request, SCENARIOS, COST, FIRST_FILL)


def run_scenarios(request, scenarios, cost, first_fill, lanes=LANES):
    """The runner, shared with the sibling cross-device file: each selected scenario on its own account
    (`first_fill`, `first_fill`+1, ... in the order `scenarios` were declared), the longest first, `lanes` at a time."""
    wanted = [i.name[len('test_'):] for i in request.session.items
              if getattr(i, 'module', None) is request.module and i.name.startswith('test_')]
    missing = [w for w in wanted if w not in scenarios]
    assert not missing, ('a test with no scenario', missing)
    H.start()
    H.OVERRIDES.clear()
    # The fail-first proof, the wallpaper test's convention: PC_WALLPAPER_OVERRIDE=
    # "/static/js/client/vault.js=/tmp/old/vault.js,…" serves an older copy of a shipped file.
    for pair in filter(None, os.environ.get('PC_WALLPAPER_OVERRIDE', '').split(',')):
        path, _, src = pair.partition('=')
        H.OVERRIDES[path] = src
    fills = {name: first_fill + i for i, name in enumerate(scenarios)}
    queue = sorted(wanted, key=lambda n: -cost.get(n, 15))
    outcome = {}

    async def lane():
        while queue:
            name = queue.pop(0)
            S = Scenario(name, fills[name])
            try:
                await scenarios[name](S)
                outcome[name] = None
            except BaseException as e:          # noqa: B036 -- kept for that scenario's own test to raise
                outcome[name] = e
            finally:
                try:
                    await S.close()
                except Exception:
                    pass

    async def main():
        await asyncio.gather(*(lane() for _ in range(lanes)))
    asyncio.run(main())

    def check(name):
        assert name in outcome, ('scenario never ran', name)
        if outcome[name] is not None:
            raise outcome[name]
    return check


def no_errors(errors):
    return [e for e in (errors or []) if 'ResizeObserver' not in e]


# ------------------------------------------------------------------------------- client prefs

async def open_settings(b):
    await b.js("__PC.switchView('settings')")
    await H.wait(b, "!!document.getElementById('set-no-images')", 40)


async def toggle(b, el_id):
    return await b.js("(()=>{const x=document.getElementById(%s);x.checked=!x.checked;"
                      "x.dispatchEvent(new Event('change',{bubbles:true}));return x.checked})()" % json.dumps(el_id))


def _prefs_with_data_saver(S):
    return [e for e in S.docs('pcai:client-prefs') if json.loads(e['content'] or '{}').get('noImages') is True]


@scenario
async def client_prefs_save_and_reach_a_fresh_device(S):
    a = await S.device()
    await open_settings(a)
    assert await toggle(a, 'set-no-images') is True
    await S.until(a, lambda: _prefs_with_data_saver(S), 90, 'the data saver in pcai:client-prefs on the home relay')
    b = await S.device()
    await H.wait(b, "ClientSettings.get('noImages')===true", 60)
    await open_settings(b)
    assert await b.js("document.getElementById('set-no-images').checked") is True
    assert not no_errors(await b.js('__errors'))


@CHROME
def test_client_prefs_save_and_reach_a_fresh_device(ran):
    ran('client_prefs_save_and_reach_a_fresh_device')


@scenario
async def client_prefs_publish_nothing_when_nobody_answers(S):
    S.hold('pcai:client-prefs')
    a = await S.device()
    await open_settings(a)
    assert await toggle(a, 'set-no-images') is True
    await S.quiet_for(30)
    assert S.pubs('pcai:client-prefs') == [], 'published prefs over a read nobody answered'
    # kept on this device, and queued for the next read that is answered
    assert await a.js("ClientSettings.get('noImages')===true && document.getElementById('set-no-images').checked")
    assert await a.js("JSON.parse(localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey)||'{}').noImages===true")


@CHROME
def test_client_prefs_publish_nothing_when_nobody_answers(ran):
    ran('client_prefs_publish_nothing_when_nobody_answers')


# ------------------------------------------------------------------------------- budget

async def open_budget(b, seconds=60):
    await b.js("__PC.switchView('budget')")
    await H.wait(b, "!!document.querySelector('#bg-n') || !!document.querySelector('.bg-err')", seconds)


@scenario
async def a_budget_bill_saves_and_reaches_a_fresh_device(S):
    a = await S.device()
    import time
    t0 = time.monotonic()
    await open_budget(a)
    took = time.monotonic() - t0
    assert await a.js("!!document.querySelector('#bg-n')"), await a.js("document.querySelector('#feed').innerText")
    # "No budget yet" answered by this node's relay is an answer: one read, not three, even with a silent relay in
    # the list (it took ~19.5s before 2026-10-22).
    assert took < 10, ('Budget kept re-asking after the home relay answered', round(took, 1))
    await a.js("(()=>{document.querySelector('#bg-n').value='Electric company';"
               "document.querySelector('#bg-a').value='42.50';document.querySelector('#bg-save').click();return true})()")
    await S.until(a, lambda: S.docs('pcai:budget'), 60, 'pcai:budget on the home relay')
    b = await S.device()
    await open_budget(b)
    await H.wait(b, "(document.querySelector('.bg-wrap')||{innerText:''}).innerText.includes('Electric company')", 60)


@CHROME
def test_a_budget_bill_saves_and_reaches_a_fresh_device(ran):
    ran('a_budget_bill_saves_and_reaches_a_fresh_device')


@scenario
async def a_budget_publishes_nothing_when_nobody_answers(S):
    S.hold('pcai:budget')
    a = await S.device()
    await open_budget(a, 90)
    # It says so -- never a blank budget whose first save would replace the real one.
    assert await a.js("!!document.querySelector('.bg-err') && !document.querySelector('#bg-n')")
    assert S.pubs('pcai:budget') == []


@CHROME
def test_a_budget_publishes_nothing_when_nobody_answers(ran):
    ran('a_budget_publishes_nothing_when_nobody_answers')


# ------------------------------------------------------------------------------- vault

MEMBER = "PCInstanceAccess.accept({pubkey:__PC.me().pubkey,qualified:true},__PC.me().pubkey);true"


async def open_vault(b, seconds=60):
    await b.js(MEMBER)
    await b.js("__PC.switchView('vault')")
    await H.wait(b, "!!document.querySelector('.pv-new') || !!document.querySelector('.pv-locked')", seconds)


@scenario
async def a_vault_is_created_saved_and_opens_on_a_fresh_device(S):
    a = await S.device()
    await open_vault(a)
    assert await a.js("!!document.querySelector('.pv-new')"), await a.js("document.querySelector('#feed').innerText")
    await a.js("document.querySelector('.pv-new').click();true")
    await H.wait(a, "!!document.querySelector('.pv-title')", 20)
    await a.js("(()=>{const t=document.querySelector('.pv-title');t.value='Bank of Testing';"
               "t.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
    await S.until(a, lambda: S.docs('pcai:pwkey') and S.docs('pcai:pw:'), 60, 'the vault key and the item')
    b = await S.device()
    await open_vault(b)
    await H.wait(b, "[...document.querySelectorAll('.pv-item')].some(x=>x.textContent.includes('Bank of Testing'))", 60)
    assert len(S.pubs('pcai:pwkey')) == 1, 'the fresh device minted a second vault key'


@CHROME
def test_a_vault_is_created_saved_and_opens_on_a_fresh_device(ran):
    ran('a_vault_is_created_saved_and_opens_on_a_fresh_device')


@scenario
async def a_vault_is_not_created_when_nobody_answers(S):
    S.hold('pcai:pwkey')
    a = await S.device()
    await open_vault(a, 90)
    assert await a.js("!!document.querySelector('.pv-locked')"), await a.js("document.querySelector('#feed').innerText")
    assert S.pubs('pcai:pwkey') == []


@CHROME
def test_a_vault_is_not_created_when_nobody_answers(ran):
    ran('a_vault_is_not_created_when_nobody_answers')


# ------------------------------------------------------------------------------- DM cache key

PUT_SEALED = r"""(async()=>{const c=__PC.deviceCache('pc-gate-test');await c.put('probe',{v:42});
  const db=await new Promise((res,rej)=>{const r=indexedDB.open('pc-gate-test',1);r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});
  const k=__PC.me().pubkey.slice(0,16)+':probe';
  const rec=await new Promise(res=>{const q=db.transaction('r','readonly').objectStore('r').get(k);q.onsuccess=()=>res(q.result);});
  return rec?{iv:Array.from(rec.iv),ct:Array.from(rec.ct)}:null})()"""
OPEN_SEALED = r"""(async rec=>{const c=__PC.deviceCache('pc-gate-test');await c.get('nothing');
  const db=await new Promise((res,rej)=>{const r=indexedDB.open('pc-gate-test',1);
    r.onupgradeneeded=()=>{try{r.result.createObjectStore('r')}catch(_){}};r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});
  const k=__PC.me().pubkey.slice(0,16)+':probe';
  await new Promise(res=>{const q=db.transaction('r','readwrite').objectStore('r').put(rec,k);q.onsuccess=()=>res();});
  return await c.get('probe')})"""


@scenario
async def the_dm_cache_key_is_made_once_and_a_fresh_device_reads_with_it(S):
    a = await S.device()
    await a.js("__PC.switchView('messages');true")
    await S.until(a, lambda: S.docs('pcai:dmkey'), 60, 'pcai:dmkey on the home relay')
    sealed = None
    for _ in range(60):
        sealed = await a.js(PUT_SEALED)
        if sealed:
            break
        await asyncio.sleep(.5)
    assert sealed, 'device A could not seal with its DM cache key'
    b = await S.device()
    await b.js("__PC.switchView('messages');true")
    got = None
    for _ in range(60):
        got = await b.js(OPEN_SEALED + '(%s)' % json.dumps(sealed))
        if got:
            break
        await asyncio.sleep(.5)
    assert got == {'v': 42}, 'the fresh device does not hold the account\'s DM cache key'
    assert len(S.pubs('pcai:dmkey')) == 1, 'a second DM cache key was minted'


@CHROME
def test_the_dm_cache_key_is_made_once_and_a_fresh_device_reads_with_it(ran):
    ran('the_dm_cache_key_is_made_once_and_a_fresh_device_reads_with_it')


@scenario
async def no_dm_cache_key_is_minted_when_nobody_answers(S):
    S.hold('pcai:dmkey')
    a = await S.device()
    await a.js("__PC.switchView('messages');true")
    await S.quiet_for(25)
    assert S.pubs('pcai:dmkey') == []
    assert await a.js("__PC.VIEW==='messages' && !!document.querySelector('#feed')")


@CHROME
def test_no_dm_cache_key_is_minted_when_nobody_answers(ran):
    ran('no_dm_cache_key_is_minted_when_nobody_answers')


# ------------------------------------------------------------------------------- read markers

SEED_MENTION = r"""(async()=>{const me=__PC.me().pubkey;
  return NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-60,tags:[['p',me]],
    content:'hello from another account'},new Uint8Array(32).fill(%d))})()""" % OTHER_PK_FILL
BELL_LIT = "(()=>{const b=document.querySelector('#notif-badge');return !!b && !b.classList.contains('hidden')})()"


async def seed_mention(S, b):
    """Another account mentions this one -- stored on the home relay, then the device reloads, so the
    notification arrives the way a real one already waiting does."""
    ev = await b.js(SEED_MENTION)
    H.RELAY._store(ev)
    await S.open[-1].reload()
    return ev


@scenario
async def notifications_read_here_are_read_on_a_fresh_device(S):
    a = await S.device()
    ev = await seed_mention(S, a)
    await a.js("__PC.switchView('notifications');true")
    await H.wait(a, "document.querySelector('#feed').innerText.includes('hello from another account')", 60)
    await S.until(a, lambda: S.docs('pcai:seen'), 60, 'pcai:seen on the home relay')
    b = await S.device()
    await H.wait(b, "(JSON.parse(localStorage.getItem('pc_seen:'+__PC.me().pubkey)||'{}').notif||0)>=%d" % ev['created_at'], 60)
    await asyncio.sleep(2)
    assert not await b.js(BELL_LIT), 'the fresh device still shows the notification as new'


@CHROME
def test_notifications_read_here_are_read_on_a_fresh_device(ran):
    ran('notifications_read_here_are_read_on_a_fresh_device')


@scenario
async def read_markers_publish_nothing_when_nobody_answers(S):
    S.hold('pcai:seen')
    a = await S.device()
    await seed_mention(S, a)
    await a.js("__PC.switchView('notifications');true")
    await H.wait(a, "document.querySelector('#feed').innerText.includes('hello from another account')", 60)
    await S.quiet_for(20)
    assert S.pubs('pcai:seen') == []
    # read on this device, and the mark is kept to send later
    assert not await a.js(BELL_LIT)
    assert await a.js("JSON.parse(localStorage.getItem('pc_seen:'+__PC.me().pubkey)||'{}').pending===true")


@CHROME
def test_read_markers_publish_nothing_when_nobody_answers(ran):
    ran('read_markers_publish_nothing_when_nobody_answers')


# ------------------------------------------------------------------------------- music shares

LOAD_SHARES = r"""(async()=>{if(!window.PCMusicShare)await new Promise((res,rej)=>{const s=document.createElement('script');
  s.src='/static/js/client/musicshare.js';s.onload=res;s.onerror=rej;document.head.appendChild(s);});return true})()"""
OTHER_PK = "NostrTools.getPublicKey(new Uint8Array(32).fill(%d))" % OTHER_PK_FILL


@scenario
async def stopping_a_sender_saves_and_holds_on_a_fresh_device(S):
    a = await S.device()
    await a.js(LOAD_SHARES)
    await a.js("PCMusicShare.stopFrom(%s);true" % OTHER_PK)
    await S.until(a, lambda: S.docs('pcai:musicshares'), 60, 'pcai:musicshares on the home relay')
    b = await S.device()
    await b.js(LOAD_SHARES)
    assert await b.js("(async()=>{await PCMusicShare.loadDecisions();return PCMusicShare.isStopped(%s)})()" % OTHER_PK)


@CHROME
def test_stopping_a_sender_saves_and_holds_on_a_fresh_device(ran):
    ran('stopping_a_sender_saves_and_holds_on_a_fresh_device')


@scenario
async def share_answers_publish_nothing_when_nobody_answers(S):
    S.hold('pcai:musicshares')
    a = await S.device()
    await a.js(LOAD_SHARES)
    await a.js("PCMusicShare.stopFrom(%s);true" % OTHER_PK)
    await S.quiet_for(20)
    assert S.pubs('pcai:musicshares') == []
    assert await a.js("PCMusicShare.isStopped(%s)" % OTHER_PK), 'the answer was lost on this device'


@CHROME
def test_share_answers_publish_nothing_when_nobody_answers(ran):
    ran('share_answers_publish_nothing_when_nobody_answers')


# ------------------------------------------------------------------------------- VM hosts

async def add_vm_host(b):
    await b.js("__PC.switchView('vms');true")
    await H.wait(b, "!!document.querySelector('[data-act=add]')", 60)
    await b.js("document.querySelector('[data-act=add]').click();true")
    await H.answer_prompt(b, HOST)
    await H.answer_prompt(b, 'wss://vmhost.example/relay')


HAS_HOST = "!!(window.PCVms && PCVms._state.hosts.some(h=>h.pubkey==='%s'))" % HOST


@scenario
async def a_vm_host_saves_and_is_listed_on_a_fresh_device(S):
    a = await S.device()
    await add_vm_host(a)
    await S.until(a, lambda: S.docs('pcai:vmhosts'), 60, 'pcai:vmhosts on the home relay')
    b = await S.device()
    await b.js("__PC.switchView('vms');true")
    await H.wait(b, HAS_HOST, 60)


@CHROME
def test_a_vm_host_saves_and_is_listed_on_a_fresh_device(ran):
    ran('a_vm_host_saves_and_is_listed_on_a_fresh_device')


@scenario
async def a_vm_host_list_is_not_published_when_nobody_answers(S):
    S.hold('pcai:vmhosts')
    a = await S.device()
    await add_vm_host(a)
    await H.wait(a, "__toasts.some(t=>/could not be read/.test(t))", 40)
    assert S.pubs('pcai:vmhosts') == []
    assert await a.js(HAS_HOST), 'the host was dropped from this device'


@CHROME
def test_a_vm_host_list_is_not_published_when_nobody_answers(ran):
    ran('a_vm_host_list_is_not_published_when_nobody_answers')


# ------------------------------------------------------------------------------- Concord folders

SEED_ROOMS = r"""(()=>{const pk=__PC.me().pubkey;localStorage.setItem('pc.concord.rooms.v1.'+pk,JSON.stringify([
  {protocol:'nip29',relay:'wss://groups-a.example',name:'Alpha'},{protocol:'nip29',relay:'wss://groups-b.example',name:'Beta'}]));return true})()"""


async def open_concord(b):
    await b.js(SEED_ROOMS)
    await b.js("__PC.switchView('concord');true")
    await H.wait(b, "document.querySelectorAll('[data-cc-server]').length>=2", 60)


async def make_folder(b, name):
    await b.js("(()=>{const x=document.querySelector('[data-cc-server=\"0\"]');"
               "x.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));return true})()")
    await H.wait(b, "!!document.querySelector('.menu-pop [data-m^=\"with:\"]')", 20)
    await b.js("document.querySelector('.menu-pop [data-m^=\"with:\"]').click();true")
    await H.answer_prompt(b, name)


FOLDER_SHOWN = "[...document.querySelectorAll('[data-cc-folder]')].some(x=>(x.title||'')==='Pets')"


@scenario
async def a_community_folder_saves_and_shows_on_a_fresh_device(S):
    a = await S.device()
    await open_concord(a)
    await make_folder(a, 'Pets')
    await H.wait(a, FOLDER_SHOWN, 20)
    await S.until(a, lambda: S.docs('pcai:concord-rail'), 60, 'pcai:concord-rail on the home relay')
    b = await S.device()
    await open_concord(b)
    await H.wait(b, FOLDER_SHOWN, 60)


@CHROME
def test_a_community_folder_saves_and_shows_on_a_fresh_device(ran):
    ran('a_community_folder_saves_and_shows_on_a_fresh_device')


@scenario
async def a_community_folder_is_not_published_when_nobody_answers(S):
    S.hold('pcai:concord-rail', 'armada/metadata')
    a = await S.device()
    await open_concord(a)
    await make_folder(a, 'Pets')
    await H.wait(a, FOLDER_SHOWN, 20)
    await S.quiet_for(20)
    assert S.pubs('pcai:concord-rail') == []
    assert await a.js(FOLDER_SHOWN), 'the folder vanished from this device'


@CHROME
def test_a_community_folder_is_not_published_when_nobody_answers(ran):
    ran('a_community_folder_is_not_published_when_nobody_answers')


# ------------------------------------------------------------------------------- go-live templates

INGEST = {'enabled': True, 'rtmp_url': 'rtmp://live.example/live', 'stream_key': 'key', 'token': 'tok', 'whip_url': ''}


async def open_go_live(b):
    H.STATE['api']['/api/streams/ingest'] = INGEST
    await b.js("document.getElementById('nav-golive').click();true")
    await H.wait(b, "!!document.querySelector('#gl-tpl-save')", 40)


async def save_template(b, name):
    await b.js("(()=>{const t=document.querySelector('#gl-title');t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
               "document.querySelector('#gl-tpl-save').click();document.querySelector('#gl-tpl-name').value=%s;"
               "document.querySelector('#gl-tpl-ok').click();return true})()" % (json.dumps(name), json.dumps(name)))
    await H.wait(b, "/Saved/.test((document.querySelector('#gl-tpl-msg')||{}).textContent||'')", 60)


TEMPLATE_LISTED = "[...document.querySelectorAll('#gl-tpl-sel option')].some(o=>o.value==='Weekly show')"


@scenario
async def a_go_live_template_saves_and_is_offered_on_a_fresh_device(S):
    a = await S.device()
    await open_go_live(a)
    await save_template(a, 'Weekly show')
    assert await a.js("/^Saved “Weekly show”\\.$/.test(document.querySelector('#gl-tpl-msg').textContent)"), \
        await a.js("document.querySelector('#gl-tpl-msg').textContent")
    await S.until(a, lambda: S.docs('pcai:livetemplates'), 30, 'pcai:livetemplates on the home relay')
    b = await S.device()
    await open_go_live(b)
    await H.wait(b, TEMPLATE_LISTED, 60)


@CHROME
def test_a_go_live_template_saves_and_is_offered_on_a_fresh_device(ran):
    ran('a_go_live_template_saves_and_is_offered_on_a_fresh_device')


@scenario
async def a_go_live_template_is_kept_here_when_nobody_answers(S):
    S.hold('pcai:livetemplates')
    a = await S.device()
    await open_go_live(a)
    await save_template(a, 'Weekly show')
    assert await a.js("/on this device/.test(document.querySelector('#gl-tpl-msg').textContent)")
    assert await a.js(TEMPLATE_LISTED)
    assert S.pubs('pcai:livetemplates') == []


@CHROME
def test_a_go_live_template_is_kept_here_when_nobody_answers(ran):
    ran('a_go_live_template_is_kept_here_when_nobody_answers')


# ------------------------------------------------------------------------------- auto-mute switch

async def open_auto_mute(b):
    await b.js("__PC.switchView('settings')")
    await H.wait(b, "!!document.getElementById('set-auto-mute')", 40)


def _auto_mute_docs(S):
    return S.docs('pcai:automute')


@scenario
async def the_auto_mute_switch_saves_and_is_on_on_a_fresh_device(S):
    a = await S.device()
    await open_auto_mute(a)
    assert await toggle(a, 'set-auto-mute') is True
    await S.until(a, lambda: _auto_mute_docs(S), 60, 'pcai:automute on the home relay')
    b = await S.device()
    await open_auto_mute(b)
    await H.wait(b, "(()=>{const x=document.getElementById('set-auto-mute');"
                    "if(!x.checked&&!window.__amRe){window.__amRe=1;setTimeout(()=>{window.__amRe=0;"
                    "__PC.switchView('home');__PC.switchView('settings')},3000)}return x.checked})()", 60)


@CHROME
def test_the_auto_mute_switch_saves_and_is_on_on_a_fresh_device(ran):
    ran('the_auto_mute_switch_saves_and_is_on_on_a_fresh_device')


@scenario
async def the_auto_mute_switch_publishes_nothing_when_nobody_answers(S):
    S.hold('pcai:automute')
    a = await S.device()
    await open_auto_mute(a)
    assert await toggle(a, 'set-auto-mute') is True
    await S.quiet_for(20)
    assert S.pubs('pcai:automute') == []
    assert await a.js("document.getElementById('set-auto-mute').checked")


@CHROME
def test_the_auto_mute_switch_publishes_nothing_when_nobody_answers(ran):
    ran('the_auto_mute_switch_publishes_nothing_when_nobody_answers')
