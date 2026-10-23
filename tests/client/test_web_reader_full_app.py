"""The in-app web reader, driven in the SHIPPED client against the node's REAL websearch router and its real fetch
code, with a fake little internet behind it (tests/reader_fake_web.py: DNS for `*.reader-web.test`, the TCP dial,
and a stand-in for this node's own network that records every hit). Nothing about the fetch or the guard is stubbed.

  reader     Web Search -> a result -> "📄 Read here" (/api/websearch/read). The article reads as its text, and a
             page MADE of XSS payloads -- in its title, its paragraphs and its markup -- is shown as inert text: no
             payload runs in the app's origin, no element of the page's markup reaches the DOM.
  page view  /api/websearch/page (served to older clients, and reachable as a plain URL on our origin) loaded the
             way a frame loads it, from the app's origin: the article's text AND its picture render (through the
             asset proxy), and none of ~25 classic payloads runs -- script tags, every handler shape, javascript: /
             data: urls, srcdoc, SVG/MathML, meta refresh, forms, <base>, mXSS through <noscript>. Pressing the
             links and buttons in it does nothing either. Then the SAME markup again WITHOUT the CSP header (a blob
             document of the app's origin), so the sanitiser is proven on its own and not only behind the policy.
  failure    a result that redirects into this node's network says it was blocked (and nothing reached it); a 404
             says "page not found"; a site that never answers says it took too long.

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/websearch.js=...".
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import pytest

from tests import reader_fake_web as FW
from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('websearch',)
FIRST_FILL = 236
SCENARIOS = {}
COST = {'the_page_view_runs_no_scripts_in_the_apps_origin': 30}
D = FW.DOMAIN
NEWS = 'news' + D


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def P(name):
    """A payload body: if it ever runs, the app page records it AND the instance sees a beacon request."""
    return ("(top.__pwned=top.__pwned||[]).push('%s');fetch('/pwned-beacon/%s')" % (name, name))


ARTICLE_TEXT = ''.join('<p>Paragraph %d of the notice explains which crossing number %d changes and why.</p>' % (i, i)
                       for i in range(1, 16))
# What /read turns into TEXT: payloads written as text in the page (entities), so the extracted article contains them
# literally -- exactly what a hostile page would do to a reader that rendered its text as markup.
TEXT_PAGE = ("<!doctype html><html><head><title>Ferry &lt;svg onload=\"%s\"&gt; notice</title></head><body><main>"
             "<h1>Ferry timetable</h1><p>The first ferry leaves at 06:10 and the last at 23:40.</p>"
             "<p>Somebody on the harbour forum complained about the new early sailing and, to make a point about the "
             "website, pasted &lt;img src=x onerror=\"%s\"&gt; into the middle of a long and otherwise ordinary "
             "paragraph about waiting on the quay in the rain for a ferry that never seems to come on time at all.</p>"
             "<p>Another reply on the same thread quoted &lt;script&gt;%s&lt;/script&gt; and then went on at length "
             "about the cafe on the pier, the price of the return ticket and the gulls that steal the chips.</p>"
             "%s</main></body></html>") % (P('title'), P('text-img'), P('text-script'), ARTICLE_TEXT)

PAYLOADS = [
    '<script>%s</script>' % P('script'),
    '<script src="/evil.js"></script>',
    '<img src="x" onerror="%s">' % P('img-onerror'),
    '<svg onload="%s"><circle r="4"/></svg>' % P('svg-onload'),
    '<svg><script>%s</script></svg>' % P('svg-script'),
    '<svg width="200" height="30"><a id="svglink" xlink:href="javascript:%s"><text x="5" y="20">svg link</text></a></svg>'
    % P('svg-xlink'),
    '<svg><animate onbegin="%s" attributeName="x" dur="1s"></animate></svg>' % P('animate-onbegin'),
    '<a id="jslink" href="javascript:%s">plain javascript link</a>' % P('a-javascript'),
    '<a id="jslink2" href="java&#9;script:%s">tab-split javascript link</a>' % P('a-tab'),
    '<a id="jslink3" href="&#x6A;avascript:%s">entity javascript link</a>' % P('a-entity'),
    '<a id="datalink" href="data:text/html,&lt;script&gt;%s&lt;/script&gt;">data link</a>' % P('a-data'),
    '<a id="clicklink" href="#" onclick="%s">onclick link</a>' % P('onclick'),
    '<iframe srcdoc="&lt;script&gt;parent.%s&lt;/script&gt;"></iframe>' % P('srcdoc').replace('top.', 'parent.'),
    '<iframe src="javascript:%s"></iframe>' % P('iframe-js'),
    '<object data="javascript:%s"></object>' % P('object'),
    '<embed src="javascript:%s">' % P('embed'),
    '<details open ontoggle="%s"><summary>details</summary>x</details>' % P('details-ontoggle'),
    '<meta http-equiv="refresh" content="0;url=javascript:%s">' % P('meta-refresh'),
    '<form action="javascript:%s"><button id="formbtn">submit</button></form>' % P('form'),
    '<button id="fabtn" formaction="javascript:%s">formaction</button>' % P('formaction'),
    '<math><maction actiontype="statusline" xlink:href="javascript:%s">math</maction></math>' % P('math'),
    '<div style="background:url(javascript:%s)">css url</div>' % P('css-url'),
    '<input id="autofocus" autofocus onfocus="%s">' % P('input-onfocus'),
    '<video><source onerror="%s"></video>' % P('video-source-onerror'),
    '<noscript><p title="</noscript><img src=x onerror=%s>"></noscript>' % P('noscript-mxss').replace(' ', '/**/'),
    '<template><img src=x onerror="%s"></template>' % P('template'),
    '<body onload="%s">' % P('body-onload'),
]
XSS_PAGE = ('<!doctype html><html><head><title>Harbour notice</title><base href="javascript:%s//">'
            '<link rel="stylesheet" href="/site.css"></head><body onload="%s"><main><h1>Harbour notice</h1>'
            '<p id="lede">The harbour opens at dawn and the first ferry leaves at 06:10.</p>'
            '<img id="pic" src="/harbour.png" alt="the harbour">%s%s</main></body></html>'
            % (P('base'), P('body-onload2'), ''.join(PAYLOADS), ARTICLE_TEXT))


class Searx:
    """SearXNG's JSON answer: one result per query word that names a page on the fake internet."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = parse_qs(urlparse(self.path).query).get('q', [''])[0]
                path = q.split()[0]
                body = json.dumps({'query': q, 'answers': [], 'suggestions': [], 'unresponsive_engines': [],
                                   'results': [{'url': 'http://%s/%s' % (NEWS, path), 'title': 'Result for ' + path,
                                                'content': 'snippet', 'engine': 'duckduckgo'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.http.server_address[1]


class Ctx:
    def __init__(self, n):
        from app.routers import websearch
        from app.services import page_render, search_service
        self.n = n
        self.mp = mp = pytest.MonkeyPatch()
        self.web = web = FW.FakeWeb()
        web.install(mp)
        web.loopback_passthrough = True          # the node's relay, instance and searx stub live on loopback
        web.dns[NEWS] = [FW.PUBLIC[0]]
        web.page(NEWS, '/article', TEXT_PAGE)
        web.page(NEWS, '/xss', XSS_PAGE)
        web.page(NEWS, '/site.css', 'h1{color:rgb(1, 2, 3)}', ctype='text/css')
        web.page(NEWS, '/harbour.png', R.png((20, 120, 200), size=40), ctype='image/png')
        web.page(NEWS, '/gone', '<html><body>gone</body></html>', status=404)
        web.redirect(NEWS, '/sneaky', 'http://127.0.0.1:%d/secret' % web.internal_port)
        web.silent(NEWS, '/hang', seconds=40)
        self.searx = Searx()
        mp.setattr(search_service, '_configured_url', lambda: self.searx.url)
        mp.setattr(search_service, 'search_enabled', lambda: True)
        mp.setattr(search_service, 'own_media_hosts', lambda: set())
        mp.setattr(search_service, 'FETCH_DEADLINE', 4.0, raising=False)
        mp.setattr(page_render, 'render_page_text', lambda *a, **k: None)     # never a headless render here
        # The frame's own links/assets point back at this node; behind the harness the Host header is not forwarded.
        mp.setattr(websearch, '_self_base', lambda request: n.origin)
        self.users = F.members(FIRST_FILL, SCENARIOS)
        some = next(iter(self.users.values()))
        # An <iframe src> carries no Authorization header; the page view's own ticket auth is covered by
        # tests/test_websearch.py. Here the frame is admitted as a member, and the CONTENT is what is under test.
        self.api = F.Routers(mp, [(websearch.router, '')], self.users, paths=['/api/websearch/'],
                             overrides={websearch._member_page_viewer: lambda: some})

    def beacons(self):
        return [p for p in R.NODE['seen'] if 'pwned' in p or p == '/evil.js']

    def stop(self):
        self.api.stop()
        self.searx.http.shutdown()
        self.web.close()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


async def read_result(d, path):
    """Web Search -> search for `path` -> the one result's "📄 Read here"."""
    await d.b.js("__PC.switchView('websearch');true")
    await F.wait(d, "!!document.querySelector('#ws-q') && !!document.querySelector('#ws-form')", 30, 'Web Search')
    await d.b.js("""(q=>{const i=document.querySelector('#ws-q');i.value=q;i.dispatchEvent(new Event('input',{bubbles:true}));
      document.querySelector('#ws-form').requestSubmit();return true})(%s)""" % json.dumps(path))
    await F.wait(d, "!!document.querySelector('.ws-read[data-i=\"0\"]')", 30, 'the result')
    await d.b.js("window.__pwned=[];document.querySelector('.ws-read[data-i=\"0\"]').click();true")
    await F.wait(d, "!!document.querySelector('.ws-reader') && !document.querySelector('.ws-reader .spinner')", 40,
                 'the reader with its text (or its error)')


# ------------------------------------------------------------------------------------------------ the reader
@scenario
async def the_reader_shows_the_article_as_text_and_runs_nothing_from_it(S):
    d = await S.device(0)
    await read_result(d, 'article')
    await asyncio.sleep(1.5)
    # Nothing of the payloads is markup, and nothing ran...
    dom = await d.b.js("""(()=>{const r=document.querySelector('.ws-reader');return {
        img:r.querySelectorAll('img').length, svg:r.querySelectorAll('svg:not(.ic):not([class*=icon])').length,
        script:r.querySelectorAll('script').length,
        handlers:[...r.querySelectorAll('*')].filter(e=>[...e.attributes].some(a=>/^on/i.test(a.name))).length,
        pwned:window.__pwned}})()""")
    assert dom == {'img': 0, 'svg': 0, 'script': 0, 'handlers': 0, 'pwned': []}, dom
    assert S.ctx.beacons() == [], S.ctx.beacons()
    # ...and the article is there, the payloads with it as words on the screen.
    text = await d.b.js("document.querySelector('.ws-reader').innerText")
    assert '06:10' in text and '23:40' in text and 'Paragraph 3 of the notice' in text, text
    assert 'onerror=' in text and '<script>' in text, text
    assert S.ctx.web.internal_hits == []


@CHROME
def test_the_reader_shows_the_article_as_text_and_runs_nothing_from_it(ran):
    ran('the_reader_shows_the_article_as_text_and_runs_nothing_from_it')


# ------------------------------------------------------------------------------------------------ the page view
FRAME_CHECK = r"""(async (mode)=>{
  window.__pwned=[];
  const url='/api/websearch/page?url='+encodeURIComponent(%s);
  const f=document.createElement('iframe');f.style.cssText='width:900px;height:700px';f.id='pcframe-'+mode;
  if(mode==='csp'){ f.src=url; }
  else{ const html=await (await fetch(url)).text(); f.src=URL.createObjectURL(new Blob([html],{type:'text/html'})); }
  const loaded=new Promise(r=>f.onload=r); document.body.appendChild(f); await loaded;
  const doc=f.contentDocument, w=f.contentWindow;
  const pic=doc.querySelector('img#pic');
  for(let i=0;i<60 && !(pic && pic.complete && pic.naturalWidth);i++) await new Promise(r=>setTimeout(r,100));
  const before=String(w.location.href);
  const press=id=>{const el=doc.getElementById(id); if(el){ try{ el.click(); }catch(_){} }};
  for(const id of ['jslink','jslink2','jslink3','datalink','formbtn','fabtn']) press(id);
  const sv=doc.getElementById('svglink'); if(sv) sv.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
  const af=doc.getElementById('autofocus'); if(af) try{af.focus();}catch(_){}
  await new Promise(r=>setTimeout(r,1500));
  const pwnedBefore=window.__pwned.slice();
  press('clicklink');            // LAST: an in-page '#' link, which (rightly) reloads the page view with a fragment
  await new Promise(r=>setTimeout(r,1500));
  let after=''; try{ after=String(f.contentWindow.location.href); }catch(e){ after='cross-origin:'+e.name; }
  const d2=f.contentDocument;
  return { text:(d2&&d2.body?d2.body.innerText:'').slice(0,4000), pic: pic ? pic.naturalWidth : -1,
           h1color: d2 && d2.querySelector('h1') ? getComputedStyle(d2.querySelector('h1')).color : '',
           scripts: d2 ? d2.querySelectorAll('script').length : -1,
           handlers: d2 ? [...d2.querySelectorAll('*')].filter(e=>[...e.attributes].some(a=>/^on/i.test(a.name))).map(e=>e.tagName) : null,
           frames: d2 ? d2.querySelectorAll('iframe,object,embed,frame').length : -1,
           before, after, pwned: pwnedBefore.concat(window.__pwned) };
})(%s)"""


@scenario
async def the_page_view_runs_no_scripts_in_the_apps_origin(S):
    d = await S.device(0)
    url = 'http://%s/xss' % NEWS
    origin = S.n.origin
    for mode in ('csp', 'no-csp'):
        got = await d.b.js(FRAME_CHECK % (json.dumps(url), json.dumps(mode)))
        assert 'The harbour opens at dawn' in got['text'] and 'Paragraph 3 of the notice' in got['text'], json.dumps([mode, {k: v for k, v in got.items() if k != 'text'}])
        assert got['pic'] == 40, (mode, 'the article picture did not render through the asset proxy', got)
        assert got['h1color'] == 'rgb(1, 2, 3)', (mode, 'the page stylesheet did not apply', got)
        assert got['pwned'] == [], (mode, 'a payload RAN in the app origin', got['pwned'])
        assert got['scripts'] == 0 and got['handlers'] == [] and got['frames'] == 0, (mode, got)
        # Pressing the page's links keeps the frame on THIS node's page view (an in-page '#' link reloads it there
        # with the fragment) -- never the raw site, never a javascript:/data: document.
        assert got['after'].startswith(origin + '/api/websearch/page?url=' + quote(url, safe='')), \
            (mode, 'pressing a link navigated the frame somewhere else', got['before'], got['after'])
    await asyncio.sleep(0.5)
    assert S.ctx.beacons() == [], ('a payload phoned home', S.ctx.beacons())
    assert S.ctx.web.internal_hits == []


@CHROME
def test_the_page_view_runs_no_scripts_in_the_apps_origin(ran):
    ran('the_page_view_runs_no_scripts_in_the_apps_origin')


# ------------------------------------------------------------------------------------------------ failures
@scenario
async def the_reader_says_plainly_when_a_page_cannot_be_read(S):
    d = await S.device(0)
    err = "(document.querySelector('.ws-reader .ws-err')||{}).textContent||''"
    await read_result(d, 'sneaky')
    msg = await d.b.js(err)
    assert 'blocked' in msg.lower() and 'not allowed' in msg.lower(), msg
    assert S.ctx.web.internal_hits == [], 'the redirect reached this node\'s own network'
    await d.b.js("document.querySelector('#ws-back').click();true")
    await read_result(d, 'gone')
    msg = await d.b.js(err)
    assert 'page not found' in msg.lower() and '404' in msg, msg
    await d.b.js("document.querySelector('#ws-back').click();true")
    await read_result(d, 'hang')
    msg = await d.b.js(err)
    assert 'took too long' in msg.lower(), msg
    assert 'nothing readable' not in msg


@CHROME
def test_the_reader_says_plainly_when_a_page_cannot_be_read(ran):
    ran('the_reader_says_plainly_when_a_page_cannot_be_read')
