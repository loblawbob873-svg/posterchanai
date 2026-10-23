"""ONE ONLINE VISIT, THEN THE NETWORK IS GONE AND THE APP IS OPENED FROM COLD.

tests/test_client_offline_shell.py promises, by reading sw.js and client.html, that the installed app
opens offline: every page script and on-demand module is precached, `/client` and its shortcut URLs
(`?view=messages`, `?view=notifications`) reach the cached shell, and a versioned request finds a
bare precached copy. Nothing ever OPENED it offline. Here a real Chrome profile visits once with the
node up, the node (HTTP and relay) is switched off, the browser is closed and launched again on the same
profile, and the app is opened -- the shipped sw.js is the only thing between the person and nothing.

  cold start   the app boots SIGNED IN (not the sign-in screen), says it is offline, and shows what
               it already had (the post this account made during the online visit); the shortcut
               URLs open their screens; Notes (an on-demand module) opens.
  writes       a post written offline is QUEUED, survives the browser being closed again while still
               offline, and reaches the relay EXACTLY ONCE when the network returns -- and a later
               launch does not send it again.

What it found (each red before its fix):
  * after one online visit the cold start loaded the shell and NONE of its scripts. The
page's tags carry `?v=<build>`, install precaches the bare paths, the first visit's own tags were
fetched before the worker controlled the page, and the worker's app-code fallback (freshFirst) matched
EXACTLY -- so every script failed and a signed-in person was shown the sign-in screen with no app
behind it ("failed 3/3 without", see the commit). sw.js freshFirst now falls back to the bare copy,
the way staleWhileRevalidate always did.

Fail-first: PC_PWA_OVERRIDE="/static/js/client/sw.js=<old sw.js>".
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.client import pwa_harness as P

pytestmark = pytest.mark.skipif(not P.chrome_ok(), reason='Chrome required')

ONLINE = 'posted while the node was up'
OFFLINE = 'written with no network at all'
SIGNED_IN = "!!window.__PC && !!(__PC.me&&__PC.me())"


async def post(pg: P.Page, text: str):
    await pg.js("document.querySelector('#btn-compose-m').click()")
    await pg.until("!!document.querySelector('#cmp-send')", 20, 'the composer')
    await pg.js("(()=>{const ta=document.querySelector('#cmp');ta.value=%s;ta.dispatchEvent(new Event('input',{bubbles:true}));"
                "document.querySelector('#cmp-send').click()})()" % json.dumps(text))


def sent(node: P.Node, text: str):
    return [e for e in node.relay.received if e.get('kind') == 1 and e.get('content') == text]


def test_offline_cold_start_opens_the_signed_in_app_with_what_it_had():
    A = P.Build('A', '1000')
    node = P.Node(A)

    async def go():
        async with P.Phone(node) as ph:
            pg = await ph.open()
            await ph.login(pg)
            await ph.sw_ready(pg, A.cache)
            await post(pg, ONLINE)
            for _ in range(100):
                if sent(node, ONLINE):
                    break
                await asyncio.sleep(.2)
            assert sent(node, ONLINE), 'the online post never reached the relay'
            # The Store writes its IndexedDB cache in 700ms batches; an app killed inside that window
            # keeps the post on the relay but not in the offline cache. Not what this test is about.
            await asyncio.sleep(2)

            node.down()
            await ph.cold_start()
            pg = await ph.open(wait_boot=False)
            await pg.until(SIGNED_IN, 60, 'the signed-in app, offline')
            assert not await pg.js("document.body.classList.contains('guest')"), 'offline launch fell back to the sign-in screen'
            await pg.until("/offline/i.test(document.body.innerText)", 20, 'an offline note')
            await asyncio.sleep(3)                            # the launch's update check has failed by now
            alarms = [t for t in await pg.js('__toasts') if 'failed' in t.lower() or '\u26a0' in t]
            assert not alarms, f'an offline launch raised an error toast: {alarms}'
            await pg.until("document.body.innerText.includes(%s)" % json.dumps(ONLINE), 30,
                           'the post this account already had')

            for view in ('messages', 'notifications'):        # the manifest's shortcuts
                await ph.close_page(pg)
                pg = await ph.open(f'/client?view={view}', wait_boot=False)
                await pg.until(SIGNED_IN, 60, f'the app via ?view={view}')
                await pg.until("!!document.querySelector('.nav-item.active[data-view=%s]')" % view, 20,
                               f'the {view} screen')

            # Notes is promised offline and its code is loaded on demand.
            await pg.js("__PC.switchView('notes')")
            await pg.until("!!window.PCNotes || document.querySelector('#feed').innerText.length>0", 30, 'Notes')
            await asyncio.sleep(1.5)
            bad = [t for t in await pg.js('__toasts') if 'Could not load' in t]
            assert not bad, bad
            assert await pg.js('!!window.PCNotes'), 'notes.js did not load offline'
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()


def test_a_post_written_offline_is_sent_exactly_once_when_the_network_returns():
    A = P.Build('A', '1000')
    node = P.Node(A)

    async def go():
        async with P.Phone(node) as ph:
            pg = await ph.open()
            await ph.login(pg)
            await ph.sw_ready(pg, A.cache)

            node.down()
            await ph.cold_start()
            pg = await ph.open(wait_boot=False)
            await pg.until(SIGNED_IN, 60, 'the signed-in app, offline')
            await post(pg, OFFLINE)
            await pg.until("window.Outbox && Outbox.list().some(x=>x.ev.content===%s)" % json.dumps(OFFLINE), 30,
                           'the offline post in the queue')
            await pg.until("document.body.innerText.includes(%s)" % json.dumps(OFFLINE), 20,
                           'the queued post on screen')

            # Closed again while STILL offline: the queue is all that holds the post.
            await ph.cold_start()
            pg = await ph.open(wait_boot=False)
            await pg.until(SIGNED_IN, 60, 'the app, offline again')
            assert await pg.js("Outbox.list().filter(x=>x.ev.content===%s).length" % json.dumps(OFFLINE)) == 1, \
                'the queued post did not survive the app being closed'

            node.up()                                         # the network comes back; the app stays open
            for _ in range(450):
                if sent(node, OFFLINE) and await pg.js('Outbox.count()') == 0:
                    break
                await asyncio.sleep(.2)
            assert sent(node, OFFLINE), 'the queued post never reached the relay once the network was back'
            assert await pg.js('Outbox.count()') == 0, await pg.js('Outbox.list()')

            await ph.cold_start()                             # a later launch must not send it again
            pg = await ph.open()
            await pg.until(SIGNED_IN + " && Relay.status==='ok'", 60, 'the app online')
            await asyncio.sleep(4)
            got = sent(node, OFFLINE)
            assert len({e['id'] for e in got}) == 1, f'the one post became {len({e["id"] for e in got})} events'
            assert len(got) == 1, f'the queued post was sent {len(got)} times'
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()


# The signer runs in a Worker (relay.js WorkerRPC) and a saved session resumes THROUGH it. Under load
# about one offline cold start in fifteen lost that worker's script fetch (ERR_ABORTED), and the app sat
# on an empty screen for good. Reproduced here deterministically: the page's first `new Worker()` for
# the signer is pointed at a script the node does not have.
BREAK_SIGNER = r"""(()=>{const W=window.Worker;let n=0;window.Worker=function(u,o){
  if(/signer-worker/.test(String(u))&&(window.__signerAlwaysFails||n++===0))u='/static/js/client/no-such-worker.js';
  return new W(u,o);};window.Worker.prototype=W.prototype;})();"""


@pytest.mark.parametrize('always', [False, True], ids=['fails-once', 'never-loads'])
def test_a_signer_that_does_not_load_never_leaves_a_blank_screen(always):
    A = P.Build('A', '1000')
    node = P.Node(A)

    async def go():
        async with P.Phone(node) as ph:
            pg = await ph.open()
            await ph.login(pg)
            await ph.sw_ready(pg, A.cache)
            node.down()
            await ph.cold_start()
            pg = await ph.open(init=('window.__signerAlwaysFails=true;' if always else '') + BREAK_SIGNER,
                               wait_boot=False)
            if not always:
                await pg.until(SIGNED_IN, 40, 'signed in after the signer restarted')
                return
            # It can never start: the app must still be a page somebody can read, and say why.
            await pg.until("document.body.innerText.trim().length>50", 40, 'a page that is not blank')
            await pg.until("__toasts.some(t=>/signer/i.test(t))", 20, 'a sentence about the signer')
    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()
