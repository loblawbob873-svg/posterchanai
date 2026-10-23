"""OFFICE EDITING AGAINST THE REAL COLLABORA (CODE) POSTERCHAN SHIPS -- not a stub of it.

Every other office test stops at the WOPI host's door: they create a session, read its JSON, and fake the editor. That
left the one thing a person does with Office -- open a document, type into it, press Save, and find the words on the
drive -- untested end to end. Here:

  CODE      the Collabora_Online.AppImage scripts/install/office.sh downloads (tests/client/code_server.py: a PRIVATE
            copy on its own port, own HOME/TMPDIR, trusting only this module's instance as WOPI host; killed and its
            directory removed at the end, with an assertion that nothing of it is left running). Never the node's own
            posterchanai-office.service.
  the node  tests/client/realnode_harness.py (the shipped relay, the instance, Blossom, real Chrome per device) with
            app/routers/office.py mounted IN-PROCESS behind the instance: CODE calls the REAL CheckFileInfo / GetFile /
            PutFile / lock routes over HTTP, exactly as it calls a node.
  the door  /office-code/ on the instance is a reverse proxy to CODE (HTTP and the editing WebSocket), the job nginx
            does on a node (`location ^~ /office-code/`, prefix kept) -- so the editor is same-origin as the client.

Flows (each its own test):
  1+2  a .docx uploaded to the drive in Files -> clicked -> "Office document" -> CODE fetches it through
       CheckFileInfo/GetFile and the editor reports Document_Loaded to the page -> REAL key presses typed into the
       document (CDP Input.insertText into the editor frame) -> the app's Save button -> Action_Save -> CODE's PutFile
       -> the drive holds a NEW blob whose Word text has the typed words (read with python-docx) -> a FRESH device of
       the account opens the drive's file and CODE is served the new version.
  4    a PutFile the storage refuses (the node answers 503) -> the person is told it was NOT saved, never "no
       changes" or "saved"; the drive keeps the old version.
  5    the same document edited from two devices of one account (the drive is per account; nothing in the app shares
       one document between two people, so there is no co-editing to test): both saves land and neither edit is lost.
  6    CODE killed mid-session -> Save says the editor is not answering and the changes were not saved; opening a
       document then says the office server is unavailable.

The WOPI boundary itself (missing/forged/expired tokens, another person's file, ...) needs no CODE and is
tests/test_office_wopi_boundary.py.

WHERE THIS RUNS: the deploy gate's full pass (`deploy_regression_gate.py --full`, every tests/**/test_*.py) on a node
that has the AppImage. It is NOT on the desktop CI's required list and is not a *_full_app.py, because that runner has
no CODE -- here it skips when there is no AppImage, which the full pass allows. $PC_CODE_APPIMAGE points it at one.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/files.js=/tmp/old/files.js".
"""
import asyncio
import io
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from tests.client import code_server
from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

pytestmark = [pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required'),
              pytest.mark.skipif(not code_server.available(), reason='no Collabora_Online.AppImage on this machine')]

FIRST_FILL = 210
SCENARIOS = {}
# Cheapest LAST: F.run orders by cost, and the CODE-goes-down scenario must be the final one (it kills CODE).
COST = {'a_document_typed_into_is_saved_to_the_drive_and_a_fresh_device_opens_it': 50,
        'a_save_the_storage_refuses_is_said_and_the_drive_keeps_the_old_version': 40,
        'two_devices_editing_one_document_lose_neither_edit': 30,
        'collabora_going_down_mid_session_is_said_in_words': 1}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# --------------------------------------------------------------------------------------------- the node
class Ctx:
    def __init__(self, n):
        from app.routers import office
        self.mp = mp = pytest.MonkeyPatch()
        self.office = office
        self.work = tempfile.mkdtemp(prefix='pc-office-sessions-')
        mp.setattr(office, '_ROOT', Path(self.work))
        mp.setenv('POSTERCHANAI_OFFICE', '1')
        mp.setattr(office, '_exts_cache', None)
        office._LOCKS.clear()
        R.NODE['config']['office_enabled'] = True
        self.code = code_server.CodeServer(n.origin)
        t0 = time.monotonic()
        self.code.start()
        self.code_start = time.monotonic() - t0
        print('\n[collabora] private CODE on %s up in %.1fs' % (self.code.url, self.code_start))
        mp.setattr(office, '_CODE', self.code.url)
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(office.router, '')], self.users, paths=['/client/office/', '/wopi/'],
                             forward=('host', 'x-wopi-', 'x-cool-', 'x-lool-'))
        H.ROUTES.append(self.proxy)

    # The instance's /office-code/ -> CODE, as nginx does it: prefix kept, Host kept, the WebSocket spliced.
    def proxy(self, h, method, path, body):
        if not path.startswith(code_server.SERVICE_ROOT + '/'):
            return False
        if 'websocket' in (h.headers.get('Upgrade') or '').lower():
            h._splice_to(self.code.port)
            return True
        hop = {'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'upgrade', 'te', 'trailer'}
        headers = {k: v for k, v in h.headers.items() if k.lower() not in hop}
        try:
            with httpx.Client(timeout=60, trust_env=False) as c:
                r = c.request(method, self.code.url + path, headers=headers, content=body or None)
        except httpx.HTTPError:
            h.send_response(502)                    # what nginx answers when the editor is not there
            h.send_header('Content-Length', '0')
            h.end_headers()
            return True
        h.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in hop | {'content-length', 'content-encoding'}:
                h.send_header(k, v)
        h.send_header('Content-Length', str(len(r.content)))
        h.end_headers()
        if method != 'HEAD':
            try:
                h.wfile.write(r.content)
            except OSError:                         # the frame navigated away mid-asset
                pass
        return True

    def session_dirs(self):
        return {p.name: p for p in Path(self.work).iterdir() if p.is_dir()}

    def stop(self):
        try:
            H.ROUTES.remove(self.proxy)
        except ValueError:
            pass
        self.api.stop()
        try:
            self.code.stop()
        finally:
            self.mp.undo()
            shutil.rmtree(self.work, ignore_errors=True)


@pytest.fixture(scope='module')
def ran(request):
    t0 = time.monotonic()
    check = F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx, lanes=1)
    print('\n[collabora] module ran in %.1fs' % (time.monotonic() - t0))
    return check


# --------------------------------------------------------------------------------------------- documents
def docx(*paragraphs):
    import docx as python_docx
    d = python_docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def docx_text(data):
    import docx as python_docx
    return '\n'.join(p.text for p in python_docx.Document(io.BytesIO(data)).paragraphs)


# The page keeps every message the editor posts to it (Collabora's postMessage API) -- what the person's screen hears.
MSG_LOG = r'''
window.__officeMsgs=[];
addEventListener('message',e=>{try{let d=e.data;if(typeof d==='string')d=JSON.parse(d);
  if(d&&d.MessageId)__officeMsgs.push({id:d.MessageId,v:d.Values||null,at:Date.now()});}catch(_){}});
'''
DRIVE = """(async()=>{const FI=__PC.filesIdx();await FI.ensure();
  return Object.entries(FI.data.files||{}).map(([sha,m])=>({sha,name:m.name,folder:m.folder||''}));})()"""


async def device(S, i=0, tag=''):
    return await S.device(i, pre_init=MSG_LOG, tag=tag)


async def upload_to_drive(d, path):
    """Files -> All files -> choose files (the real <input type=file>), as a person uploads."""
    await d.b.js("__PC.switchView('blossom');true")
    await F.wait(d, "!!document.querySelector('.fx-home-tile[data-folder=\"\"]')", 30, 'the Files home')
    await d.b.js("document.querySelector('.fx-home-tile[data-folder=\"\"]').click();true")
    await F.wait(d, "!!document.querySelector('#bl-file')", 30, 'the upload control')
    doc = await d.b.call('DOM.getDocument', {'depth': -1})
    node = await d.b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': '#bl-file'})
    await d.b.call('DOM.setFileInputFiles', {'nodeId': node['nodeId'], 'files': [str(path)]})
    name = os.path.basename(path)
    await F.wait(d, "(async()=>{const FI=__PC.filesIdx();return Object.values(FI.data.files||{}).some(m=>m.name===%s)})()"
                 % json.dumps(name), 60, 'the upload on the drive')


async def open_from_files(d, name):
    """Files -> All files -> click the file -> the open-with sheet -> "Office document"."""
    await d.b.js("__PC.switchView('blossom');true")
    await F.wait(d, "!!document.querySelector('.fx-home-tile[data-folder=\"\"]') || !!document.querySelector('#bl-file')",
                 30, 'the Files screen')
    await d.b.js("(t=>{if(t)t.click();return true})(document.querySelector('.fx-home-tile[data-folder=\"\"]'))")
    card = "[...document.querySelectorAll('.file-card[data-sha] > a')].find(a=>(a.dataset.name||a.textContent).includes(%s))" \
           % json.dumps(name)
    await F.wait(d, "!!(%s)" % card, 40, 'the file listed in Files')
    await d.b.js("(%s).click();true" % card)
    await F.wait(d, "!!document.querySelector('[data-ow=office]')", 20, 'the open-with sheet offering Office')
    await d.b.js("document.querySelector('[data-ow=office]').click();true")
    await F.wait(d, "!!document.querySelector('.office-frame') && !!document.querySelector('#office-save')", 60,
                 'the office editor frame')


async def editor_loaded(d, after=0):
    await F.wait(d, "(window.__officeMsgs||[]).some(m=>m.id==='App_LoadingStatus'&&m.v&&m.v.Status==='Document_Loaded'"
                    "&&m.at>=%d)" % after, 120, 'Collabora said Document_Loaded')


async def type_in_editor(d, text):
    """Click into the document inside the editor frame and TYPE (CDP key input into the focused frame)."""
    rect = await d.b.js("(()=>{const r=document.querySelector('.office-frame').getBoundingClientRect();"
                        "return {x:r.left,y:r.top,w:r.width,h:r.height}})()")
    # CODE greets a browser it has not seen with an "Explore the new version" slideshow over the page (stored in
    # that browser's localStorage, so once per person) -- a person closes it first, with its x or Escape.
    await asyncio.sleep(1.0)
    for t in ('rawKeyDown', 'keyUp'):
        await d.b.call('Input.dispatchKeyEvent', {'type': t, 'key': 'Escape', 'code': 'Escape',
                                                  'windowsVirtualKeyCode': 27})
    await asyncio.sleep(.5)
    x, y = rect['x'] + rect['w'] / 2, rect['y'] + rect['h'] * 0.3
    for t in ('mousePressed', 'mouseReleased'):
        await d.b.call('Input.dispatchMouseEvent', {'type': t, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    await asyncio.sleep(1.0)
    # Ctrl+End: the end of the document, so the words go after what is there.
    for t in ('rawKeyDown', 'keyUp'):
        await d.b.call('Input.dispatchKeyEvent', {'type': t, 'key': 'End', 'code': 'End', 'windowsVirtualKeyCode': 35,
                                                  'modifiers': 2})
    await asyncio.sleep(.5)
    for t in ('rawKeyDown', 'keyUp'):
        await d.b.call('Input.dispatchKeyEvent', {'type': t, 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})
    for ch in text:                                     # one key at a time, each with its text: real typing
        await d.b.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'text': ch, 'unmodifiedText': ch, 'key': ch})
        await d.b.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': ch})
        await asyncio.sleep(.03)
    await asyncio.sleep(1.5)


def wopi_calls(S, method, sid, contents):
    p = '/wopi/files/%s%s' % (sid, '/contents' if contents else '')
    return [x for x in S.ctx.api.seen if x == (method, p)]


async def session_id(d):
    """The office session the page is showing (the iframe is named pc-office-<id>)."""
    name = await d.b.js("document.querySelector('.office-frame').name")
    return name.removeprefix('pc-office-')


async def drive_entry(d, name):
    files = await d.b.js(DRIVE)
    hits = [f for f in files if f['name'] == name]
    return hits


async def toasts_since(d, n):
    return (await d.b.js('window.__toastLog||[]'))[n:]


# --------------------------------------------------------------------------------------------- 1 + 2
@scenario
async def a_document_typed_into_is_saved_to_the_drive_and_a_fresh_device_opens_it(S):
    tmp = Path(tempfile.mkdtemp(prefix='pc-office-doc-'))
    try:
        name = 'Quarterly %d.docx' % S.base
        (tmp / name).write_bytes(docx('Quarterly report', 'Revenue went up.'))
        d = await device(S)
        await upload_to_drive(d, tmp / name)
        before = await drive_entry(d, name)
        assert len(before) == 1, before

        await open_from_files(d, name)
        sid = await session_id(d)
        await editor_loaded(d)
        # CODE asked the REAL WOPI routes, with the token, for this session.
        assert wopi_calls(S, 'GET', sid, False), ('no CheckFileInfo', S.ctx.api.seen[-12:])
        assert wopi_calls(S, 'GET', sid, True), ('no GetFile', S.ctx.api.seen[-12:])

        words = 'Typed in Collabora %d' % S.base
        await type_in_editor(d, words)
        n = len(await d.b.js('window.__toastLog||[]'))
        await d.b.js("document.querySelector('#office-save').click();true")
        await F.wait(d, "(window.__toastLog||[]).slice(%d).some(t=>/document saved|save failed|no changes|not saved/i.test(t))"
                     % n, 60, 'the Save answered')
        said = await toasts_since(d, n)
        assert any('document saved' in t for t in said), said
        assert wopi_calls(S, 'POST', sid, True), 'Save never reached PutFile'

        after = await drive_entry(d, name)
        assert len(after) == 1, ('the drive should hold ONE file of that name, the new version', after)
        assert after[0]['sha'] != before[0]['sha'], 'the drive still points at the old bytes'
        raw = R.H.STATE['blobs'].get(after[0]['sha'])
        assert raw is not None, 'the saved version is not in Blossom'
        text = docx_text(raw)
        assert words in text and 'Quarterly report' in text, text

        # Save does not close: the editor is still there, and a SECOND edit + Save replaces that version in place.
        assert await d.b.js("!!document.querySelector('.office-frame') && !!document.querySelector('#office-save')"), \
            'the editor was taken off the screen by its own Save'
        more = 'Second pass %d' % S.base
        await type_in_editor(d, more)
        said = await save(d)
        assert any('document saved' in t for t in said), said
        again = await drive_entry(d, name)
        assert len(again) == 1, ('a second Save left more than one file of that name on the drive', again)
        text = docx_text(R.H.STATE['blobs'][again[0]['sha']])
        assert words in text and more in text, text
        after = again

        # A FRESH device of the account opens the drive's file: CODE is handed the new version.
        d2 = await device(S)
        await open_from_files(d2, name)
        sid2 = await session_id(d2)
        assert sid2 != sid
        await editor_loaded(d2)
        served = (Path(S.ctx.work) / sid2 / 'document').read_bytes()
        assert words in docx_text(served), 'the fresh device opened the OLD version'
        assert wopi_calls(S, 'GET', sid2, True), 'CODE never fetched the fresh device\'s document'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_document_typed_into_is_saved_to_the_drive_and_a_fresh_device_opens_it(ran):
    ran('a_document_typed_into_is_saved_to_the_drive_and_a_fresh_device_opens_it')



async def save(d):
    """Press the app's Save; returns what the person was told."""
    n = len(await d.b.js('window.__toastLog||[]'))
    await d.b.js("document.querySelector('#office-save').click();true")
    await F.wait(d, "(window.__toastLog||[]).slice(%d).some(t=>/saved|save failed|no changes|not saved/i.test(t))" % n,
                 60, 'the Save answered')
    await F.wait(d, "!document.querySelector('#office-save').disabled", 30, 'Save given back')
    return await toasts_since(d, n)


async def new_doc_on_drive(S, d, name, *paras):
    tmp = Path(tempfile.mkdtemp(prefix='pc-office-doc-'))
    try:
        (tmp / name).write_bytes(docx(*paras))
        await upload_to_drive(d, tmp / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return (await drive_entry(d, name))[0]


# --------------------------------------------------------------------------------------------- 4
@scenario
async def a_save_the_storage_refuses_is_said_and_the_drive_keeps_the_old_version(S):
    name = 'Refused %d.docx' % S.base
    d = await device(S)
    old = await new_doc_on_drive(S, d, name, 'Kept as it was')
    await open_from_files(d, name)
    sid = await session_id(d)
    await editor_loaded(d)
    words = 'Never stored %d' % S.base
    await type_in_editor(d, words)
    route = '/wopi/files/%s/contents' % sid
    S.ctx.api.fail(route, 503)                           # the node refuses CODE's PutFile
    try:
        said = await save(d)
        assert wopi_calls(S, 'POST', sid, True) or any(p == route for p, _ in S.ctx.api.faulted), \
            'CODE never tried to store it'
        assert not any(('document saved' in t) or ('no changes' in t) for t in said), \
            ('a save that was refused was reported as fine', said)
        assert any('not saved' in t.lower() for t in said), ('the refusal was not said', said)
        now = await drive_entry(d, name)
        assert [e['sha'] for e in now] == [old['sha']], ('the drive moved off the old version', now)
        assert 'Kept as it was' in docx_text(R.H.STATE['blobs'][old['sha']])
    finally:
        S.ctx.api.heal(route)
        nseen = len(S.ctx.api.seen)
    # The node answers again. Collabora retries a refused upload by itself (measured: ~10-15 s later), and once that
    # has landed the same edit -- still in the editor -- saves to the drive.
    await F.until(lambda: ('POST', route) in S.ctx.api.seen[nseen:], 90, "Collabora's own retry of the upload", d)
    said = await save(d)
    assert any('document saved' in t for t in said), said
    now = await drive_entry(d, name)
    assert len(now) == 1 and words in docx_text(R.H.STATE['blobs'][now[0]['sha']])


def test_a_save_the_storage_refuses_is_said_and_the_drive_keeps_the_old_version(ran):
    ran('a_save_the_storage_refuses_is_said_and_the_drive_keeps_the_old_version')


# --------------------------------------------------------------------------------------------- 5
@scenario
async def two_devices_editing_one_document_lose_neither_edit(S):
    name = 'Shared %d.docx' % S.base
    a = await device(S)
    orig = await new_doc_on_drive(S, a, name, 'Agenda')
    b = await device(S)                                  # the same account on a second device
    await open_from_files(a, name)
    await editor_loaded(a)
    await open_from_files(b, name)
    await editor_loaded(b)
    await type_in_editor(a, 'Alpha from the laptop')
    await type_in_editor(b, 'Beta from the phone')
    said_a = await save(a)
    assert any('document saved' in t for t in said_a), said_a
    # The laptop's save is complete when the drive's index on the node names its new version.
    pk = await a.b.js('__PC.me().pubkey')
    await F.until(lambda: any(m.get('name') == name for m in
                              ((R.H.STATE['indexes'].get(pk) or {}).get('files') or {}).values()
                              if isinstance(m, dict)) and
                  [sha for sha, m in ((R.H.STATE['indexes'].get(pk) or {}).get('files') or {}).items()
                   if isinstance(m, dict) and m.get('name') == name] != [orig['sha']],
                  30, "the laptop's save on the node's drive index", a)
    said_b = await save(b)
    fresh = await device(S)
    files = [f for f in await fresh.b.js(DRIVE) if f['name'].startswith('Shared %d' % S.base)]
    texts = [docx_text(R.H.STATE['blobs'][f['sha']]) for f in files if f['sha'] in R.H.STATE['blobs']]
    assert any('Alpha from the laptop' in t for t in texts), ('the laptop\'s edit is gone from the drive', files, said_b)
    assert any('Beta from the phone' in t for t in texts) or any('not saved' in t.lower() or 'changed' in t.lower()
                                                                 for t in said_b), \
        ('the phone\'s edit is neither on the drive nor reported unsaved', files, said_b)


def test_two_devices_editing_one_document_lose_neither_edit(ran):
    ran('two_devices_editing_one_document_lose_neither_edit')


# --------------------------------------------------------------------------------------------- 6
@scenario
async def collabora_going_down_mid_session_is_said_in_words(S):
    name = 'Crash %d.docx' % S.base
    d = await device(S)
    old = await new_doc_on_drive(S, d, name, 'Before the crash')
    await open_from_files(d, name)
    await editor_loaded(d)
    await type_in_editor(d, 'Typed just before CODE died')
    S.ctx.code.kill()                                    # the editor server goes away, all of it
    said = await save(d)
    assert not any(('document saved' in t) or ('no changes' in t) for t in said), \
        ('CODE was down and the person was told all is well', said)
    assert any('not saved' in t.lower() for t in said), ('nothing said the edit was not saved', said)
    assert [e['sha'] for e in await drive_entry(d, name)] == [old['sha']]
    # Opening a document now says the office server is unavailable -- not a white frame.
    await d.b.js("document.querySelector('#office-close').click();true")
    n = len(await d.b.js('window.__toastLog||[]'))
    await open_from_files_no_editor(d, name)
    await F.wait(d, "(window.__toastLog||[]).slice(%d).some(t=>/office unavailable/i.test(t))" % n, 60,
                 'the open said the editor is unavailable')
    said = await toasts_since(d, n)
    assert any('unavailable' in t and ('office server' in t or 'editor' in t) for t in said), said
    assert not await d.b.js("!!document.querySelector('.office-frame')"), 'an editor frame opened onto nothing'


async def open_from_files_no_editor(d, name):
    await d.b.js("__PC.switchView('blossom');true")
    await F.wait(d, "!!document.querySelector('.fx-home-tile[data-folder=\"\"]') || !!document.querySelector('#bl-file')",
                 30, 'the Files screen')
    await d.b.js("(t=>{if(t)t.click();return true})(document.querySelector('.fx-home-tile[data-folder=\"\"]'))")
    card = "[...document.querySelectorAll('.file-card[data-sha] > a')].find(a=>(a.dataset.name||a.textContent).includes(%s))" \
           % json.dumps(name)
    await F.wait(d, "!!(%s)" % card, 40, 'the file listed in Files')
    await d.b.js("(%s).click();true" % card)
    await F.wait(d, "!!document.querySelector('[data-ow=office]')", 20, 'the open-with sheet offering Office')
    await d.b.js("document.querySelector('[data-ow=office]').click();true")


def test_collabora_going_down_mid_session_is_said_in_words(ran):
    ran('collabora_going_down_mid_session_is_said_in_words')
