"""AN INSTALLED PWA ON BUILD N, AND BUILD N+1 IS DEPLOYED: WHAT DOES IT RUN, AND WHAT DOES IT KEEP?

One Chrome profile holds the app the way a phone does (tests/client/pwa_harness.py): the SHIPPED
service worker, registered by the shipped app.js, caching the shipped shell. The node then serves a
second build -- the same tree with the worker's CACHE bumped (what every client deploy does) and one
appended line in each named module recording which build it came from (`window.__PC_MARKS`). Every
document load is logged with its `__VER` and those marks, so a test can see each load, including one
the app reloads away from on its own.

  mid-session   an open app stays put when the new worker takes over (no reload under somebody's
                hands), says "Update available", and keeps the composer text; pressing the update
                row (#upd-notif) lands on build N+1 with every module from it, and the text is in
                Drafts.
  no grafting   the first screen opened after the takeover needs a module the page has not loaded
                (Communities -> cord-reader.js). It must never run that module from N+1 beside
                concord.js and app.js from N: the page moves onto N+1 and reopens Communities.
  stale launch  the app is opened AFTER the deploy, on a device whose worker and cache are still N.
                No load may pair one build's document with the other's modules, the app must not
                show a blank screen, and text typed in the first seconds survives the launch reload.

What these found (each red before its fix, see the report in the commit):
  * the shell document was served CACHE-FIRST, so the launch after every deploy painted build N's
    document with each of its scripts answered by the network from build N+1 (sw.js shellDoc);
  * a module loaded after the takeover came from the new build into the old page: concord.js N with
    cord-reader.js N+1 (app.js `_ontoNewBuild`);
  * with the shell network-first, a node slow to answer the DOCUMENT (> 2.5s) while its scripts came
    back at once still paired the cached shell N with modules N+1 (sw.js `_cachedPages`: a page whose
    shell came from the cache takes its code from the cache).

  slow shell    the scenario of that last finding.

Fail-first: PC_PWA_OVERRIDE="/static/js/client/sw.js=<old sw.js>,/static/js/client/app.js=<old app.js>".
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.client import pwa_harness as P

pytestmark = pytest.mark.skipif(not P.chrome_ok(), reason='Chrome required')

MODS = ('app.js', 'concord.js', 'cord-reader.js', 'drafts.js', 'compose.js')
LOADS = "JSON.parse(sessionStorage.getItem('__loads')||'[]')"
DRAFTS = ("Object.keys(localStorage).filter(k=>k.startsWith('pc_drafts_'))"
          ".map(k=>localStorage.getItem(k)).join('\\n')")
TEXT = 'a half written thought about the deploy'


def builds():
    v = P.shipped_cache_version()
    return (P.Build('A', '1000', cache=v, mark=MODS),
            P.Build('B', '2000', cache=v + 1, mark=MODS))


def consistent(load):
    """A load is ONE build: every module it ran carries the build its document names."""
    want = {'1000': 'A', '2000': 'B'}[load['ver']]
    return all(tag == want for tag in load['marks'].values())


async def install(ph: P.Phone, build: P.Build):
    """First visit, signed in, worker active; then the reload a returning user's next launch is."""
    pg = await ph.open()
    await ph.login(pg)
    await ph.sw_ready(pg, build.cache)
    await pg.call('Page.reload')
    await pg.until('!!window.__PC && !!(__PC.me&&__PC.me()) && !!navigator.serviceWorker.controller', 60,
                   'the app on its worker')
    return pg


async def type_in_composer(pg: P.Page, text=TEXT):
    await pg.js("document.querySelector('#btn-compose-m').click()")
    await pg.until("!!document.querySelector('#cmp')", 20, 'the composer')
    await pg.js("(()=>{const ta=document.querySelector('#cmp');ta.value=%s;"
                "ta.dispatchEvent(new Event('input',{bubbles:true}))})()" % json.dumps(text))


async def settle(pg: P.Page, quiet=4.0):
    """Signed in on a page that has not reloaded for `quiet` seconds."""
    last = -1
    for _ in range(30):
        await pg.until("!!document.body && !!window.__PC && !!(__PC.me&&__PC.me())", 60, 'the app')
        try:
            n = len(await pg.js(LOADS))
        except P.CDPError:                                 # reloading under us: look again
            continue
        if n == last:
            return
        last = n
        await asyncio.sleep(quiet)
    raise AssertionError('the app kept reloading')


async def worker_takes_over(pg: P.Page, build: P.Build):
    """The app's own update check (reg.update(): on load and every 15 minutes), run now."""
    await pg.js("window.__changed=0;navigator.serviceWorker.addEventListener('controllerchange',()=>__changed++)")
    await pg.js("navigator.serviceWorker.getRegistration('/client').then(r=>r.update())")
    await pg.until('window.__changed>0', 90, 'controllerchange to the new worker')
    await pg.until('caches.has(%s)' % json.dumps(build.cache), 30, build.cache)


def test_an_open_app_is_offered_the_update_and_lands_on_it_whole():
    A, B = builds()
    node = P.Node(A)

    async def go():
        async with P.Phone(node) as ph:
            pg = await install(ph, A)
            await asyncio.sleep(21)                       # past the 20s launch window: this is mid-session
            await type_in_composer(pg)
            await pg.js('window.__samePage=1')
            node.deploy(B)
            await worker_takes_over(pg, B)
            await pg.until("__toasts.some(t=>/Update available/.test(t))", 20, 'the Update available toast')
            assert await pg.js('window.__samePage') == 1, 'the app reloaded under the user mid-session'
            assert await pg.js("document.querySelector('#cmp').value") == TEXT, 'the composer lost its text'
            assert await pg.js("window.__PC_MARKS['app.js']") == 'A'

            # The person finishes later: closes the composer, opens Notifications, presses the row.
            await pg.js("(document.querySelector('#cmp-close')||{click(){}}).click()")
            await pg.js("document.querySelector('.nav-item[data-view=notifications]').click()")
            await pg.until("!!document.querySelector('#upd-notif')", 20, 'the update row')
            await pg.js("document.querySelector('#upd-notif').click()")
            await pg.until("window.__VER==='2000' && !!window.__PC && !!(__PC.me&&__PC.me())", 60,
                           'the app on the new build')
            loads = await pg.js(LOADS)
            assert [l['ver'] for l in loads][-1] == '2000', loads
            assert all(consistent(l) for l in loads), f'a load mixed two builds: {loads}'
            assert await pg.js("navigator.serviceWorker.controller.scriptURL.endsWith('/client/sw.js')")
            assert not await pg.js("caches.has(%s)" % json.dumps(A.cache)), 'the old build is still cached'
            assert TEXT in await pg.js(DRAFTS), 'the unsent composer text is not in Drafts after the update'
            assert not await pg.js('window.__errors'), await pg.js('window.__errors')
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()


def test_a_module_opened_after_the_update_is_never_grafted_onto_the_old_build():
    A, B = builds()
    node = P.Node(A)

    async def go():
        async with P.Phone(node) as ph:
            pg = await install(ph, A)
            await asyncio.sleep(21)
            assert await pg.js("!window.PosterCordReader"), 'cord-reader.js is a page tag again; nothing to test'
            node.deploy(B)
            await worker_takes_over(pg, B)
            # Communities, opened with the sidebar the way a person does: its reader is loaded on demand.
            await pg.js("document.querySelector('.nav-item[data-view=concord]').click()")
            await pg.until("window.__VER==='2000' && !!window.PosterCordReader", 60,
                           'Communities open on the new build with its reader')
            loads = await pg.js(LOADS)
            marks = await pg.js("Object.assign({},window.__PC_MARKS)")
            # The page that was running build A must never have run a module from B.
            assert all(consistent(l) for l in loads), f'a load mixed two builds: {loads}'
            assert marks.get('cord-reader.js') == 'B' and marks.get('concord.js') == 'B' \
                and marks.get('app.js') == 'B', marks
            assert await pg.js("document.querySelector('.nav-item[data-view=concord]').classList.contains('active')"), \
                'the move onto the new build did not reopen Communities'
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()


def test_a_launch_after_a_deploy_never_mixes_builds_and_keeps_what_was_typed():
    A, B = builds()
    node = P.Node(A)

    async def go():
        async with P.Phone(node) as ph:
            await install(ph, A)
            node.deploy(B)
            await ph.cold_start()                          # the app closed, then opened from the home screen
            pg = await ph.open()
            await ph.login(pg)                             # resumes the saved session; no key typed
            await type_in_composer(pg, TEXT + ' (launch)')
            # The new worker activates (the old cache goes) and, inside the launch window, reloads the
            # app onto itself. The shell is fetched network-first, so the first load is often build B
            # already and that reload is a second, harmless one: wait until the page stops reloading.
            await pg.until("caches.has(%s).then(b=>b&&caches.has(%s).then(a=>!a))" % (json.dumps(B.cache), json.dumps(A.cache)),
                           90, 'the new worker active')
            await settle(pg)
            loads = await pg.js(LOADS)
            assert loads, 'no load was logged'
            assert all(consistent(l) for l in loads), f'a load mixed two builds: {loads}'
            assert await pg.js("!!document.querySelector('#feed') && document.body.innerText.trim().length>50"), \
                ('blank screen after the update', await pg.js(
                    "({len:document.body.innerText.trim().length,feed:!!document.querySelector('#feed'),"
                    "cls:document.body.className,toasts:__toasts,errors:__errors})"))
            drafts = await pg.js(DRAFTS)
            composer = await pg.js("(document.querySelector('#cmp')||{}).value||''")
            assert TEXT + ' (launch)' in drafts + composer, 'text typed during the launch was lost'
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()


def test_a_slow_shell_is_never_paired_with_new_modules():
    """The node answers the DOCUMENT slower than the worker waits (APP_FRESH_MS, 2.5s) while its
    scripts come back at once -- a busy node, a radio waking up. The worker paints the cached shell
    (build A), and every script that page asks for must come from the same place, not fresh from B."""
    A, B = builds()
    node = P.Node(A)

    def slow_shell(h, body):
        import time as _t
        _t.sleep(4)
        b = node.build.shell()
        h._send(200, b, 'text/html; charset=utf-8', {'Cache-Control': 'no-store'})

    async def go():
        async with P.Phone(node) as ph:
            await install(ph, A)
            node.deploy(B)
            node.routes[('GET', '/client')] = slow_shell
            await ph.cold_start()
            pg = await ph.open(wait_boot=False)
            await pg.until("!!window.__PC && !!(__PC.me&&__PC.me())", 60, 'the app')
            node.routes.clear()
            await pg.until("caches.has(%s)" % json.dumps(B.cache), 90, 'the new worker')
            await settle(pg)
            loads = await pg.js(LOADS)
            assert loads[0]['ver'] == '1000', f'the slow shell was not the cached one -- nothing tested: {loads}'
            assert all(consistent(l) for l in loads), f'a load mixed two builds: {loads}'
            assert await pg.js("window.__VER") == '2000', 'the app never moved onto the new build'
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()
