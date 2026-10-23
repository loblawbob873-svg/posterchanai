"""A DEVICE THAT USED AN OLDER BUILD OPENS TODAY'S BUILD WITH EVERYTHING STILL THERE -- AND NOTHING OF ONE ACCOUNT'S
UNDER ANOTHER.

Every deploy changes stored shapes (localStorage keys, the kind-30078 pcai:* documents, the service worker's caches),
and each fix of the form "the old unscoped copy is adopted by ..." is a migration that runs exactly once per device, on
data no test made. So the old state here is made by the OLD CODE ITSELF, not by a fixture:

  old builds  the client (static/ + templates/) at three commits found in this repository's own history, relative to
              HEAD's commit date: the last one before HEAD's day ("yesterday"), 14 days and 30 days before HEAD
              (`git archive`, into a temporary directory removed afterwards). No commit is hard-coded: next month this
              still tests last month's data.
  the node    tests/client/realnode_harness.py -- the shipped RelayServer over PosterChanDB, the public CORD relays
              played locally (Concord), this node's REAL drafts route; one node per old build.
  the device  ONE Chrome profile (Account A). The old build is served at the node's address and A USES it: a missed
              call rung by Account C's real client, a calculator tape, a Meme Builder caption, a draft, a bookmark, a
              follow, a budget bill, a note, a Concord community. Chrome is closed. The node is then switched to TODAY's
              files -- shell, static files and sw.js -- and the same profile is reopened, so the upgrade goes the way a
              deploy reaches a person: the old service worker paints the cached old build, finds the new sw.js,
              installs it, and the page reloads itself onto the new build.

What must hold on today's build (one test per area and old build, so a failure names both):
  privacy     the first account to open each screen after the upgrade is NOT the one who used it: B is added on the
              device (the account card's "Add an account") before A opens anything, and B's Calculator, Calls, Meme
              Builder, Drafts and Communities show none of A's
  each area   back on A: the tape, the missed call, the caption, the draft, the bookmark, the follow, the bill, the
              note and the community are all there, each ONCE; a follow / a bill added on today's build keeps the old
              one; the relay's copies (kind 3, kind 10003, the drafts document, pcai:budget) still hold them
  upgrade     the page really runs today's build (every client script it loaded carries today's version), the old
              shell cache is gone, and nothing on today's build threw

What these found (2026-10-23, each red on all three old builds before its fix):
  * B was handed A's calculator tape, missed calls and Meme Builder build, and A lost the tape and the missed calls
    for good: each module adopted the old device-wide copy for "the first account to open the screen" -- app.js
    `_adoptPreAccountData` now gives it to the account signed in when the new build first starts;
  * A's build in progress never came back after B only LOOKED at the builder (meme.js load(): an empty slot read as
    "no build" while A's sat parked);
  * the device froze the moment C's call rang: a ringtone the browser refused retried itself for ever (calls.js
    `_ringtone`; test_a_refused_ringtone_falls_back_to_the_beep_full_app.py) -- which is why this device's Chrome
    allows autoplay: the OLD builds still have that bug.
Not an upgrade question, and so not asserted: an old build that could not deliver its Concord message ("channel key
is unavailable", 1 run in 3 on the build before 2026-10-23) or create a community at all (1 month back) -- printed as
a note / skipped with the old build's own words. A device signed out at the upgrade has no account to name as the
writer; the first to sign in takes the old copies, as the old build would have shown them to it.

WHERE THIS RUNS: the deploy gate's full pass (`deploy_regression_gate.py --full`) on a node, which has the whole
history. It is deliberately NOT a *_full_app.py and not on the gate's required list: the desktop CI checks out ONE
commit (actions/checkout, fetch-depth 1), so there is no old build to serve, and every test here skips saying so.

Fail-first: PC_UPGRADE_TODAY_OVERRIDE="/static/js/client/app.js=/tmp/x/app.js" serves another copy of a file as
TODAY's (the old builds are always served as they were).
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client import test_effects_full_app as full
from tests.client import wallpaper_harness as H
from tests import old_builds

ROOT = R.ROOT
A, B, C = 211, 212, 213               # the device's owner, a second account added on it, the caller
FAKE_MEDIA = ('--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
              '--autoplay-policy=no-user-gesture-required')
# The upgrading device may play sound on its own: every build before 2026-10-23 froze the page when the browser refused
# the ringtone (tests/client/test_a_refused_ringtone_falls_back_to_the_beep_full_app.py), and the OLD build is what
# rings here.
AUTOPLAY = ('--autoplay-policy=no-user-gesture-required',)
AREAS = ('privacy', 'calculator', 'missed_calls', 'meme', 'drafts', 'bookmarks', 'follows', 'budget', 'notes',
         'concord', 'upgrade')


# --------------------------------------------------------------------------------------------- the old builds
BUILDS = old_builds.client_builds()
CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
HISTORY = pytest.mark.skipif('' in BUILDS, reason=BUILDS.get('', ''))


# --------------------------------------------------------------------------------------------- serving one build
SERVING = {'root': ROOT, 'token': 'today'}


def serve(root, token):
    """From now on the node serves `root`'s client: the shell (rendered with `token` as its ?v=, as a deploy changes
    it), every static file and sw.js."""
    SERVING.update(root=Path(root), token=token)
    full.ROOT = Path(root)
    H.OVERRIDES.clear()
    if token == 'today':
        for pair in filter(None, os.environ.get('PC_UPGRADE_TODAY_OVERRIDE', '').split(',')):
            path, _, src = pair.partition('=')
            H.OVERRIDES[path] = src


def _shell(h, method, path, body):
    if method == 'GET' and path.split('?')[0] == '/client/sw.js':
        # As app/routers/client.py `client_sw` serves it: the worker's scope (/client) is above its own directory,
        # which only this header allows -- without it registration fails and no service worker ever runs.
        data = H.OVERRIDES.get('/static/js/client/sw.js')
        data = Path(data).read_bytes() if data else (SERVING['root'] / 'static/js/client/sw.js').read_bytes()
        h.send_response(200)
        h.send_header('Content-Type', 'application/javascript')
        h.send_header('Service-Worker-Allowed', '/client')
        h.send_header('Cache-Control', 'no-cache')
        h.send_header('Content-Length', str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return True
    if method != 'GET' or path.split('?')[0] not in ('/client', '/client/'):
        return False
    from jinja2 import Template
    html = Template((SERVING['root'] / 'templates/client.html').read_text()).render(
        ver=SERVING['token'], build=SERVING['token'], nostr_only=False, secure=True, meta=None,
        registration_enabled=True)
    data = html.encode()
    h.send_response(200)
    h.send_header('Content-Type', 'text/html; charset=utf-8')
    h.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
    h.send_header('Content-Length', str(len(data)))
    h.end_headers()
    h.wfile.write(data)
    return True


# --------------------------------------------------------------------------------------------- the node's API
class Ctx:
    """The node's real drafts route, each account a member (the bookmarks/drafts flow suite's wiring)."""

    def __init__(self, n):
        from fastapi import APIRouter
        from app.routers import client
        from app.services import nostr_store, settings_store, user_table
        from app.services.nostr import nostr_service
        from tests.client.test_bookmarks_drafts_flows_full_app import storage_sk
        self.storage_sk = storage_sk
        self.mp = mp = pytest.MonkeyPatch()
        self.n = n
        self.users = users = {R.pubkey(f): F.member(R.pubkey(f), f) for f in (A, B, C)}
        by_npub = {}
        for pk, u in users.items():
            by_npub[nostr_service.npub_of(pk)] = u
            by_npub[pk] = u

        async def aby_npub(_db, npub):
            return by_npub.get(npub)
        mp.setattr(user_table, 'aby_npub', aby_npub)
        mp.setattr(nostr_store, 'user_storage_seckey', lambda _db, u: storage_sk(u))
        real_get = settings_store.get
        mp.setattr(settings_store, 'get', lambda k, *a, **kw: str(n.home.port) if k == 'nostr_relay_port'
                   else real_get(k, *a, **kw))
        r = APIRouter()
        r.add_api_route('/client/drafts', client.drafts_sync, methods=['POST'])
        self.api = F.Routers(mp, [(r, '')], users, paths=['/client/drafts'])
        H.ROUTES.append(_shell)

    def server_drafts(self, pk):
        """The account's drafts as the NODE holds them (decrypted with its storage key), live ones only."""
        from app.services import nostr_store
        doc = asyncio.run_coroutine_threadsafe(
            nostr_store.get_doc(self.n.home.port, 'pcai:drafts', seckey=self.storage_sk(self.users[pk]), strict=True),
            self.api.loop).result(30)
        return [d.get('text') for d in ((doc or {}).get('drafts') or []) if isinstance(d, dict) and not d.get('del')]

    def stop(self):
        try:
            H.ROUTES.remove(_shell)
        except ValueError:
            pass
        self.api.stop()
        self.mp.undo()


# --------------------------------------------------------------------------------------------- devices
# Every uncaught error AND every unhandled rejection the page meets, kept across nothing: a reload starts a new list,
# so `errors()` is read before every navigation.
ERRORS_INIT = r'''
addEventListener('unhandledrejection',e=>{try{(window.__rejections=window.__rejections||[]).push(String(e.reason&&(e.reason.stack||e.reason.message)||e.reason));}catch(_){}});
'''


async def device(n, fill, profile=None, chrome_args=()):
    if profile:
        Path(profile, 'DevToolsActivePort').unlink(missing_ok=True)     # the last Chrome's, or the new one reads it
    d = R.Device(n, fill, pre_init=F.CLIENT_INIT + ERRORS_INIT, profile=profile, chrome_args=chrome_args)
    await d.__aenter__()
    d.pk = R.pubkey(fill)
    return d


async def close(d):
    try:
        await d.__aexit__()
    except Exception:
        pass


async def js(d, expr):
    return await d.b.js(expr)


async def wait(d, expr, seconds=30, what=None):
    await F.wait(d, expr, seconds, what)


async def until(fn, seconds, what, d=None):
    return await F.until(fn, seconds, what, d)


SCRIPTS = ("[...new Set([...document.querySelectorAll('script[src]')].map(s=>s.src)"
           ".concat(performance.getEntriesByType('resource').map(e=>e.name)))]"
           ".filter(u=>/\\/static\\/js\\/client\\/[^/?]+\\.js/.test(u))")


async def on_build(d, token, seconds=90):
    """The page is running the build served as `token` (the shell's ?v=), signed in and connected."""
    await wait(d, "(()=>{const s=document.querySelector('script[src*=\"/static/js/client/app.js\"]');"
                  "return !!s && s.src.includes('v=%s') && !!window.__PC && !!__PC.me && !!__PC.me() && "
                  "Relay.status==='ok'})()" % token, seconds, 'the page running the %s build' % token)


async def errors(d):
    return await js(d, "({errors:(window.__errors||[]).filter(e=>!/ResizeObserver/.test(e)),"
                       "rejections:window.__rejections||[]})")


# --------------------------------------------------------------------------------------------- what a person does
async def add_account(d, fill):
    await F.add_account(d, fill)


async def switch_account(d, fill):
    await F.switch_account(d, fill)


# calculator
TAPE = "[...document.querySelectorAll('.calc-tape-row')].map(r=>r.textContent.replace(/\\s+/g,' ').trim())"


async def open_calculator(d):
    await js(d, "__PC.switchView('calculator');true")
    await wait(d, "!!document.querySelector('#feed .calc-app')", 30, 'the Calculator')


async def calculate(d, *keys):
    for k in keys:
        await js(d, "document.querySelector(%s).click();true" % json.dumps('.calc-key[data-k="%s"]' % k))


# calls
MISSED_ON_SCREEN = "[...document.querySelectorAll('.call-missed .call-contact[data-pk]')].map(e=>e.dataset.pk)"


async def open_calls(d):
    await js(d, "__PC.switchView('calls');true")
    await wait(d, "!!document.querySelector('#call-npub')", 30, 'the Calls screen')
    await asyncio.sleep(.5)


async def ring_and_give_up(caller, callee):
    """The caller's real client rings the callee, who does not answer; the caller hangs up."""
    npub = await js(callee, "NostrTools.nip19.npubEncode(__PC.me().pubkey)")
    for attempt in range(3):
        await open_calls(caller)
        await js(caller, "document.querySelector('#call-npub').value=%s;document.querySelector('#call-start-btn').click();true"
                 % json.dumps(npub))
        try:
            await wait(callee, "!!document.getElementById('call-accept')", 25, 'the incoming call')
            break
        except AssertionError:
            await js(caller, "(document.getElementById('call-hang')||{click(){}}).click();true")
            if attempt == 2:
                raise
            await asyncio.sleep(2)
    await wait(caller, "!!document.getElementById('call-hang')", 15, 'the hang-up button')
    await js(caller, "document.getElementById('call-hang').click();true")
    await wait(callee, "!document.querySelector('#call-overlay')", 30, 'the phone to stop ringing')


# meme builder
async def open_meme(d):
    await js(d, "__PC.switchView('meme');true")
    await wait(d, "!!document.getElementById('mb-stage') && !!window.PCMeme && !!document.querySelector('.mb-wrap')",
               40, 'the Meme Builder')


STAGE_TEXT = "(document.getElementById('mb-stage')||{}).innerText||''"


async def add_caption(d, text):
    await js(d, "document.getElementById('mb-add-text').click();true")
    await wait(d, "!!document.getElementById('mb-f-text')", 15, 'the caption inspector')
    await js(d, "(t=>{const e=document.getElementById('mb-f-text');e.value=t;"
                "e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));"
                "return true})(%s)" % json.dumps(text))
    await wait(d, "%s.includes(%s)" % (STAGE_TEXT, json.dumps(text)), 15, 'the caption on the stage')


# drafts
DRAFTS = "[...document.querySelectorAll('#feed .draft-card .draft-body')].map(x=>x.innerText.trim())"


async def save_draft(d, text):
    await F.view(d, 'home', "!!document.querySelector('#tl-fab')")
    await js(d, "document.querySelector('#tl-fab').click();true")
    await wait(d, "!!document.querySelector('#cmp') && !!document.querySelector('#cmp-draft')", 20, 'the composer')
    await js(d, "(()=>{const t=document.querySelector('#cmp');t.focus();t.value=%s;"
                "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-draft').click();return true})()"
             % json.dumps(text))
    await wait(d, "(window.__toastLog||[]).some(t=>/saved to drafts/i.test(t))", 20, 'the draft saved')


async def open_drafts(d):
    await js(d, "__PC.switchView('drafts');true")
    await wait(d, "!!document.querySelector('#feed #sched-section')", 30, 'the Drafts screen')


# bookmarks
def bookmarked(n, pk):
    evs = n.home.query({'kinds': [10003], 'authors': [pk]})
    if not evs:
        return None
    return [t[1] for t in max(evs, key=lambda e: e['created_at'])['tags'] if t and t[0] == 'e']


async def press_bookmark(d, note_id):
    await F.open_note(d, note_id)
    card = F.CARD % note_id
    await wait(d, "!!%s.querySelector('.act[data-a=menu]')" % card, 20, "the post's menu button")
    await js(d, "%s.querySelector('.act[data-a=menu]').click();true" % card)
    await wait(d, "!!document.querySelector('.menu-pop [data-m=bookmark]')", 20, 'the post menu')
    await js(d, "document.querySelector('.menu-pop [data-m=bookmark]').click();true")


LISTED = "[...document.querySelectorAll('#feed .note[data-id]')].map(x=>x.dataset.id)"


# follows
def follows(n, pk):
    evs = n.home.query({'kinds': [3], 'authors': [pk]})
    if not evs:
        return None
    return {t[1] for t in max(evs, key=lambda e: e['created_at'])['tags'] if t and t[0] == 'p'}


async def press_follow(d, pk):
    """Profile -> its menu -> Follow / Unfollow; returns the label pressed."""
    await js(d, "document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove());"
                "__PC.openProfile(%s);true" % json.dumps(pk))
    await wait(d, "!!document.querySelector('#feed .prof') && !!document.querySelector('#feed #prof-menu')", 60,
               'the profile')
    await asyncio.sleep(.5)
    await js(d, "document.querySelector('#feed #prof-menu').click();true")
    await wait(d, "!!document.querySelector('.menu-pop [data-m=\"follow\"]')", 20, 'the profile menu')
    label = await js(d, "document.querySelector('.menu-pop [data-m=\"follow\"]').textContent")
    await js(d, "document.querySelector('.menu-pop [data-m=\"follow\"]').click();true")
    return label


# budget
BILLS = "[...document.querySelectorAll('.bg-row .bg-name')].map(x=>x.firstChild.textContent.trim())"


async def open_budget(d):
    await js(d, "__PC.switchView('budget');true")
    await wait(d, "!!document.querySelector('#bg-n') || !!document.querySelector('.bg-err')", 40, 'Budget')
    await wait(d, "!!document.querySelector('#bg-n')", 20, 'Budget loaded')


async def add_bill(d, name, amount):
    await js(d, """(()=>{document.querySelector('#bg-n').value=%s;document.querySelector('#bg-a').value=%s;
      const i=document.querySelector('#bg-inc');if(i)i.checked=false;document.querySelector('#bg-save').click();return true})()"""
             % (json.dumps(name), json.dumps(amount)))
    await wait(d, "%s.includes(%s)" % (BILLS, json.dumps(name)), 20, 'the bill listed')


async def budget_on_relay(d, n):
    got = F.stored(n, {'kinds': [30078], 'authors': [d.pk], '#d': ['pcai:budget']})
    if not got:
        return None
    ev = max(got, key=lambda e: e['created_at'])
    doc = await js(d, "(async()=>JSON.parse(await __PC.nip44dec(__PC.me().pubkey,%s)))()" % json.dumps(ev['content']))
    return sorted(b['name'] for b in doc.get('bills') or [])


# notes
SET = """((sel,v)=>{const x=document.querySelector(sel);x.value=v;x.dispatchEvent(new Event('input',{bubbles:true}));
  x.dispatchEvent(new Event('change',{bubbles:true}));return true})"""
MEMBER = ("(()=>{try{PCInstanceAccess.accept({pubkey:__PC.me().pubkey,qualified:true},__PC.me().pubkey)}catch(_){}"
          "return true})()")
NOTE_ITEMS = ("[...document.querySelectorAll('.nt-results .nt-item')].map(x=>x.innerText.replace(/\\s+/g,' ').trim())")


async def open_notes(d):
    await js(d, MEMBER)
    await js(d, "__PC.switchView('notes');true")
    await wait(d, "!!document.querySelector('.nt-wrap .nt-new')", 60, 'Notes')


async def write_note(d, title, body):
    await js(d, "document.querySelector('.nt-wrap .nt-new').click();true")
    await wait(d, "!!document.querySelector('.nt-editor .nt-title') && "
                  "!document.querySelector('.nt-editor .nt-body').classList.contains('hidden')", 20, 'a new note')
    await js(d, SET + '(%s,%s)' % (json.dumps('.nt-editor .nt-title'), json.dumps(title)))
    await js(d, SET + '(%s,%s)' % (json.dumps('.nt-editor .nt-body'), json.dumps(body)))
    await js(d, "document.querySelector('.nt-editor .nt-body').dispatchEvent(new Event('blur'));true")
    await wait(d, "/saved/i.test(document.querySelector('.nt-editor .nt-state').textContent)", 30, 'the note saved')


# concord
CLIP = ("(()=>{window.__copies=window.__copies||[];const p=__PC;if(p.__clipWrapped)return true;p.__clipWrapped=true;"
        "const c=p.copyValue;p.copyValue=(v,...a)=>{__copies.push(String(v));try{return c&&c.call(p,v,...a);}catch(_){return true;}};"
        "return true})()")
RAIL = "[...document.querySelectorAll('[data-cc-server]')].map(b=>(b.getAttribute('title')||b.getAttribute('aria-label')||b.innerText||'').trim())"
LINES = ("[...document.querySelectorAll('.cc-message[data-message-id]')]"
         ".map(r=>((r.querySelector('.cc-message-body')||r).innerText||'').trim())")


async def open_concord(d):
    await js(d, CLIP)
    await js(d, "__PC.switchMessagesTab('concord');true")
    await wait(d, '!!window.PCConcord && !!window.PosterCord', 40, 'Concord loaded')


async def create_community(d, name):
    await open_concord(d)
    await wait(d, "!!document.querySelector('#cc-create-go')", 30, 'the Create dialog')
    await js(d, "__copies.length=0;document.querySelector('#cc-community-name').value=%s;"
                "document.querySelector('#cc-create-go').click();true" % json.dumps(name))
    await wait(d, "__copies.some(x=>x.includes('/invite/'))", 90, 'the community created')
    for _ in range(30):
        if await js(d, "(()=>{const m=[...document.querySelectorAll('.uiconfirm-msg')].find(x=>/Copy this/.test(x.textContent));"
                       "if(!m)return false;const c=[...m.closest('.uiconfirm').querySelectorAll('button')].find(x=>x.textContent.trim()==='Close');"
                       "if(c)c.click();return true})()"):
            break
        await asyncio.sleep(.1)


# --------------------------------------------------------------------------------------------- the scenario
TEXT = {'tape': '12*3= 36', 'caption': 'Upgrade day caption', 'draft': 'A post kept for later, from the old build',
        'bill': 'Rent from the old build', 'bill2': 'Power on today\'s build', 'note': 'Old build shopping list',
        'note_body': 'eggs, flour and a migration', 'community': 'Old build club', 'message': 'hello from the old build'}


_T0 = time.monotonic()


def log(*a):
    """Progress on stderr (`pytest -s` shows where a long scenario is)."""
    import sys
    print('[upgrade %6.1fs]' % (time.monotonic() - _T0), *a, file=sys.stderr, flush=True)


class Outcome(dict):
    """area -> None (held) | the AssertionError that says what did not | 'n/a: why'."""
    notes: list

    def __init__(self):
        super().__init__()
        self.notes = []

    def area(self, name):
        out = self

        class _A:
            def __enter__(self):
                log('area', name)
                return self

            def __exit__(self, kind, exc, tb):
                if exc is None:
                    out.setdefault(name, None)
                    return False
                log('area', name, 'FAILED:', str(exc)[:600])
                if isinstance(exc, (AssertionError, Exception)) and not isinstance(exc, (KeyboardInterrupt,)):
                    if out.get(name) is None:
                        out[name] = exc if isinstance(exc, AssertionError) else AssertionError(repr(exc))
                    return True
                return False
        return _A()

    def fail(self, name, why):
        if self.get(name) is None:
            self[name] = AssertionError(why)

    def na(self, name, why):
        self.setdefault(name, 'n/a: ' + why)


async def scenario(n, ctx, old_root, label, sha):
    out = Outcome()
    has_calc = (old_root / 'static/js/client/calculator.js').exists()
    profile = tempfile.mkdtemp(prefix='pc-upgrade-profile-')
    old_token = 'old-' + sha[:10]
    made = set()
    try:
        # ------------------------------------------------------------------ the OLD build, used
        serve(old_root, old_token)
        log('serving the', label, 'build', sha[:10])
        caller = await device(n, C, chrome_args=FAKE_MEDIA)
        a = await device(n, A, profile=profile, chrome_args=AUTOPLAY)
        await on_build(a, old_token)
        await on_build(caller, old_token)
        log('both devices on the old build')
        post = await F.post_note(caller, 'A post worth keeping (%s)' % label)
        await asyncio.sleep(2)                      # both clients subscribed for their own call frames
        with out.area('missed_calls'):
            await ring_and_give_up(caller, a)
            await open_calls(a)
            await wait(a, "%s.length===1" % MISSED_ON_SCREEN, 20, "the missed call on the OLD build's Calls screen")
            made.add('missed_calls')
        await close(caller)
        with out.area('calculator'):
            if not has_calc:
                out.na('calculator', 'the %s build has no Calculator' % label)
            else:
                await open_calculator(a)
                await calculate(a, '1', '2', '*', '3', 'eq')
                assert await js(a, TAPE) == [TEXT['tape']], await js(a, TAPE)
                made.add('calculator')
        with out.area('meme'):
            await open_meme(a)
            await add_caption(a, TEXT['caption'])
            await asyncio.sleep(1)
            assert TEXT['caption'] in (await js(a, "localStorage.getItem('pc_meme_project')||''")), 'the old build kept no project'
            made.add('meme')
        with out.area('drafts'):
            await save_draft(a, TEXT['draft'])
            await until(lambda: TEXT['draft'] in ctx.server_drafts(a.pk), 30, 'the draft on the node', a)
            made.add('drafts')
        with out.area('bookmarks'):
            await press_bookmark(a, post['id'])
            await until(lambda: bookmarked(n, a.pk) == [post['id']], 30, 'the bookmark on the relay', a)
            made.add('bookmarks')
        with out.area('follows'):
            await press_follow(a, R.pubkey(C))
            await until(lambda: R.pubkey(C) in (follows(n, a.pk) or ()), 30, 'the follow on the relay', a)
            made.add('follows')
        with out.area('budget'):
            await open_budget(a)
            await add_bill(a, TEXT['bill'], '1200')
            await until(lambda: F.stored(n, {'kinds': [30078], 'authors': [a.pk], '#d': ['pcai:budget']}), 30,
                        'the budget on the relay', a)
            made.add('budget')
        with out.area('notes'):
            await open_notes(a)
            await write_note(a, TEXT['note'], TEXT['note_body'])
            made.add('notes')
        with out.area('concord'):
            try:
                await create_community(a, TEXT['community'])
            except AssertionError:
                refused = [t for t in await js(a, "window.__toastLog||[]") if 'community creation failed' in t]
                if not refused:
                    raise
                # The old build's own bug (1 month back: "Assignment to constant variable."): nothing to upgrade.
                out.na('concord', 'the %s build could not create a community: %s' % (label, refused[-1]))
            if out.get('concord') is None:
                made.add('concord')
                await R.open_room_channel(a)
                await R.concord_send(a, TEXT['message'])
                # Delivered = the optimistic row was replaced by the signed one. Older builds failed here on their own
                # ("channel key is unavailable", fixed 2026-10-23); such a message never existed anywhere to upgrade.
                try:
                    await wait(a, "[...document.querySelectorAll('.cc-message[data-message-id]')].some(r=>"
                                  "r.innerText.includes(%s)&&!r.dataset.messageId.startsWith('pending-'))"
                               % json.dumps(TEXT['message']), 30, 'the message delivered')
                    made.add('concord_message')
                except AssertionError:
                    out.notes.append('the %s build did not deliver its Concord message: %s'
                                     % (label, (await js(a, "window.__toastLog||[]"))[-1:]))
        old_errors = await errors(a)
        # The old build's service worker controls the page (and holds its shell) before the app is closed: the
        # reopen below must go through it, as a phone's does.
        await wait(a, "!!navigator.serviceWorker.controller", 60, 'the old build\'s service worker in control')
        await close(a)

        # ------------------------------------------------------------------ TODAY's build, reached through sw.js
        serve(ROOT, 'today')
        log('serving TODAY; reopening the profile')
        # Strictly after the old build's last second: an event it signed in the second the app closed is not one
        # today's build published.
        await asyncio.sleep(1.2)
        upgraded_at = int(time.time())
        a = await device(n, A, profile=profile, chrome_args=AUTOPLAY)
        seen_errors = []
        with out.area('upgrade'):
            await on_build(a, 'today', 120)
        if out.get('upgrade') is not None:
            return out                              # nothing below is about today's build
        with out.area('upgrade'):
            stale = [u for u in await js(a, SCRIPTS) if 'v=today' not in u]
            assert not stale, ('client scripts from the OLD build in a page running today\'s', stale)
            caches = await js(a, "(async()=>(await caches.keys()))()")
            shells = [c for c in caches if c.startswith('pc-nostr-')]
            assert len(shells) == 1, ('the old shell cache is still there beside the new one', caches)

        # ---- privacy: B is added on this device before A opens anything, and opens every screen first
        with out.area('privacy'):
            seen_errors.append(await errors(a))
            await add_account(a, B)
            leaks = []
            if 'calculator' in made:
                await open_calculator(a)
                if await js(a, TAPE):
                    leaks.append(('calculator tape', await js(a, TAPE)))
            if 'missed_calls' in made:
                await open_calls(a)
                if await js(a, MISSED_ON_SCREEN):
                    leaks.append(('missed calls', await js(a, MISSED_ON_SCREEN)))
            if 'meme' in made:
                await open_meme(a)
                if TEXT['caption'] in await js(a, STAGE_TEXT):
                    leaks.append(('meme project', await js(a, STAGE_TEXT)))
            if 'drafts' in made:
                await open_drafts(a)
                await asyncio.sleep(2)
                if any(TEXT['draft'] in x for x in await js(a, DRAFTS)):
                    leaks.append(('drafts', await js(a, DRAFTS)))
            if 'concord' in made:
                await open_concord(a)
                await asyncio.sleep(3)
                if any(TEXT['community'] in x for x in await js(a, RAIL)):
                    leaks.append(('communities', await js(a, RAIL)))
            seen_errors.append(await errors(a))
            assert not leaks, ('account B, added after the upgrade, was shown account A\'s data', leaks)
        seen_errors.append(await errors(a))
        await switch_account(a, A)
        log('back on A')

        # ---- every area, back on A
        with out.area('calculator'):
            if 'calculator' in made:
                await open_calculator(a)
                assert await js(a, TAPE) == [TEXT['tape']], ('A\'s tape after the upgrade', await js(a, TAPE))
        with out.area('missed_calls'):
            if 'missed_calls' in made:
                await open_calls(a)
                assert await js(a, MISSED_ON_SCREEN) == [R.pubkey(C)], \
                    ('A\'s missed call after the upgrade', await js(a, MISSED_ON_SCREEN))
        with out.area('meme'):
            if 'meme' in made:
                await open_meme(a)
                await wait(a, "%s.includes(%s)" % (STAGE_TEXT, json.dumps(TEXT['caption'])), 15,
                           'A\'s caption in the Meme Builder after the upgrade')
        with out.area('drafts'):
            if 'drafts' in made:
                await open_drafts(a)
                await wait(a, "%s.filter(x=>x.includes(%s)).length===1" % (DRAFTS, json.dumps(TEXT['draft'])), 30,
                           'A\'s draft, once, after the upgrade')
                await asyncio.sleep(2)
                assert ctx.server_drafts(a.pk).count(TEXT['draft']) == 1, ctx.server_drafts(a.pk)
        with out.area('bookmarks'):
            if 'bookmarks' in made:
                await js(a, "__PC.switchView('bookmarks');true")
                await wait(a, "%s.filter(x=>x===%s).length===1" % (LISTED, json.dumps(post['id'])), 40,
                           'the bookmarked post, once, after the upgrade')
                assert bookmarked(n, a.pk) == [post['id']], bookmarked(n, a.pk)
        with out.area('follows'):
            if 'follows' in made:
                assert R.pubkey(C) in (follows(n, a.pk) or ()), follows(n, a.pk)
                # A follow pressed on today's build (followsAt never written by the old one) keeps the old follow.
                label_ = await press_follow(a, R.pubkey(B))
                assert 'Unfollow' not in label_, label_
                await until(lambda: {R.pubkey(B), R.pubkey(C)} <= (follows(n, a.pk) or set()), 30,
                            'the old follow kept beside the new one', a)
        with out.area('budget'):
            if 'budget' in made:
                await open_budget(a)
                await wait(a, "%s.filter(x=>x===%s).length===1" % (BILLS, json.dumps(TEXT['bill'])), 30,
                           'the old bill, once')
                await add_bill(a, TEXT['bill2'], '80')
                for _ in range(60):
                    if await budget_on_relay(a, n) == sorted([TEXT['bill'], TEXT['bill2']]):
                        break
                    await asyncio.sleep(.5)
                assert await budget_on_relay(a, n) == sorted([TEXT['bill'], TEXT['bill2']]), await budget_on_relay(a, n)
        with out.area('notes'):
            if 'notes' in made:
                await open_notes(a)
                await wait(a, "%s.filter(x=>x.includes(%s)).length===1" % (NOTE_ITEMS, json.dumps(TEXT['note'])), 40,
                           'the note, once, after the upgrade')
        with out.area('concord'):
            if 'concord' in made:
                await open_concord(a)
                await wait(a, "%s.filter(x=>x.includes(%s)).length===1" % (RAIL, json.dumps(TEXT['community'])), 60,
                           'the community, once, after the upgrade')
                if 'concord_message' in made:
                    await R.open_room_channel(a)
                    await wait(a, "%s.filter(x=>x.includes(%s)).length===1" % (LINES, json.dumps(TEXT['message'])), 40,
                               'the old message, once, in #general')
        seen_errors.append(await errors(a))

        # ---- nothing on today's build threw, and every module opened since (Concord's cord-reader.js is loaded on
        # demand since 2026-10-23) came from today's build, not the old worker's cache
        with out.area('upgrade'):
            stale = [u for u in await js(a, SCRIPTS) if 'v=today' not in u]
            assert not stale, ('client scripts from the OLD build loaded after the upgrade', stale)
            thrown = [e for s in seen_errors for e in s['errors']]
            assert not thrown, ('uncaught errors on today\'s build after the upgrade', thrown)
        out.republished = sorted({(e['kind'], next((t[1] for t in e['tags'] if t and t[0] == 'd'), ''))
                                  for e in F.stored(n, {'authors': [a.pk], 'since': upgraded_at})})
        with out.area('upgrade'):
            # Nothing re-published by the upgrade itself: what A published on today's build is what A did there (a
            # follow, a bill, the read mark of the channel opened) -- not the bookmarks, a note, a community's listing
            # post or any other document re-sent on boot.
            expected = {(3, ''), (30078, 'pcai:budget'), (30078, 'pcai:concord-read')}
            extra = [(e['kind'], e['created_at'] - upgraded_at, e['tags'][:3], e['content'][:80])
                     for e in F.stored(n, {'authors': [a.pk], 'since': upgraded_at})
                     if (e['kind'], next((t[1] for t in e['tags'] if t and t[0] == 'd'), '')) not in expected]
            assert not extra, ('published by the upgraded device without being asked (kind, seconds after the '
                               'upgrade, tags, content)', extra)
        out.old_errors = old_errors
        out.rejections = [e for s in seen_errors for e in s['rejections']]
        await close(a)
        return out
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture(scope='module', params=[k for k in BUILDS if k] or ['none'])
def upgraded(request):
    if '' in BUILDS or not R.chrome_ok():
        pytest.skip(BUILDS.get('', 'Chrome required'))
    label = request.param
    sha = BUILDS[label]
    tmp = tempfile.mkdtemp(prefix='pc-upgrade-old-')
    try:
        old_root = old_builds.extract(sha, tmp)
        with R.Node(internet=R.CORD_PUBLIC, members=[R.pubkey(f) for f in (A, B, C)]) as n:
            ctx = Ctx(n)
            try:
                result = asyncio.run(scenario(n, ctx, old_root, label, sha))
            finally:
                ctx.stop()
                serve(ROOT, 'today')
        result.label, result.sha = label, sha
        log(label, sha[:10], 'notes:', result.notes, 'rejections:', getattr(result, 'rejections', None),
            'published after the upgrade:', getattr(result, 'republished', None))
        yield result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check(upgraded, area):
    got = upgraded.get(area)
    if isinstance(got, str):
        pytest.skip('%s (%s build %s)' % (got, upgraded.label, upgraded.sha[:9]))
    if area not in upgraded:
        raise AssertionError('%s was never reached on the %s build (an earlier step failed: %s)'
                             % (area, upgraded.label, {k: str(v)[:300] for k, v in upgraded.items() if v}))
    if got is not None:
        raise AssertionError('%s build %s: %s' % (upgraded.label, upgraded.sha[:9], got)) from got


@CHROME
@HISTORY
@pytest.mark.parametrize('area', AREAS)
def test_an_old_device_upgrades_cleanly(upgraded, area):
    _check(upgraded, area)
