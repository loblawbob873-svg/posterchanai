"""PRESS "ENABLE PUSH", CLOSE THE APP, GET MENTIONED: THE CARD APPEARS. PRESS IT OFF: NOTHING MORE IS SENT.

The whole Web Push chain with one real piece of everything:

  client     the shipped app in Chrome; the toggle (#set-push-toggle) pressed in Settings ->
             Notifications, so push.js asks for the VAPID key, subscribes, signs its proof and
             registers -- and later unregisters -- through its own code;
  server     the REAL /api/push/* routes (app/routers/push.py, served by uvicorn) on the shipped relay
             and push tables (tests/push_relay_harness.py), the REAL poller (`_poll`) and the REAL
             sender (pywebpush with the node's own VAPID key);
  vendor     a local push service (tests/web_push_service.py) that verifies VAPID and decrypts with
             the subscription's keys -- standing in for fcm.googleapis.com, which nothing here may reach;
  worker     the decrypted bytes delivered to the SHIPPED sw.js in that same Chrome with no window
             open (CDP ServiceWorker.deliverPushMessage), and the notification it shows read back
             from its registration.

The one thing replaced is the browser's own link to its vendor: PushManager.subscribe hands back the
subscription the local push service issued (a real Chrome would ask Google's servers for one).

Found on the way (tests/test_web_push_through_a_push_service.py has the fail-first counts): no Web
Push had ever left this server -- the stored PEM VAPID key was unreadable to pywebpush -- and every
push was sent with TTL 0. This test passes only with both fixed.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from tests.client import pwa_harness as P

pytestmark = pytest.mark.skipif(not P.chrome_ok(), reason='Chrome required')

STUB = r'''
(()=>{const SUB=__SUB__;let cur=null;window.__pushUnsubscribed=0;
  const mk=()=>({endpoint:SUB.endpoint,expirationTime:null,options:{userVisibleOnly:true},getKey(){return null;},
    toJSON(){return {endpoint:SUB.endpoint,expirationTime:null,keys:SUB.keys};},
    unsubscribe(){cur=null;window.__pushUnsubscribed++;return Promise.resolve(true);}});
  if(window.PushManager){
    PushManager.prototype.subscribe=function(o){const k=o&&o.applicationServerKey;
      window.__askedKey=k?btoa(String.fromCharCode(...new Uint8Array(k.buffer||k))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''):'';
      cur=mk();return Promise.resolve(cur);};
    PushManager.prototype.getSubscription=function(){return Promise.resolve(cur);};
  }})();
'''


class Api:
    """The shipped push routes under uvicorn, on their own loop (which the watcher shares)."""

    def __init__(self):
        import uvicorn
        from fastapi import FastAPI
        from app.database import get_db
        from app.routers import push as R
        app = FastAPI()
        app.include_router(R.router)
        app.dependency_overrides[get_db] = lambda: None       # the push routes keep nothing in SQL
        self.port = P.free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning',
                                                    lifespan='off'))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(.1)
        assert self.server.started, 'uvicorn did not start'

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.server.should_exit = True
        self.thread.join(10)


async def _to_notifications(pg: P.Page):
    await pg.js("__PC.switchView('settings')")
    await pg.until("!!document.querySelector('.us-tab[data-tab=\"notifications\"]')", 30, 'the Settings tabs')
    await pg.js("document.querySelector('.us-tab[data-tab=\"notifications\"]').click()")
    await pg.until("!!document.getElementById('set-push-toggle') && !document.getElementById('set-push-toggle').disabled",
                   30, 'the push toggle')


def test_push_enabled_in_the_app_reaches_it_closed_and_stops_when_turned_off(tmp_path, monkeypatch):
    from tests import push_relay_harness as H
    from tests import web_push_service as WP
    from app.services import nostr_push_service as nps, push_store
    from app.services.nostr import bip340, relay
    from app.services.nostr import event as nostr_event

    for k in ('NO_PROXY', 'no_proxy'):
        monkeypatch.setenv(k, '127.0.0.1,localhost')
    rel = H.start(tmp_path, monkeypatch)
    url = f'ws://127.0.0.1:{rel.port}'
    monkeypatch.setattr(nps, '_local_relay', lambda: [url])
    for name, val in (('_cursor', 0), ('_sub_pks_at', 0.0)):
        monkeypatch.setattr(nps, name, val)
    for name in ('_seen', '_names', '_dm_recent', '_call_recent', '_sub_pks'):
        monkeypatch.setattr(nps, name, type(getattr(nps, name))())
    svc = WP.PushService()
    tok, sub = svc.subscribe()
    api = Api()
    node = P.Node(P.Build('A', '1000'))
    node.proxy['/api/push/'] = f'http://127.0.0.1:{api.port}'
    me = bip340.pubkey_from_seckey(bytes([7]) * 32).hex()          # Phone(sk_fill=7)
    alice = bytes.fromhex('a1' * 32)

    async def mention(text):
        ev = nostr_event.build_event(alice, 1, text, tags=[['p', me]])
        assert await relay.publish([url], ev) == 1
        await nps._poll()
        return ev

    async def go():
        api.run(relay.publish([url], nostr_event.build_event(alice, 0, json.dumps({'name': 'Alice'}))))
        async with P.Phone(node) as ph:
            pg = await ph.open(init=STUB.replace('__SUB__', json.dumps(sub)))
            await ph.login(pg)
            await ph.sw_ready(pg, node.build.cache)
            await pg.call('ServiceWorker.enable')
            await _to_notifications(pg)
            await pg.js("document.getElementById('set-push-toggle').click()")
            await pg.until("__toasts.some(t=>/Push notifications on/.test(t))", 30, 'notifications on')
            rows = api.run(push_store.subs_where(lambda r: r.get('pubkey') == me))
            assert [(r['endpoint'], r['p256dh'], r['auth']) for r in rows] == \
                [(sub['endpoint'], sub['keys']['p256dh'], sub['keys']['auth'])], ('the subscription stored', rows)
            from app.services import push_service
            assert await pg.js('window.__askedKey') == push_service.get_public_key(), \
                'the browser subscribed with a key that is not this node\'s VAPID key'
            regs = [e['params']['registrations'] for e in ph.cdp.events
                    if e['method'] == 'ServiceWorker.workerRegistrationUpdated' and e.get('sessionId') == pg.session]
            api.run(nps._poll())                                   # the scheduler's first pass sets the cursor
            await asyncio.sleep(1.1)
            reg_id = next(r['registrationId'] for batch in regs for r in batch
                          if r['scopeURL'].startswith(node.origin + '/client') and not r.get('isDeleted'))

            # The app is CLOSED; somebody mentions this account.
            await ph.close_page(pg)
            # ...closed as far as the WORKER knows. The push handler shows nothing while a window is visible (the open
            # app raises its own), and under load a just-closed page is still one of its clients for a moment -- so a
            # push delivered at once was rightly suppressed and this test failed 3 in 10 run 10-up. Wait for that.
            w = await ph.worker()
            for _ in range(100):
                if not await w.js("clients.matchAll({type:'window',includeUncontrolled:true}).then(c=>c.length)"):
                    break
                await asyncio.sleep(.1)
            else:
                raise AssertionError('the closed page is still a window of the service worker after 10 s')
            ev = api.run(mention('are you there nostr:me'))
            for _ in range(100):
                if svc.received.get(tok):
                    break
                await asyncio.sleep(.1)
            [got] = svc.take(tok)
            assert got['payload']['eid'] == ev['id'] and got['ttl'] > 0, got
            t = await ph.cdp.call('Target.createTarget', {'url': 'about:blank'})
            s = await ph.cdp.call('Target.attachToTarget', {'targetId': t['targetId'], 'flatten': True})
            await ph.cdp.call('ServiceWorker.enable', session=s['sessionId'])
            await ph.cdp.call('ServiceWorker.deliverPushMessage', {'origin': node.origin, 'registrationId': reg_id,
                                                                    'data': got['raw']}, session=s['sessionId'])
            w = await ph.worker()
            shown = None
            for _ in range(100):
                shown = await w.js("self.registration.getNotifications().then(ns=>ns.map(n=>({title:n.title,body:n.body,"
                                   "tag:n.tag,eid:(n.data||{}).eid})))")
                if shown:
                    break
                await asyncio.sleep(.1)
            assert shown == [{'title': 'PosterChan', 'body': 'Alice mentioned you', 'tag': 'nostr-' + ev['id'],
                              'eid': ev['id']}], ('the notification shown by the worker', shown)

            # Turned OFF in the app: the server forgets the device and nothing more is sent to it.
            pg = await ph.open(init=STUB.replace('__SUB__', json.dumps(sub)))
            await pg.until("!!(__PC.me&&__PC.me())", 40, 'signed in')
            # The stub's subscription lives in the page, so this page re-subscribes first (as the
            # browser would still hold it) -- then the user presses the toggle off.
            await pg.js("navigator.serviceWorker.ready.then(r=>r.pushManager.subscribe({userVisibleOnly:true}))")
            await _to_notifications(pg)
            await pg.until("/Turn off/.test(document.getElementById('set-push-toggle').textContent)", 20, 'the toggle reading on')
            await pg.js("document.getElementById('set-push-toggle').click()")
            await pg.until("__toasts.some(t=>/Push notifications off/.test(t)) && window.__pushUnsubscribed>0", 30,
                           'notifications off')
            assert api.run(push_store.subs_where(lambda r: r.get('pubkey') == me)) == []
            before = svc.attempts.get(tok, 0)
            api.run(mention('and now? nostr:me'))
            await asyncio.sleep(2)
            assert svc.attempts.get(tok, 0) == before, 'a push was sent to a device that turned notifications off'
            assert not svc.errors, svc.errors

    try:
        asyncio.run(asyncio.wait_for(go(), 300))
    finally:
        node.close()
        api.close()
        svc.close()
        H.reset()
        rel.close()
