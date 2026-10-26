"""Web Search, Code and Office, driven end to end in the shipped client against the node's REAL routers.

  websearch  app/routers/websearch.py -> search_service.search_page -> a SearXNG over HTTP (`Searx`, answering
             SearXNG 1.x's JSON: results, answers, suggestions, unresponsive_engines). The results render with
             their titles, snippets and the instant answer; More results asks for page 2; leaving and coming back
             repaints them without asking again. A SearXNG whose engines are all suspended, a node answering 503
             and a slow node each end in what happened -- never "No results", never a spinner for ever.
             (The AI overview and per-link summaries are AI and out of scope; reading a result in-app fetches an
             arbitrary web page through the SSRF guard and is covered by test_websearch_reader_buttons_full_app.)
  code       app/routers/code.py over a member's own workspace (a temporary directory): the tree lists the
             files, a file opens, an edit is saved to the disk, and a FRESH device opens the saved text. A node
             that cannot list the folder says so; a save the node refuses (503) keeps the edit in the tab,
             marked unsaved, and saving again once the node answers writes it; a file changed on disk since it
             was opened is not overwritten (409).
  office     New document -> the real /client/office/blank route -> stored on the drive (Blossom + the encrypted
             index) -> a FRESH device of the account lists it in Files with its bytes a real ODF package. Opening
             it on a node with no office editor says so; a node that cannot make the blank document says so and
             nothing is added to the drive. (EDITING inside Collabora is not testable here: no CODE server.)

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/code.js=...".
"""
import json
import os
import shutil
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('websearch', 'code', 'office')
FIRST_FILL = 170
SCENARIOS = {}
COST = {'a_document_made_in_office_is_on_the_drive_of_a_fresh_device': 30}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------------ SearXNG
class Searx:
    """SearXNG's /search?format=json, 1.x shape. A query containing 'suspended' is the morning every engine is
    rate-limited (HTTP 200, no results, every engine listed unresponsive) -- SearXNG's own way of saying so."""

    def __init__(self):
        self.asked = []
        sx = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                query, page = q.get('q', [''])[0], int(q.get('pageno', ['1'])[0])
                sx.asked.append((query, page))
                if 'suspended' in query:
                    body = {'query': query, 'results': [], 'answers': [], 'suggestions': [],
                            'unresponsive_engines': [['duckduckgo', 'Suspended: too many requests'],
                                                     ['brave', 'Suspended: access denied']]}
                else:
                    body = {'query': query, 'number_of_results': 40, 'answers': (
                                [{'answer': 'The %s is a small marsupial.' % query.split()[0], 'url': 'https://en.example.org/'}]
                                if page == 1 else []),
                            'suggestions': [query + ' habitat'], 'unresponsive_engines': [],
                            'results': [{'url': 'https://site%d.example.org/%s/%d' % (i, query.replace(' ', '-'), page),
                                         'title': '%s — page %d result %d' % (query.title(), page, i),
                                         'content': 'All about the %s, part %d.' % (query, i), 'engine': 'duckduckgo',
                                         'publishedDate': None} for i in range(1, 11)]}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.http.server_address[1]


# ------------------------------------------------------------------------------------------------ the node
class Ctx:
    def __init__(self, n):
        from app.routers import code, office, websearch
        from app.services import node_service, search_service, settings_store
        self.mp = mp = pytest.MonkeyPatch()
        self.searx = Searx()
        mp.setattr(search_service, '_configured_url', lambda: self.searx.url)
        mp.setattr(search_service, 'search_enabled', lambda: True)
        # Code: every member edits their own workspace under a temporary base (never the node's checkout).
        self.work = tempfile.mkdtemp(prefix='pc-code-ws-')
        get = settings_store.get
        mp.setattr(settings_store, 'get', lambda k, *a, **kw: self.work if k == 'code_user_root' else get(k, *a, **kw))
        mp.setattr(node_service, 'user_allowed', lambda db, user: False)
        # Office: the module as a node WITHOUT an office editor runs it (POSTERCHANAI_OFFICE unset -> disabled).
        mp.setattr(office, '_ROOT', __import__('pathlib').Path(tempfile.mkdtemp(prefix='pc-office-')))
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(websearch.router, ''), (code.router, ''), (office.router, '')], self.users,
                             paths=['/api/websearch/', '/api/code/', '/client/office/'])

    def workspace(self, pk):
        return os.path.join(self.work, str(self.users[pk].id))

    def stop(self):
        self.api.stop()
        self.searx.http.shutdown()
        self.mp.undo()
        shutil.rmtree(self.work, ignore_errors=True)


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------------ websearch

TITLES = "[...document.querySelectorAll('#ws-results .ws-title')].map(a=>a.textContent)"


async def web_search(d, q):
    await d.b.js("__PC.switchView('websearch');true")
    await F.wait(d, "!!document.querySelector('#ws-q') && !!document.querySelector('#ws-form')", 30, 'Web Search')
    await d.b.js("""(q=>{const i=document.querySelector('#ws-q');i.value=q;i.dispatchEvent(new Event('input',{bubbles:true}));
      document.querySelector('#ws-form').requestSubmit();return true})(%s)""" % json.dumps(q))


@scenario
async def a_web_search_lists_results_pages_on_and_survives_leaving(S):
    sx = S.ctx.searx
    d = await S.device(0)
    q = 'quokka %d' % S.base
    await web_search(d, q)
    await F.wait(d, "%s.length===10" % TITLES, 30, 'ten results')
    titles = await d.b.js(TITLES)
    assert titles[0] == '%s — page 1 result 1' % q.title(), titles
    text = await d.b.js("document.querySelector('#ws-results').innerText")
    assert 'All about the %s, part 1.' % q in text and 'The quokka is a small marsupial.' in text, text
    await F.wait(d, "!!document.querySelector('#ws-more')", 10, 'the More results button')
    await d.b.js("document.querySelector('#ws-more').click();true")
    await F.wait(d, "%s.length===20" % TITLES, 30, 'More results added page 2')
    assert (q, 2) in sx.asked
    asked = len([a for a in sx.asked if a[0] == q])
    await d.b.js("__PC.switchView('home');true")
    await d.b.js("__PC.switchView('websearch');true")
    await F.wait(d, "%s.length===20" % TITLES, 20, 'the results repainted on return')
    assert len([a for a in sx.asked if a[0] == q]) == asked, 'coming back searched again'


@CHROME
def test_a_web_search_lists_results_pages_on_and_survives_leaving(ran):
    ran('a_web_search_lists_results_pages_on_and_survives_leaving')


@scenario
async def a_search_nobody_could_answer_says_what_happened(S):
    api, tag = S.ctx.api, 'ws%d' % S.base
    d = await S.device(0, tag=tag)
    # Every engine suspended: SearXNG says 200 with nothing -- the screen must not read that as "no results".
    await web_search(d, 'suspended wombat %d' % S.base)
    await F.wait(d, "!!document.querySelector('#ws-results .ws-err')", 30, 'the suspended engines named')
    err = await d.b.js("document.querySelector('#ws-results .ws-err').textContent")
    assert 'no search engine answered' in err and 'duckduckgo' in err, err
    # A pasted paragraph (over the node's 300-character limit): a sentence, never "[object Object]".
    await web_search(d, ('a very long pasted paragraph about bandicoots %d ' % S.base) * 8)
    await F.wait(d, "!!document.querySelector('#ws-results .ws-err')", 30, 'the over-long query refused')
    err = await d.b.js("document.querySelector('#ws-results .ws-err').textContent")
    assert 'object Object' not in err and 'too long' in err, err
    # The node itself answering 503.
    api.fail('/api/websearch/search', 503, tag)
    await web_search(d, 'numbat %d' % S.base)
    await F.wait(d, "!!document.querySelector('#ws-results .ws-err')", 30, 'the 503 said')
    assert 'No results' not in await d.b.js("document.querySelector('#ws-results').innerText")
    # Slow (3 s): a spinner, then the results -- the answer is not lost.
    api.fail('/api/websearch/search', 3, tag)
    await web_search(d, 'bilby %d' % S.base)
    await F.wait(d, "!!document.querySelector('#ws-results .spinner')", 10, 'the spinner while it waits')
    await F.wait(d, "%s.length===10" % TITLES, 30, 'the slow answer drawn')
    api.heal('/api/websearch/search', tag)


@CHROME
def test_a_search_nobody_could_answer_says_what_happened(ran):
    ran('a_search_nobody_could_answer_says_what_happened')


# ------------------------------------------------------------------------------------------------ code

async def open_code(d):
    await d.b.js("__PC.switchView('code');true")
    await F.wait(d, "!!document.querySelector('#pcc-tree') && (!!document.querySelector('#pcc-tree .pcc-item')"
                    " || !!document.querySelector('#pcc-tree .pcc-note'))", 40, 'Code with its tree')


async def open_file(d, name):
    sel = "#pcc-tree .pcc-item[data-file=\"%s\"]" % name
    await F.wait(d, "!!document.querySelector(%s)" % json.dumps(sel), 20, '%s in the tree' % name)
    await d.b.js("document.querySelector(%s).click();true" % json.dumps(sel))
    await F.wait(d, "!!document.querySelector('#pcc-ta') && document.querySelector('.pcc-tab.on') && "
                    "document.querySelector('.pcc-tab.on').textContent.includes(%s)" % json.dumps(name), 20, '%s open' % name)


async def type_text(d, text):
    await d.b.js("""(t=>{const ta=document.querySelector('#pcc-ta');ta.focus();ta.value=t;
      ta.setSelectionRange(t.length,t.length);ta.dispatchEvent(new Event('input',{bubbles:true}));return true})(%s)"""
                 % json.dumps(text))


SAVED = "document.querySelector('#pcc-status').textContent"


@scenario
async def a_file_edited_in_code_is_saved_and_a_fresh_device_opens_it(S):
    pk = S.pk(0)
    ws = S.ctx.workspace(pk)
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, 'hello.py'), 'w') as f:
        f.write('print("hello")\n')
    with open(os.path.join(ws, 'notes.md'), 'w') as f:
        f.write('# notes\n')
    d = await S.device(0)
    await open_code(d)
    names = await d.b.js("[...document.querySelectorAll('#pcc-tree .pcc-item')].map(x=>x.textContent.trim())")
    assert any('hello.py' in n for n in names) and any('notes.md' in n for n in names), names
    await open_file(d, 'hello.py')
    assert await d.b.js("document.querySelector('#pcc-ta').value") == 'print("hello")\n'
    await type_text(d, 'print("hello, world")\n')
    await F.wait(d, "document.querySelector('.pcc-tab.on').classList.contains('dirty')", 10, 'the tab marked unsaved')
    await d.b.js("document.querySelector('#pcc-save').click();true")
    await F.wait(d, "/Saved hello.py/.test(%s)" % SAVED, 20, 'saved')
    assert open(os.path.join(ws, 'hello.py')).read() == 'print("hello, world")\n'

    d2 = await S.device(0)                                  # a fresh device
    await open_code(d2)
    await open_file(d2, 'hello.py')
    assert await d2.b.js("document.querySelector('#pcc-ta').value") == 'print("hello, world")\n'


@CHROME
def test_a_file_edited_in_code_is_saved_and_a_fresh_device_opens_it(ran):
    ran('a_file_edited_in_code_is_saved_and_a_fresh_device_opens_it')


@scenario
async def a_node_that_cannot_answer_never_costs_an_edit(S):
    api, tag, pk = S.ctx.api, 'code%d' % S.base, S.pk(0)
    ws = S.ctx.workspace(pk)
    os.makedirs(ws, exist_ok=True)
    path = os.path.join(ws, 'plan.txt')
    with open(path, 'w') as f:
        f.write('step one\n')
    # The node does not answer at all: Code says it could not reach it and offers Try again -- which asks again.
    api.fail('/api/code/config', 503, tag)
    api.fail('/api/code/tree', 503, tag)
    d = await S.device(0, tag=tag)
    await d.b.js("__PC.switchView('code');true")
    await F.wait(d, "!!document.querySelector('#pcc-gate-retry')", 40, 'Try again on an unreachable node')
    api.heal('/api/code/config', tag)
    await d.b.js("document.querySelector('#pcc-gate-retry').click();true")
    # Then it cannot list the folder: Code says so -- never an empty workspace, never a spinner for ever. (Waited for
    # in place: re-entering the screen retries by itself, which would pass without the button doing anything.)
    await F.wait(d, "!!document.querySelector('#pcc-tree .pcc-note')", 20, 'the tree read after Try again')
    note = await d.b.js("(document.querySelector('#pcc-tree .pcc-note')||{}).textContent||''")
    assert note and 'empty' not in note.lower(), note
    api.heal('/api/code/tree', tag)
    await d.b.js("document.querySelector('#pcc-tree-retry').click();true")
    await F.wait(d, "!!document.querySelector('#pcc-tree .pcc-item')", 20, 'the tree after Try again')
    await open_file(d, 'plan.txt')
    await type_text(d, 'step one\nstep two\n')
    # The save is refused: the edit stays in the tab, unsaved, and the file on disk is untouched.
    api.fail('/api/code/file', 503, tag)
    await d.b.js("document.querySelector('#pcc-save').click();true")
    await F.wait(d, "/Could not save/.test(%s)" % SAVED, 20, 'the refused save said so')
    assert await d.b.js("document.querySelector('#pcc-ta').value") == 'step one\nstep two\n'
    assert await d.b.js("document.querySelector('.pcc-tab.on').classList.contains('dirty')")
    assert open(path).read() == 'step one\n'
    api.heal('/api/code/file', tag)
    await d.b.js("document.querySelector('#pcc-save').click();true")
    await F.wait(d, "/Saved plan.txt/.test(%s)" % SAVED, 20, 'the second save')
    assert open(path).read() == 'step one\nstep two\n'
    # Somebody changes it on disk (the terminal under the editor); this tab's next save must not overwrite it.
    with open(path, 'w') as f:
        f.write('rewritten in the terminal\n')
    later = os.path.getmtime(path) + 5
    os.utime(path, (later, later))                          # its own mtime, whatever the clock's resolution
    await type_text(d, 'step one\nstep two\nstep three\n')
    await d.b.js("document.querySelector('#pcc-save').click();true")
    await F.wait(d, "/changed on disk/.test(%s)" % SAVED, 20, 'the conflict named')
    assert open(path).read() == 'rewritten in the terminal\n'


@CHROME
def test_a_node_that_cannot_answer_never_costs_an_edit(ran):
    ran('a_node_that_cannot_answer_never_costs_an_edit')


# ------------------------------------------------------------------------------------------------ office

async def new_document(d, name, kind='spreadsheet'):
    await d.b.js("__PC.switchView('office');true")
    await F.wait(d, "!!document.querySelector('[data-office-new=\"%s\"]')" % kind, 30, 'the Office home')
    await d.b.js("document.querySelector('[data-office-new=\"%s\"]').click();true" % kind)
    await F.wait(d, "!!document.querySelector('#nd-name') && !!document.querySelector('#nd-create')", 15, 'New document')
    await d.b.js("(n=>{document.querySelector('#nd-name').value=n;document.querySelector('#nd-create').click();return true})(%s)"
                 % json.dumps(name))


DRIVE = """(async()=>{const FI=__PC.filesIdx();await FI.ensure();
  return Object.entries(FI.data.files||{}).map(([sha,m])=>({sha,name:m.name,mime:m.mime}));})()"""


@scenario
async def a_document_made_in_office_is_on_the_drive_of_a_fresh_device(S):
    import hashlib
    d = await S.device(0)
    await new_document(d, 'Budget %d' % S.base)
    name = 'Budget %d.ods' % S.base
    # This node has no office editor: opening the new document says so, in words.
    await F.wait(d, "(window.__toastLog||[]).some(t=>/office unavailable/i.test(t))", 30, 'the open said why it could not')
    toast = [t for t in await d.b.js('window.__toastLog') if 'office unavailable' in t.lower()][0]
    assert 'disabled' in toast or 'no office editor' in toast, toast

    # The same account, a fresh device -- opened once the first device's drive-index save has LANDED on the node (it
    # is saved on a 900 ms debounce; a device opened before that reads the index once and lists the state before the
    # document: measured 2/2 failing, deploy 70). The same wait test_office_real_collabora.py's scenarios use.
    pk = await d.b.js('__PC.me().pubkey')
    await F.until(lambda: name in {m.get('name') for m in ((R.H.STATE['indexes'].get(pk) or {}).get('files') or {}).values()
                                   if isinstance(m, dict)}, 30, "the new document on the node's drive index", d)
    d2 = await S.device(0)
    files = await d2.b.js(DRIVE)
    doc = [f for f in files if f['name'] == name]
    assert doc, ('the new document is not on the drive a fresh device reads', files)
    await d2.b.js("__PC.switchView('blossom');true")
    await F.wait(d2, "!!document.querySelector('.fx-home-tile[data-folder=\"\"]')", 30, 'the Files home')
    await d2.b.js("document.querySelector('.fx-home-tile[data-folder=\"\"]').click();true")
    await F.wait(d2, "/Showing \\d+ of/.test(document.querySelector('#feed').innerText) && "
                     "document.querySelector('#feed').innerText.includes(%s)" % json.dumps(name), 30,
                 'listed under All files')
    raw = R.H.STATE['blobs'].get(doc[0]['sha'])
    assert raw is not None and hashlib.sha256(raw).hexdigest() == doc[0]['sha']
    with zipfile.ZipFile(__import__('io').BytesIO(raw)) as z:
        assert z.read('mimetype') == b'application/vnd.oasis.opendocument.spreadsheet', z.namelist()


@CHROME
def test_a_document_made_in_office_is_on_the_drive_of_a_fresh_device(ran):
    ran('a_document_made_in_office_is_on_the_drive_of_a_fresh_device')


@scenario
async def a_node_that_cannot_make_the_document_adds_nothing(S):
    api, tag = S.ctx.api, 'off%d' % S.base
    api.fail('/client/office/blank/', 503, tag)
    try:
        d = await S.device(0, tag=tag)
        before = await d.b.js(DRIVE)
        await new_document(d, 'Never made %d' % S.base, 'text')
        await F.wait(d, "(window.__toastLog||[]).some(t=>/could not create it/i.test(t))", 30, 'the refusal said so')
        assert await d.b.js("!!document.querySelector('#nd-create') && !document.querySelector('#nd-create').disabled"), \
            'the dialog did not give the Create button back'
        after = await d.b.js(DRIVE)
        assert after == before, ('a document that was never made was added to the drive', after)
    finally:
        api.heal('/client/office/blank/', tag)


@CHROME
def test_a_node_that_cannot_make_the_document_adds_nothing(ran):
    ran('a_node_that_cannot_make_the_document_adds_nothing')
