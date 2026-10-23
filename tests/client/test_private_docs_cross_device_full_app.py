"""What one device writes, a FRESH device of the same account opens -- Notes, the encrypted drive, the
Calendar and the wallet document -- and a deletion stays deleted.

The sibling of tests/client/test_private_docs_save_with_a_silent_relay_full_app.py (same harness, same
runner: every scenario its own account, three at a time, "use my own relays" on with a SilentRelay in
the list). Each scenario drives the SHIPPED client in real Chrome through the controls a person uses:

  * persist   device A writes through the app; device B -- a fresh Chrome profile, same account -- opens
              it, decrypted (the exact text, the exact bytes, the same address);
  * no read   the read nobody answers: nothing is written over it, the screen says so, and what the
              device already held is still shown.

The two documents the SERVER keeps for the account (Calendar: `app/routers/calendar.py`, encrypted with the
user's server-held storage key; the wallet: `app/routers/exodus_wallet.py`) are answered by those REAL
routers, mounted in this process (`Node`), whose `nostr_store` reads and writes go to the harness's real
relay. Only the session (`/api/auth/nostr-login` -> a bearer per account) and the storage key (one per
account, derived here) are stand-ins -- so "nobody answers" is the relay really never answering the
storage key's read, and the router's own strict-read 503 is what the client gets.

Notes' "deleted, and the offline queue flushes later": a note saved while the relay REFUSES it is queued
on the device (notes.js `queue`); deleted once the relay is back, that stale queued copy must never be
sent again -- any relay that did not see the tombstone would hold the note's text again.

The drive has NO Trash for its own files: a drive file's ✕ is a Blossom DELETE plus an index tombstone
(filesindex.js `forget`), so what is asserted is that the deletion sticks on another fresh device. Files ->
Trash is folder sync's (tombstoned `pcai:fs:` records behind `/client/sync-state`, filled by a device's sweep),
which is not this harness's to drive.

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/notes.js=/tmp/old/notes.js,..." serves an older or
deliberately broken copy of a shipped file (see the sibling's docstring).
"""
import asyncio
import base64
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.client import wallpaper_harness as H
from tests.client.test_private_docs_save_with_a_silent_relay_full_app import MEMBER, no_errors, run_scenarios

CHROME = pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')
SCENARIOS = {}
FIRST_FILL = 80            # 80, 81, ... -- never the sibling's 40.. nor 1 / 7
# Measured seconds when run alone: the longest start first, so the lanes finish together.
COST = {}
# A PUBLISHED BIP-39 TEST VECTOR -- every wallet library's first test, holds nothing, and is never a seed.
TEST_PHRASE = 'abandon ' * 11 + 'about'


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


# ------------------------------------------------------------------------------------- the node's own API

class Node:
    """The node's calendar and wallet routers, REAL, in this process: `nostr_store` against the harness relay,
    one storage key per account, a bearer per account from `/api/auth/nostr-login`."""

    def __init__(self, pubkeys):
        self.users = {pk: SimpleNamespace(id=1000 + i, username='acct%d' % i, nostr_npub=pk,
                                          email='acct%d@example.invalid' % i)
                      for i, pk in enumerate(pubkeys)}
        self.loop = None
        self.client = None
        self.answered = []           # (account pubkey, path, status) of every request the routers answered

    @staticmethod
    def storage_sk(user):
        return hashlib.sha256(b'pc-test-storage:' + str(user.nostr_npub).encode()).digest()

    def storage_pk(self, pk):
        from app.services.nostr import bip340
        return bip340.pubkey_from_seckey(self.storage_sk(self.users[pk])).hex()

    def start(self, mp):
        import httpx
        from fastapi import FastAPI, HTTPException, Request
        from app.database import get_db
        from app.auth import get_current_user
        from app.services import caldav_store, exodus_vault, nostr_store
        from tests.doc_table_mem import mem_tables
        from tests.member_fixtures import allow_keys

        port = lambda *_a, **_k: H.RELAY.port           # noqa: E731
        mp.setattr(nostr_store, 'user_storage_seckey', lambda _db, u: self.storage_sk(u), raising=False)
        mp.setattr(caldav_store, 'user_storage_seckey', lambda _db, u: self.storage_sk(u))
        mp.setattr(caldav_store, '_port', port)
        mp.setattr(caldav_store, 'enabled', lambda: True)
        mp.setattr(exodus_vault, '_port', port)

        async def no_password(*_a, **_k):
            return False
        mp.setattr('app.services.user_settings_table.ahas', no_password)
        mem_tables(mp)                                     # the wallet's backup row: operator tables, in memory
        allow_keys(mp, *self.users)
        from app.routers.calendar import router as cal_router
        from app.routers.exodus_wallet import router as ex_router
        app = FastAPI()
        app.include_router(cal_router)
        app.include_router(ex_router)

        def who(request: Request):
            tok = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
            if not tok.startswith('tok-') or tok[4:] not in self.users:
                raise HTTPException(401, 'not signed in')
            return self.users[tok[4:]]
        app.dependency_overrides[get_current_user] = who
        app.dependency_overrides[get_db] = lambda: None
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node')
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        H.ROUTES.append(self.route)

    def stop(self):
        try:
            H.ROUTES.remove(self.route)
        except ValueError:
            pass
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def route(self, h, method, path, body):
        p = path.split('?')[0]
        if p == '/api/auth/nostr-login' and method == 'POST':
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
            if pk not in self.users:
                return False
            u = self.users[pk]
            h._json({'access_token': 'tok-' + pk, 'token_type': 'bearer',
                     'user': {'id': u.id, 'username': u.username, 'nostr_npub': pk}})
            return True
        if not (p.startswith('/api/calendar/') or p.startswith('/api/wallet/exodus/')):
            return False
        if p == '/api/wallet/exodus/balances':      # chain providers are the internet; "unknown" is an answer
            h._json({'detail': 'balances are not part of this test'}, 503)
            return True
        headers = {k: v for k, v in h.headers.items() if k.lower() in ('authorization', 'content-type', 'accept')}
        fut = asyncio.run_coroutine_threadsafe(self.client.request(method, path, headers=headers, content=body),
                                               self.loop)
        r = fut.result(90)
        tok = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
        self.answered.append((tok[4:], p, r.status_code))
        h.send_response(r.status_code)
        h.send_header('Content-Type', r.headers.get('content-type', 'application/json'))
        h.send_header('Content-Length', str(len(r.content)))
        h.end_headers()
        h.wfile.write(r.content)
        return True


NODE = None


@pytest.fixture(scope='module')
def ran(request):
    global NODE
    H.start()
    NODE = Node([_pubkey(FIRST_FILL + i) for i in range(len(SCENARIOS))])
    with pytest.MonkeyPatch.context() as mp:
        NODE.start(mp)
        try:
            check = run_scenarios(request, SCENARIOS, COST, FIRST_FILL)
        finally:
            NODE.stop()
    return check


def _pubs_by(pk, prefix):
    """Every kind-30078 this key SENT whose d-tag starts with `prefix` (the storage key's, for the node's documents)."""
    return [e for e in H.STATE['pubs'] if e.get('pubkey') == pk and H._d(e).startswith(prefix)]


async def confirm(b, seconds=20):
    """Press OK on the app's own confirm dialog (uiConfirm), as a person does."""
    await H.wait(b, "!!document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')", seconds)
    await b.js("document.querySelector('.uiconfirm-bg [data-uc=\"1\"]').click();true")


SET = """((sel,v)=>{const x=document.querySelector(sel);x.value=v;x.dispatchEvent(new Event('input',{bubbles:true}));
  x.dispatchEvent(new Event('change',{bubbles:true}));return true})"""


async def type_into(b, sel, value):
    await b.js(SET + '(%s,%s)' % (json.dumps(sel), json.dumps(value)))


async def click(b, sel):
    if not await b.js("!!document.querySelector(%s)" % json.dumps(sel)):
        raise AssertionError({'nothing to press': sel, 'screen': await b.js("(document.querySelector('#feed')||{}).innerText"),
                              'toasts': await b.js('__toasts')})
    await b.js("(()=>{const x=[...document.querySelectorAll(%s)].find(e=>e.offsetParent!==null)||document.querySelector(%s);"
               "x.click();return true})()" % (json.dumps(sel), json.dumps(sel)))


# =========================================================================================== NOTES

async def open_notes(b):
    await b.js(MEMBER)
    await b.js("__PC.switchView('notes')")
    await H.wait(b, "!!document.querySelector('.nt-wrap .nt-new')", 60)


async def write_note(b, title, body):
    """New note -> type a title and a body -> leave the field (saves now), as a person does. Returns its id."""
    await click(b, '.nt-wrap .nt-new')
    await H.wait(b, "!!document.querySelector('.nt-editor .nt-title') && "
                    "!document.querySelector('.nt-editor .nt-body').classList.contains('hidden')", 20)
    await type_into(b, '.nt-editor .nt-title', title)
    await type_into(b, '.nt-editor .nt-body', body)
    await b.js("document.querySelector('.nt-editor .nt-body').dispatchEvent(new Event('blur'));true")
    await H.wait(b, "/saved/.test(document.querySelector('.nt-editor .nt-state').textContent)", 30,
                 state="document.querySelector('.nt-editor .nt-state').textContent")
    return await b.js("PCNotes.current().id")


def _note_live(S, nid):
    """The home relay's newest copy of this note is a real one (not a tombstone)."""
    got = [e for e in S.docs('pcai:note:' + nid)]
    return got and got[0]['content']


ITEM = "[...document.querySelectorAll('.nt-results .nt-item')].find(x=>x.querySelector('.nt-title,b,strong,span')&&x.textContent.includes(%s))"


async def open_listed(b, title, seconds=60):
    await H.wait(b, '!!' + ITEM % json.dumps(title), seconds,
                 state="({list:(document.querySelector('.nt-results')||{}).innerText,toasts:__toasts,errors:__errors})")
    await b.js((ITEM % json.dumps(title)) + '.click();true')


SHOWN_TEXT = "(document.querySelector('.nt-editor .nt-render')||{}).innerText||''"


@scenario
async def a_note_opens_decrypted_on_a_fresh_device_and_follows_an_edit(S):
    a = await S.device()
    await open_notes(a)
    nid = await write_note(a, 'Shopping list', 'eggs and flour, v1')
    await S.until(a, lambda: _note_live(S, nid), 60, 'the note on the home relay')
    assert 'eggs and flour' not in S.docs('pcai:note:' + nid)[0]['content'], 'the note went out in plaintext'
    b = await S.device()
    await open_notes(b)
    await open_listed(b, 'Shopping list')
    await H.wait(b, SHOWN_TEXT + ".includes('eggs and flour, v1')", 30, state=SHOWN_TEXT)
    # An edit on A, while B has the note open: B shows the new text without a reload.
    before = S.docs('pcai:note:' + nid)[0]['id']
    await type_into(a, '.nt-editor .nt-body', 'eggs and flour and milk, v2')
    await a.js("document.querySelector('.nt-editor .nt-body').dispatchEvent(new Event('blur'));true")
    await S.until(a, lambda: S.docs('pcai:note:' + nid)[0]['id'] != before, 60, 'the edit on the home relay')
    await H.wait(b, "(document.querySelector('.nt-results')||{}).innerText.includes('v2')", 60,
                 state="(document.querySelector('.nt-results')||{}).innerText")
    await open_listed(b, 'Shopping list')
    await H.wait(b, SHOWN_TEXT + ".includes('milk, v2')", 30, state=SHOWN_TEXT)
    assert not no_errors(await b.js('__errors'))


@CHROME
def test_a_note_opens_decrypted_on_a_fresh_device_and_follows_an_edit(ran):
    ran('a_note_opens_decrypted_on_a_fresh_device_and_follows_an_edit')


FOLDER_ROW = "!!document.querySelector('.nt-folder[data-f=\"Recipes\"]')"


@scenario
async def a_notes_folder_and_the_note_in_it_reach_a_fresh_device(S):
    a = await S.device()
    await open_notes(a)
    await click(a, '.nt-addfolder')
    await H.answer_prompt(a, 'Recipes')
    await H.wait(a, FOLDER_ROW, 20)
    await S.until(a, lambda: [e for e in S.docs('pcai:notefolder:') if e['content']], 60, 'the folder on the home relay')
    await click(a, '.nt-folder[data-f="Recipes"]')
    nid = await write_note(a, 'Pancakes', 'two eggs, a cup of flour')
    await S.until(a, lambda: _note_live(S, nid), 60, 'the note on the home relay')
    b = await S.device()
    await open_notes(b)
    await H.wait(b, FOLDER_ROW, 60, state="(document.querySelector('.nt-folders')||{}).innerText")
    await click(b, '.nt-folder[data-f="Recipes"]')
    await open_listed(b, 'Pancakes')
    await H.wait(b, SHOWN_TEXT + ".includes('a cup of flour')", 30, state=SHOWN_TEXT)
    assert await b.js("document.querySelectorAll('.nt-results .nt-item').length") == 1
    assert await b.js("document.querySelector('.nt-editor .nt-folder-sel').selectedOptions[0].textContent") == 'Recipes'


@CHROME
def test_a_notes_folder_and_the_note_in_it_reach_a_fresh_device(ran):
    ran('a_notes_folder_and_the_note_in_it_reach_a_fresh_device')


DOOMED = ['Doomed draft %d' % i for i in range(1, 5)]


@scenario
async def a_deleted_note_stays_deleted_after_the_offline_queue_flushes(S):
    a = await S.device()
    await open_notes(a)
    # ONE SECOND, as a person who saves and deletes quickly has it: every event below carries the same created_at.
    # A tie between a note and its tombstone is then settled by which copy a cache happens to keep (NIP-01: the
    # lower id, i.e. a coin toss), so FOUR notes go through it -- one that comes back is the failure.
    await a.js("(()=>{const t=Date.now();window.__realNow=Date.now;Date.now=()=>t;return true})()")
    # Written while every relay REFUSES them: kept on this device and queued to send later.
    H.STATE['refuse_docs'].add((S.pk, 'pcai:note:'))
    doomed = []
    for title in DOOMED:
        doomed.append(await write_note(a, title, 'this text must not come back'))
        await click(a, '.nt-back')
    assert await a.js("PCNotes.pendingCount()") == len(DOOMED), 'the refused notes were not queued'
    H.STATE['refuse_docs'].discard((S.pk, 'pcai:note:'))
    keeper = await write_note(a, 'Keeper', 'this one stays')
    await S.until(a, lambda: _note_live(S, keeper), 60, 'the second note on the home relay')
    await click(a, '.nt-back')
    for title, nid in zip(DOOMED, doomed):
        await open_listed(a, title)
        await click(a, '.nt-editor .nt-del')
        await confirm(a)
        await S.until(a, lambda: S.docs('pcai:note:' + nid) and not S.docs('pcai:note:' + nid)[0]['content'],
                      60, 'the tombstone on the home relay')
    await a.js("(()=>{Date.now=window.__realNow;return true})()")
    sent = {nid: len(S.pubs('pcai:note:' + nid)) for nid in doomed}
    # Back online, the queue drains (a relay reconnect does this; here the same call, now).
    await a.js("PCNotes.flush()")
    await asyncio.sleep(2)
    for nid in doomed:
        again = [e for e in S.pubs('pcai:note:' + nid)[sent[nid]:] if e['content']]
        assert not again, 'a deleted note\'s old text was published again by the offline queue'
        assert not S.docs('pcai:note:' + nid)[0]['content'], 'the home relay holds a deleted note again'
    b = await S.device()
    await open_notes(b)
    await open_listed(b, 'Keeper')
    await asyncio.sleep(3)
    assert 'Doomed draft' not in await b.js("document.querySelector('.nt-results').innerText"), \
        'a deleted note is back on a fresh device'
    await S.open[0].reload()
    await open_notes(a)
    await open_listed(a, 'Keeper')
    # Not only at first paint: the relay's read has answered by now, and it holds the tombstones.
    await asyncio.sleep(4)
    assert 'Doomed draft' not in await a.js("document.querySelector('.nt-results').innerText"), \
        ('a deleted note came back on the device that deleted it', await a.js("document.querySelector('.nt-results').innerText"))


@CHROME
def test_a_deleted_note_stays_deleted_after_the_offline_queue_flushes(ran):
    ran('a_deleted_note_stays_deleted_after_the_offline_queue_flushes')


@scenario
async def notes_publish_nothing_on_open_when_nobody_answers(S):
    S.hold('pcai-notes')
    a = await S.device()
    await open_notes(a)
    await S.quiet_for(30)
    assert S.pubs('pcai:note:') == [] and S.pubs('pcai:notefolder:') == [], \
        'opening Notes over an unanswered read published a note document'
    # A note written now is ONE document of its own -- no read of the library decides it -- so it is kept.
    nid = await write_note(a, 'Written blind', 'kept, not lost')
    assert await a.js("!!PCNotes.current() && PCNotes.current().title==='Written blind'")
    H.STATE['hold_docs'].discard((S.pk, 'pcai-notes'))
    await S.until(a, lambda: _note_live(S, nid), 30,
                  'the note written while nobody answered the library read')
    b = await S.device()
    await open_notes(b)
    await open_listed(b, 'Written blind')


@CHROME
def test_notes_publish_nothing_on_open_when_nobody_answers(ran):
    ran('notes_publish_nothing_on_open_when_nobody_answers')


# =========================================================================================== ENCRYPTED DRIVE

SECRET_BYTES = bytes(range(256)) * 9 + b'tax return 2026 -- the exact bytes'


SHOWN_TOASTS = r"""(()=>{if(window.__shown)return true;window.__shown=[];const r=document.querySelector('#toast-root');
  new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>__shown.push(n.textContent||'')))).observe(r,{childList:true});
  return true})()"""   # every toast DRAWN -- files.js holds its own `toast`, which the harness hook never sees


async def open_drive(b):
    await b.js(SHOWN_TOASTS)
    await b.js("__PC.switchView('blossom')")
    await H.wait(b, "!!document.querySelector('#bl-newfolder')", 60,
                 state="({feed:(document.querySelector('#feed')||{}).innerText,toasts:__toasts})")


async def new_encrypted_folder(b, name):
    await click(b, '#bl-newfolder')
    await H.wait(b, "!!document.querySelector('#nf-name')", 20)
    await b.js("(n=>{document.querySelector('#nf-name').value=n;"
               "document.querySelector('input[name=\"nf-enc\"][value=\"1\"]').checked=true;"
               "document.querySelector('#nf-create').click();return true})(%s)" % json.dumps(name))


async def upload(b, name, data):
    await H.wait(b, "!!document.querySelector('#bl-file')", 30)
    await b.js("""((n,b64)=>{const bin=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
      const dt=new DataTransfer();dt.items.add(new File([bin],n,{type:'application/pdf'}));
      const i=document.querySelector('#bl-file');i.files=dt.files;i.dispatchEvent(new Event('change',{bubbles:true}));
      return true})(%s,%s)""" % (json.dumps(name), json.dumps(base64.b64encode(data).decode())))


def _index_of(pk):
    return H.STATE['indexes'].get(pk) or {}


def _indexed(pk, name):
    files = _index_of(pk).get('files') or {}
    return next((sha for sha, m in files.items() if (m or {}).get('name') == name), None)


FOLDER_TILE = "[...document.querySelectorAll('[data-folder=\"Tax papers\"]')].find(e=>e.offsetParent)"
CARD = "!!document.querySelector('#bl-grid .file-card[data-sha=\"%s\"]')"


@scenario
async def an_encrypted_drive_file_opens_to_the_exact_bytes_on_a_fresh_device(S):
    a = await S.device()
    await open_drive(a)
    await new_encrypted_folder(a, 'Tax papers')
    await upload(a, 'return.pdf', SECRET_BYTES)
    await S.until(a, lambda: _indexed(S.pk, 'return.pdf'), 90, 'the uploaded file in the drive index')
    sha = _indexed(S.pk, 'return.pdf')
    idx = _index_of(S.pk)
    assert 'Tax papers' in (idx.get('encFolders') or []), ('the folder is not an encrypted one', idx.get('encFolders'))
    assert idx['files'][sha].get('enc'), 'the file was uploaded in plaintext'
    assert b'tax return 2026' not in H.STATE['blobs'][sha], 'Blossom holds the plaintext'
    b = await S.device()
    await open_drive(b)
    await H.wait(b, '!!' + FOLDER_TILE, 60, state="(document.querySelector('#feed')||{}).innerText")
    await b.js(FOLDER_TILE + '.click();true')
    await H.wait(b, CARD % sha, 60, state="(document.querySelector('#bl-grid')||{}).innerText")
    assert await b.js("document.querySelector('#bl-grid .file-card[data-sha=\"%s\"]').classList.contains('enc')" % sha)
    got = await b.js("""(async()=>{const u=await __PC.encFileUrl(%s);const a=new Uint8Array(await (await fetch(u)).arrayBuffer());
      let s='';for(const x of a)s+=String.fromCharCode(x);return btoa(s)})()""" % json.dumps(sha))
    assert base64.b64decode(got) == SECRET_BYTES, 'the fresh device did not decrypt the exact bytes'
    # Deleted on A -> gone on another fresh device, and it stays gone.
    await a.js("document.querySelector('#bl-grid .file-card[data-sha=\"%s\"] button.del').click();true" % sha)
    await confirm(a)
    await S.until(a, lambda: sha not in (_index_of(S.pk).get('files') or {}), 60, 'the deletion in the drive index')
    c = await S.device()
    await open_drive(c)
    await H.wait(c, '!!' + FOLDER_TILE, 60)
    await c.js(FOLDER_TILE + '.click();true')
    await H.wait(c, "!!document.querySelector('#bl-grid .empty, #bl-grid .file-card')", 60)
    await asyncio.sleep(2)
    assert not await c.js(CARD % sha), 'the deleted file is listed on a fresh device'


@CHROME
def test_an_encrypted_drive_file_opens_to_the_exact_bytes_on_a_fresh_device(ran):
    ran('an_encrypted_drive_file_opens_to_the_exact_bytes_on_a_fresh_device')


@scenario
async def the_drive_index_is_not_written_when_its_read_is_not_answered(S):
    H.STATE['index_fail'].add(S.pk)
    a = await S.device()
    await open_drive(a)
    await new_encrypted_folder(a, 'Tax papers')
    await upload(a, 'return.pdf', SECRET_BYTES)
    await H.wait(a, "__shown.some(t=>/\\d+ added/.test(t))", 60, state='__shown')    # the upload has finished
    await asyncio.sleep(2)
    assert S.pk not in H.STATE['indexes'], 'the drive index was written over a read that was never answered'
    # It SAYS so -- never "saved" over a list it could not read -- and the folder made here is kept on this device
    # for the save that can happen once the read is answered.
    shown = await a.js('__shown')
    assert any("Couldn't read your folders" in t for t in shown), shown
    assert any('folder list NOT saved' in t for t in shown), shown
    assert await a.js("__PC.filesIdx().folders().includes('Tax papers')")


@CHROME
def test_the_drive_index_is_not_written_when_its_read_is_not_answered(ran):
    ran('the_drive_index_is_not_written_when_its_read_is_not_answered')


# =========================================================================================== CALENDAR

def _today_key():
    import datetime
    d = datetime.date.today()
    return d.isoformat()


def _weekly_ics():
    """A weekly lesson from the first of this month: the kind of event a phone syncs, imported as a .ics."""
    import datetime
    first = datetime.date.today().replace(day=1)
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\nUID:piano-weekly@test\r\n'
            'DTSTAMP:20260101T000000Z\r\nDTSTART:%sT120000Z\r\nDTEND:%sT130000Z\r\n'
            'RRULE:FREQ=WEEKLY;COUNT=6\r\nSUMMARY:Piano lesson\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
            % (first.strftime('%Y%m%d'), first.strftime('%Y%m%d')))


async def open_calendar(b, seconds=60):
    """Open Calendar and wait until it has heard the node's answer about the calendars (or given up on it) --
    the grid painted before that is the empty first paint, not an answer."""
    pk = await b.js('__PC.me().pubkey')
    asked = sum(1 for who, path, _ in NODE.answered if who == pk and path == '/api/calendar/calendars')
    await b.js("__PC.switchView('calendar')")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while sum(1 for who, path, _ in NODE.answered if who == pk and path == '/api/calendar/calendars') <= asked:
        assert loop.time() < deadline, ('the calendar never asked the node', await b.js('__toasts'))
        await asyncio.sleep(.3)
    await H.wait(b, "!document.querySelector('.cal-wrap .spinner') && "
                    "!!document.querySelector('.cal-wrap .cal-grid, .cal-wrap .ws-err, .cal-wrap .cal-off')", seconds,
                 state="({feed:(document.querySelector('#feed')||{}).innerText,toasts:__toasts})")
    await asyncio.sleep(.5)


CHIPS = "[...document.querySelectorAll('.cal-grid .cal-chip')].filter(c=>c.title===%s).length"


@scenario
async def a_calendar_event_and_a_repeating_one_show_on_a_fresh_device(S):
    a = await S.device()
    await open_calendar(a)
    await click(a, '#cal-new')                           # no calendar yet: it asks for one first
    await H.wait(a, "!!document.querySelector('#cnew-name')", 20)
    await type_into(a, '#cnew-name', 'Home')
    await click(a, '#cnew-save')
    await H.wait(a, "!!document.querySelector('#cal-pick')", 60, state='__toasts')
    await click(a, '#cal-new')
    await H.wait(a, "!!document.querySelector('#cev-title')", 20)
    await type_into(a, '#cev-title', 'Dentist')
    await type_into(a, '#cev-date', _today_key())
    await click(a, '#cev-save')
    await H.wait(a, (CHIPS % json.dumps('Dentist')) + '>=1', 60, state='__toasts')
    # The repeating one arrives the way a phone's calendar does: an .ics, imported through the menu.
    await click(a, '#cal-menu')
    await H.wait(a, "!!document.querySelector('#cal-file')", 20)
    await a.js("""(t=>{const dt=new DataTransfer();dt.items.add(new File([t],'lessons.ics',{type:'text/calendar'}));
      const i=document.querySelector('#cal-file');i.files=dt.files;i.dispatchEvent(new Event('change',{bubbles:true}));
      return true})(%s)""" % json.dumps(_weekly_ics()))
    await H.wait(a, (CHIPS % json.dumps('Piano lesson')) + '>=4', 60, state='__toasts')
    assert all('Dentist' not in e['content'] for e in _pubs_by(NODE.storage_pk(S.pk), 'pcai:cal:')), \
        'the event went to the relay in plaintext'
    b = await S.device()
    await open_calendar(b)
    await H.wait(b, (CHIPS % json.dumps('Dentist')) + '===1', 60,
                 state="({toasts:__toasts,grid:(document.querySelector('.cal-wrap')||{}).innerText})")
    await H.wait(b, (CHIPS % json.dumps('Piano lesson')) + '>=4', 30,
                 state="({toasts:__toasts,grid:(document.querySelector('.cal-wrap')||{}).innerText})")
    assert await b.js(CHIPS % json.dumps('Piano lesson')) == await a.js(CHIPS % json.dumps('Piano lesson'))


@CHROME
def test_a_calendar_event_and_a_repeating_one_show_on_a_fresh_device(ran):
    ran('a_calendar_event_and_a_repeating_one_show_on_a_fresh_device')


@scenario
async def the_calendar_writes_nothing_and_keeps_its_copy_when_nobody_answers(S):
    a = await S.device()
    await open_calendar(a)
    await click(a, '#cal-new')
    await H.wait(a, "!!document.querySelector('#cnew-name')", 20)
    await type_into(a, '#cnew-name', 'Home')
    await click(a, '#cnew-save')
    await H.wait(a, "!!document.querySelector('#cal-pick')", 60, state='__toasts')
    await click(a, '#cal-new')
    await H.wait(a, "!!document.querySelector('#cev-title')", 20)
    await type_into(a, '#cev-title', 'Dentist')
    await type_into(a, '#cev-date', _today_key())
    await click(a, '#cev-save')
    await H.wait(a, (CHIPS % json.dumps('Dentist')) + '>=1', 60, state='__toasts')
    spk = NODE.storage_pk(S.pk)
    H.STATE['hold_docs'].add((spk, '*'))                # from here on, nobody answers the calendar's reads
    sent = len(_pubs_by(spk, 'pcai:cal'))
    # A device that HAS the calendar keeps showing it -- the saved copy, said to be one.
    await S.open[0].reload()
    await open_calendar(a, 90)
    await H.wait(a, (CHIPS % json.dumps('Dentist')) + '>=1', 60,
                 state="(document.querySelector('.cal-wrap')||{}).innerText")
    await H.wait(a, "!!document.querySelector('.cal-stale')", 60,
                 state="(document.querySelector('.cal-wrap')||{}).innerText")
    # A fresh device has nothing to show: it says it could not reach them, and offers nothing to write.
    b = await S.device()
    await open_calendar(b, 90)
    await H.wait(b, "!!document.querySelector('.cal-wrap .ws-err')", 60,
                 state="(document.querySelector('.cal-wrap')||{}).innerText")
    assert not await b.js("!!document.querySelector('#cal-new')"), 'a fresh device offered to start a calendar over an unanswered read'
    assert (S.pk, '/api/calendar/calendars', 503) in NODE.answered, [x for x in NODE.answered if x[0] == S.pk]
    assert _pubs_by(spk, 'pcai:cal')[sent:] == [], 'a calendar document was written over a read nobody answered'


@CHROME
def test_the_calendar_writes_nothing_and_keeps_its_copy_when_nobody_answers(ran):
    ran('the_calendar_writes_nothing_and_keeps_its_copy_when_nobody_answers')


# =========================================================================================== WALLET DOCUMENT

async def open_wallet(b, seconds=60):
    await b.js(MEMBER)
    await b.js("__PC.switchView('exodus')")
    await H.wait(b, "!!document.querySelector('.ex-wrap #ex-create, .ex-wrap #ex-reveal, .ex-wrap .empty')", seconds,
                 state="({feed:(document.querySelector('#feed')||{}).innerText,toasts:__toasts})")


async def receive_address(b, sym='ETH'):
    await b.js("document.querySelector('.ex-receive[data-sym=\"%s\"]').click();true" % sym)
    await H.wait(b, "!!document.querySelector('.ex-panel .ex-addr')", 60, state='__toasts')
    return await b.js("document.querySelector('.ex-panel .ex-addr').textContent")


@scenario
async def a_restored_wallet_is_the_same_wallet_on_a_fresh_device(S):
    a = await S.device()
    await open_wallet(a)
    assert await a.js("!!document.querySelector('#ex-create')"), await a.js("document.querySelector('#feed').innerText")
    await click(a, '#ex-restore')
    await type_into(a, '#ex-phrase', TEST_PHRASE)
    await click(a, '#ex-restore-go')
    await H.wait(a, "!!document.querySelector('.ex-wrap #ex-reveal')", 90, state='__toasts')
    here = await receive_address(a)
    spk = NODE.storage_pk(S.pk)
    wallet_docs = _pubs_by(spk, 'pcai:exodus')
    assert wallet_docs, 'the wallet document never reached the relay'
    assert all('abandon' not in e['content'] for e in wallet_docs), 'the phrase went to the relay in the clear'
    b = await S.device()
    await open_wallet(b)
    assert await b.js("!!document.querySelector('.ex-wrap #ex-reveal') && !document.querySelector('#ex-create')"), \
        await b.js("document.querySelector('#feed').innerText")
    assert await receive_address(b) == here, 'the fresh device shows a different wallet'


@CHROME
def test_a_restored_wallet_is_the_same_wallet_on_a_fresh_device(ran):
    ran('a_restored_wallet_is_the_same_wallet_on_a_fresh_device')


@scenario
async def no_wallet_is_offered_or_written_when_nobody_answers(S):
    spk = NODE.storage_pk(S.pk)
    H.STATE['hold_docs'].add((spk, '*'))
    a = await S.device()
    await open_wallet(a, 120)
    shown = await a.js("document.querySelector('#feed').innerText")
    assert await a.js("!document.querySelector('#ex-create') && !document.querySelector('#ex-restore')"), \
        ('offered to create a wallet over a read nobody answered', shown)
    assert 'unavailable' in shown, shown
    # ...and that is the node's own strict read refusing (503), not some other failure.
    assert (S.pk, '/api/wallet/exodus/wallets', 503) in NODE.answered, [x for x in NODE.answered if x[0] == S.pk]
    assert _pubs_by(spk, 'pcai:exodus') == []


@CHROME
def test_no_wallet_is_offered_or_written_when_nobody_answers(ran):
    ran('no_wallet_is_offered_or_written_when_nobody_answers')
