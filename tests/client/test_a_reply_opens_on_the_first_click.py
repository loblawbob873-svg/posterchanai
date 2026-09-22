"""OPENING A POST MUST TAKE ONE CLICK, WHETHER OR NOT SOCIAL IS ALREADY OPEN.

Reported on the windowed desktop: "clicking on a reply on desktop has to be done twice now to bring
up the reply in Social", with the discriminating half supplied unprompted — "if social is not open,
it opens up the reply fine".

That pair is the whole specification. The post-opening path is the same call either way
(`openThread` → `PCOS.openDoc('post:<id>')` → `openApp`), so the only thing that differs is whether
a Social window already exists when it runs. A test that opens a post from a clean desktop passes
and says nothing about the bug; the state has to be set up first, which is exactly the class of
failure `feedback_test_the_state_the_code_ends_in` is about.

  cold    no Social window → the post opens on the first call
  warm    a Social window already open → the post STILL opens on the first call

Both drive the real client, the real desktop (PCOS) and the shipped renderers.

WHAT THIS DOES **NOT** COVER, AND WHY THE REPORTED BUG IS STILL OPEN. Both cases PASS here, so the
in-page window path is not where the double click comes from. On PosterChanOS a view is a REAL
COMPOSITOR TOPLEVEL (`PCOSWin.open`, the `_openedReal` branch in os.js `openApp`), and os.js says so
itself: "Invisible on the web, where PCOSWin is never enabled and this branch cannot run, which is
why every browser-driven check passes." A headless browser cannot make one, so this file guards the
half that works and is blind to the half that is broken. Closing that needs either a PCOSWin stub
driven through the same assertions, or a check that runs against the real desktop's shell. Kept
because the in-page path is a real regression surface — not because it proves the report is fixed.
"""
import asyncio
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT
from tests.client_source import client_source

# What the desktop holds after one attempt to open a post.
PROBE = """(() => {
  // What matters is not that a frame appeared — it is whether the POST is in it. The first probe
  // measured window identity (data-view) and the client's VIEW, neither of which these frames
  // carry, so it reported the working cold case as broken too.
  const frames = [...document.querySelectorAll('.osw')];
  const post = frames.find(f => /Post/i.test((f.querySelector('.osw-title') || {}).textContent || ''));
  const text = post ? (post.innerText || '') : '';
  return {
    windows: frames.length,
    titles: frames.map(f => ((f.querySelector('.osw-title') || {}).textContent || '').trim()),
    postWindow: !!post,
    // the seeded note bodies are "reply test N"
    showsPost: /reply test/i.test(text),
    spinner: !!(post && post.querySelector('.spinner')),
    empty: post ? text.replace(/\s+/g, '').length < 40 : true,
    sample: text.replace(/\s+/g, ' ').slice(0, 180),
  };
})()"""


# THE POPPED-OUT PATH, WHICH NO BROWSER TEST COULD REACH UNTIL NOW.
#
# On PosterChanOS a view is a real compositor toplevel: os.js `openApp` calls `popOutView`, and when
# that succeeds it sets `_openedReal` and returns null — a shape `openDoc` alone knows how to read.
# A headless browser cannot make such a window, so os.js's own comment says every browser-driven
# check passes through here blind. This stub does not fake a window manager; it only makes
# `PCOSWin.enabled()` true and records what `open()` was asked for, which is exactly enough to drive
# that branch and see whether ONE request to open a post produces ONE window for that post.
POPOUT_STUB = """(() => {
  window.__popped = [];
  window.PCOSWin = {
    enabled: () => true,
    isWindow: () => false,
    viewOf: () => '',
    adopt: () => {},
    routeExisting: (v) => { window.__routed = String(v || ''); },
    open: (view, label, hint) => { window.__popped.push(String(view || '')); return true; },
  };
  return true;
})()"""


async def run(open_social_first, popout=False, open_twice=False):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-reply-click-')
    try:
        proc = subprocess.Popen(
            ['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
             '--window-size=1440,1000', '--remote-debugging-port=0',
             '--user-data-dir=' + profile, 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            tgt = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': 1440, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                await b.call('Page.addScriptToEvaluateOnNewDocument',
                             {'source': 'window.__hasChats=false;' + INIT})
                await b.call('Page.navigate',
                             {'url': 'http://127.0.0.1:%d/client' % server.server_port})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("""(()=>{const key=new Uint8Array(32).fill(3);
                  window.__events=Array.from({length:12},(_,i)=>NostrTools.finalizeEvent(
                    {kind:1,created_at:Math.floor(Date.now()/1000)-i,content:'reply test '+i,tags:[]},key));
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=12")
                if popout:
                    await b.js(POPOUT_STUB)
                await b.js("PCOS.enter()")
                await asyncio.sleep(0.8)

                if open_social_first:
                    await b.js("""(()=>{const i=document.querySelector('.os-icon[data-view=global]');
                                  if(i) i.click();})()""")
                    await asyncio.sleep(1.2)

                # The id of a real post, then ONE request to open it — the click's own endpoint.
                await b.js("window.__target = window.__events[0].id")
                await b.js("(() => { window.__stack=null; window.addEventListener('error', e => { if(/Maximum call stack|too much recursion/i.test(String(e.message||''))) window.__stack=String(e.message); }); __PC.openThread(window.__target); return true; })()")
                await asyncio.sleep(1.5)
                if open_twice:
                    # The SECOND open finds an existing window for this document and takes
                    # openApp's reuse branch — the path that re-entered switchView for ever.
                    await b.js("__PC.openThread(window.__target)")
                    await asyncio.sleep(1.5)
                out = await b.js(PROBE)
                out['popped'] = await b.js("window.__popped || []")
                out['stack'] = await b.js("window.__stack || ''")
                out['alive'] = await b.js("!!(window.__PC && __PC.me && __PC.me())")
                out['routed'] = await b.js("window.__routed || ''")
                return out
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _opened(state):
    """Did one call actually put the POST on screen — not merely a frame?"""
    return state['postWindow'] and state['showsPost']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_cold_desktop_opens_a_post_on_the_first_click():
    state = asyncio.run(run(open_social_first=False))
    print('cold=%r' % (state,))
    assert _opened(state), (
        'opening a post from a clean desktop did nothing on the first call: %r' % (state,))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_an_open_social_window_does_not_swallow_the_first_click():
    state = asyncio.run(run(open_social_first=True))
    print('warm=%r' % (state,))
    assert _opened(state), (
        'with Social already open, the first click to open a post did nothing — the user has to '
        'click twice. Same call, same post; the only difference is that a Social window existed '
        'when it ran: %r' % (state,))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_the_popped_out_desktop_opens_one_window_for_the_post():
    """The branch os.js says browser checks cannot reach — now reached.

    With views popped out as real toplevels, ONE request to open a post must ask the shell for
    exactly ONE window, and that window must be for the post. Asking for none is the reported double
    click (nothing happens, you click again); asking twice is a duplicate window.
    """
    state = asyncio.run(run(open_social_first=True, popout=True))
    print('popout=%r' % (state,))
    posts = [v for v in state['popped'] if 'post:' in v]
    assert posts, (
        'with the desktop popping views out as real windows, opening a post asked the shell for no '
        'window at all — which is exactly "nothing happened, I clicked again". asked for: %r'
        % (state['popped'],))
    assert len(posts) == 1, (
        'one request to open a post asked for %d windows: %r' % (len(posts), posts))


def test_every_route_into_a_window_knows_a_post_is_not_a_view():
    """TWO paths carry a view into a window, and covering one is how this regressed.

    `routeFromPath` runs on the window's FIRST paint. The BroadcastChannel handler in oswin.js runs
    when a window that is ALREADY OPEN is re-routed. The first fix covered only the former, so a
    re-routed post window called `switchView('doc:post:<id>')` — which does not validate its
    argument — and printed "Nothing here can show doc:post:43698d01…".

    Pinned as a rule in both files, because the failure is structural: switchView accepts anything.
    """
    app = client_source()
    oswin = (Path(__file__).resolve().parents[2] / 'static/js/client/oswin.js').read_text(encoding='utf-8')

    for name, src in (('app.js routeFromPath', app), ('oswin.js re-route', oswin)):
        assert 'doc:post:' in src, '%s no longer knows about post windows at all' % name
        # Somewhere in this file, a post-window guard must lead to openThread. Spelled loosely on
        # purpose: the two files write the same regex with and without a capture group, and pinning
        # one spelling is how a rule test stops testing the rule.
        import re as _re
        guards = [m.start() for m in _re.finditer(r'doc:post:\(?\[0-9a-f\]\{64\}', src)]
        assert guards, '%s has no post-window guard' % name
        assert any('openThread' in src[i:i + 600] for i in guards), (
            '%s matches a post window and does not open the post — the next thing it reaches is '
            'switchView, which sets VIEW to a name nothing routes' % name)


def test_switchview_itself_refuses_to_render_a_post_window_as_a_view():
    """THE FLOOR, so a third route cannot repeat this.

    Fixing each caller as it is discovered is how the same bug shipped twice: `routeFromPath` was
    covered, the oswin.js re-route was not, and the second one printed "Nothing here can show
    doc:post:43698d01…" on a real desktop. switchView does not validate its argument — it sets VIEW,
    renders nothing, and leaves an apology — so it is the one place that every caller, including the
    one written next, already passes through.
    """
    app = client_source()
    start = app.index('function switchView(')
    head = app[start:start + 1400]
    assert 'doc:post:' in head, (
        'switchView does not recognise a post window. Every caller then has to remember, and the '
        'two that exist already got it wrong once each.')
    assert 'openThread' in head, (
        'switchView matches a post window and does not open it')
    # It has to come before the body does anything else, or VIEW is already wrong.
    assert head.index('doc:post:') < head.index('openEmojiPopover'), (
        'the guard runs after switchView has begun changing state')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_opening_the_same_post_twice_is_stable():
    """Opening the same post twice must stay stable — and this does NOT reach the reuse branch.

    Stated plainly because it matters: this was written to catch a re-entry through openApp's reuse
    branch (switchView -> openThread -> openDoc -> openApp -> reuse -> switchView). It does not, and
    it passes with that guard reverted. `openDoc` short-circuits on an existing document window
    before `openApp` is ever called, so the second open never takes the path the guard protects.

    Kept because opening the same post twice IS worth pinning — a stack overflow or a dead client
    here would be caught — but it must not be read as proof that the re-entry is covered. It is not.
    Covering it needs a harness that can reach openApp with a doc view and an existing window, which
    the PCOSWin stub cannot produce because it registers no in-page window.
    """
    state = asyncio.run(run(open_social_first=True, popout=True, open_twice=True))
    print('twice=%r' % (state,))
    assert not state['stack'], (
        'opening the same post twice re-entered itself: %s' % state['stack'])
    assert state['alive'], 'the client stopped responding after the second open'
    posts = [v for v in state['popped'] if 'post:' in v]
    assert len(posts) <= 2, (
        'the reuse path asked the shell for a window every time round a loop: %r' % posts)
