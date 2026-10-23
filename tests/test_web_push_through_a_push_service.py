"""WEB PUSH, SERVER TO SHADE, WITH NOTHING STUBBED BUT THE VENDOR.

Every Web Push test before this stopped at the call into pywebpush (tests/push_chain_server.py records
the `data=` and goes no further), so what actually crosses the wire -- VAPID, the encryption, the
status codes a push service answers with -- was never exercised. Here:

  * the triggering events are signed and PUBLISHED to the shipped relay (tests/push_relay_harness.py);
  * the REAL watchers turn them into pushes: `_poll` (mentions, zaps), the live `_dm_sub_loop`;
  * devices are registered and preferences set through the REAL routes (/api/push/subscribe, /prefs,
    /unsubscribe, /vapid), each with a genuinely signed proof;
  * the REAL sender (push_service.send -> pywebpush, with the node's real VAPID key from ensure_vapid)
    POSTs to a local push service (tests/web_push_service.py) that verifies VAPID and DECRYPTS the body
    with the subscriber's keys, as a browser's vendor and then the browser do;
  * the decrypted bytes are handed to the SHIPPED sw.js push handler
    (tests/client/push_chain_sw_runtime.mjs), which says what card it draws and where a tap goes.

  payloads       a mention, a DM and a zap each arrive once, decrypt, carry the right type/tag/route,
                 and draw the right card;
  unsubscribe    after /api/push/unsubscribe nothing more is POSTed to that endpoint;
  expired        an endpoint the push service answers 410 (or 404) is pruned -- from the mention
                 poller AND from the DM watcher;
  per account    "mentions" off (no device named = every device of the account) sends no mention while
                 a DM still arrives; "dm" off sends no DM.

What it found, each red before its fix (counts in the commit):
  * NO Web Push had ever been sent. ensure_vapid stores the key as PEM; pywebpush, handed a string,
    reads it as base64url DER and died with "Could not deserialize key data" on every send -- logged
    as a send error, reported by /api/push/test as "could not send" (push_service._vapid_signer);
  * every push went out with TTL 0, "deliver now or discard", so a sleeping device lost it
    (push_service._ttl);
  * the DM and call watchers threw away send()'s "expired" answer, so a browser that had unsubscribed
    at its vendor stayed registered and was POSTed every DM and call for ever -- only a mention, which
    goes through `_poll`, ever pruned it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pywebpush as _real_pywebpush  # noqa: F401  -- the REAL module, imported before anything stubs it

from tests import web_push_service as WP
from tests.push_chain_server import BOB, BOB_SK, Server, _Req, _self_auth

ROOT = Path(__file__).resolve().parents[1]


async def _until(pred, timeout=12.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return pred()


@pytest.fixture(scope='module')
def wire(tmp_path_factory):
    """Run every step once against one relay; return what reached each endpoint, step by step."""
    from app.routers import push as R
    from app.services import push_service, push_store, settings_store
    mp = pytest.MonkeyPatch()
    real_get = settings_store.get
    s = Server(tmp_path_factory.mktemp('web-push'), mp)
    # Undo the harness's stand-ins on the Web Push path: the real pywebpush, the real VAPID key.
    mp.setitem(sys.modules, 'pywebpush', _real_pywebpush)
    mp.setattr(settings_store, 'get', real_get)
    for k in ('NO_PROXY', 'no_proxy'):
        mp.setenv(k, '127.0.0.1,localhost')
    svc = WP.PushService()
    out: dict = {'steps': {}, 'errors': svc.errors}
    toks: dict = {}

    async def deliveries(tok, settle=2.0):
        await asyncio.sleep(settle)
        return svc.take(tok)

    async def subscribe(name):
        tok, sub = svc.subscribe()
        r = await R.subscribe(_Req({'pubkey': BOB, 'auth': _self_auth(BOB_SK, 'push-subscribe'), 'subscription': sub}))
        assert r.get('ok'), r
        toks[name] = tok
        return tok

    async def rows():
        return await push_store.subs_where(lambda r: r.get('pubkey') == BOB)

    async def mention(text):
        ev = await s.publish(s.note(1, text, [['p', BOB]]))
        await s.nps._poll()
        return ev

    async def dm(text):
        s.nps._dm_recent.clear()
        return await s.publish(s.dm_to_bob(text))

    async def go():
        priv, pub = await push_service.ensure_vapid(None)
        out['vapid_public'] = (await R.vapid_key(None))['publicKey']
        assert out['vapid_public'] == pub
        a = await subscribe('laptop')
        b = await subscribe('phone')
        await s.publish(s.note(0, json.dumps({'name': 'Alice'}), []))
        s.nps._dm_stop, s.nps._call_stop = asyncio.Event(), asyncio.Event()
        s._tasks = [asyncio.ensure_future(s.nps._dm_sub_loop())]
        await s.nps._poll()                       # the first poll sets the cursor
        await asyncio.sleep(1.5)                  # the DM subscription reaches EOSE
        bobs = await s.publish(s.note(1, 'my post', [], sk=BOB_SK))

        ev = {}
        ev['mention'] = await mention('hey nostr:bob, look')
        out['steps']['mention'] = {n: await deliveries(t) for n, t in toks.items()}

        ev['dm'] = await dm('hi bob')
        await _until(lambda: svc.received.get(a))
        out['steps']['dm'] = {n: await deliveries(t) for n, t in toks.items()}

        zap_req = s.note(9734, '', [['p', BOB], ['e', bobs['id']], ['amount', '21000']])
        ev['zap'] = await s.publish(s.note(9735, '', [['p', BOB], ['e', bobs['id']], ['bolt11', 'lnbc210n1fake'],
                                                     ['description', json.dumps(zap_req)]]))
        await s.nps._poll()
        out['steps']['zap'] = {n: await deliveries(t) for n, t in toks.items()}

        # Per account: no device named means every device this account has.
        r = await R.set_prefs(_Req({'pubkey': BOB, 'auth': _self_auth(BOB_SK, 'push-prefs'),
                                    'prefs': {'mentions': False}}))
        out['prefs_mentions_off'] = r
        ev['mention_off'] = await mention('you will not hear this one nostr:bob')
        await dm('but you will hear this')
        await _until(lambda: svc.received.get(a))
        out['steps']['mentions_off'] = {n: await deliveries(t) for n, t in toks.items()}
        r = await R.set_prefs(_Req({'pubkey': BOB, 'auth': _self_auth(BOB_SK, 'push-prefs'), 'prefs': {'dm': False}}))
        out['prefs_dm_off'] = r
        await dm('nor this one')
        out['steps']['dm_off'] = {n: await deliveries(t, 3.0) for n, t in toks.items()}
        await R.set_prefs(_Req({'pubkey': BOB, 'auth': _self_auth(BOB_SK, 'push-prefs'), 'prefs': {}}))

        # Unsubscribe the phone through the route the client calls.
        r = await R.unsubscribe(_Req({'endpoint': svc.endpoint(b)}))
        assert r.get('ok'), r
        before = svc.attempts.get(b, 0)
        await mention('after the phone unsubscribed nostr:bob')
        await dm('after the phone unsubscribed')
        await _until(lambda: svc.received.get(a))
        await asyncio.sleep(2)
        out['unsubscribed_attempts'] = svc.attempts.get(b, 0) - before
        out['steps']['after_unsubscribe'] = {n: svc.take(t) for n, t in toks.items()}

        # Expiry at the vendor: the laptop's endpoint now answers 410 -- reached first by a DM.
        svc.expire(a, 410)
        await dm('to a browser that is gone')
        await _until(lambda: svc.attempts.get(a, 0) > 0 and svc.gone.get(a), 5)
        await asyncio.sleep(3)
        out['dm_prunes'] = not any(r.get('endpoint') == svc.endpoint(a) for r in await rows())
        # ...and a mention, which goes through the poller.
        c = await subscribe('tablet')
        svc.expire(c, 404)
        await mention('to another browser that is gone nostr:bob')
        await asyncio.sleep(1)
        out['mention_prunes'] = not any(r.get('endpoint') == svc.endpoint(c) for r in await rows())
        out['ev'] = ev

    try:
        s.run(go(), timeout=240)
    finally:
        s.close()
        svc.close()
        mp.undo()
    return out


def _one(wire, step, device='laptop'):
    got = wire['steps'][step][device]
    assert len(got) == 1, f'{step}: {len(got)} pushes reached {device}, expected one: {got}'
    return got[0]


def test_nothing_the_server_sent_was_rejected_by_the_push_service(wire):
    assert not wire['errors'], wire['errors']


def test_every_push_is_signed_with_the_key_the_browser_subscribed_with(wire):
    for step in ('mention', 'dm', 'zap'):
        assert _one(wire, step)['vapid']['k'] == wire['vapid_public'], step


def test_a_push_waits_for_a_sleeping_device(wire):
    """TTL 0 (pywebpush's default) is "deliver now or discard" (RFC 8030 5.2): a phone asleep in a
    pocket -- the case Web Push exists for -- would never hear about the mention or the DM."""
    for step in ('mention', 'dm', 'zap'):
        assert _one(wire, step)['ttl'] >= 3600, (step, _one(wire, step)['ttl'])


def test_a_mention_decrypts_to_the_card_and_route_of_that_post(wire):
    for dev in ('laptop', 'phone'):
        p = _one(wire, 'mention', dev)['payload']
        eid = wire['ev']['mention']['id']
        assert p['type'] == 'mentions' and p['eid'] == eid and p['tag'] == 'nostr-' + eid, p
        assert p['body'] == 'Alice mentioned you', p


def test_a_dm_decrypts_to_a_blind_card_that_opens_messages(wire):
    p = _one(wire, 'dm')['payload']
    assert p['type'] == 'dm' and p['view'] == 'messages' and p['tag'] == 'pc-dm', p
    assert p['wid'] == wire['ev']['dm']['id']
    assert 'hi bob' not in json.dumps(p), 'the server must not (cannot) read a gift wrap'


def test_a_zap_decrypts_to_a_zap_card_on_the_zapped_post(wire):
    p = _one(wire, 'zap')['payload']
    bobs_post = [t[1] for t in wire['ev']['zap']['tags'] if t[0] == 'e'][0]
    assert p['type'] == 'zaps' and p['eid'] == bobs_post, p
    assert p['tag'] == 'nostr-' + wire['ev']['zap']['id'], p


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_the_shipped_service_worker_draws_each_decrypted_payload(wire):
    labels = ['mention', 'dm', 'zap']
    payloads = [[label, _one(wire, label)['raw']] for label in labels]
    env = dict(os.environ, PC_PUSH_PAYLOADS=json.dumps(payloads))
    r = subprocess.run(['node', str(ROOT / 'tests/client/push_chain_sw_runtime.mjs')],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    sw = {d['label']: d for d in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
    ev = wire['ev']
    assert sw['mention']['shown'] == [{'title': 'PosterChan', 'tag': 'nostr-' + ev['mention']['id'],
                                       'body': 'Alice mentioned you', 'requireInteraction': False}]
    assert sw['mention']['opened'] == ['/client?event=' + ev['mention']['id']]
    assert sw['dm']['shown'][0]['tag'] == 'pc-dm' and sw['dm']['opened'] == ['/client?view=messages']
    assert sw['zap']['shown'][0]['title'] == 'PosterChan' and sw['zap']['shown'][0]['body'].startswith('⚡')
    assert len(sw['zap']['opened']) == 1 and sw['zap']['opened'][0].startswith('/client?event=')


def test_mentions_off_for_the_account_silences_mentions_on_every_device_but_not_dms(wire):
    assert wire['prefs_mentions_off'] == {'ok': True, 'devices': 2}
    for dev in ('laptop', 'phone'):
        types = [d['payload']['type'] for d in wire['steps']['mentions_off'][dev]]
        assert types == ['dm'], f'{dev}: {types}'


def test_dms_off_sends_no_dm(wire):
    assert wire['prefs_dm_off']['ok']
    assert wire['steps']['dm_off'] == {'laptop': [], 'phone': []}


def test_an_unsubscribed_endpoint_is_never_posted_to_again(wire):
    assert wire['unsubscribed_attempts'] == 0
    assert wire['steps']['after_unsubscribe']['phone'] == []
    assert sorted(d['payload']['type'] for d in wire['steps']['after_unsubscribe']['laptop']) == ['dm', 'mentions']


def test_an_endpoint_gone_at_the_vendor_is_pruned_by_the_dm_watcher(wire):
    assert wire['dm_prunes'], 'a 410 from the push service did not remove the device (DM path)'


def test_an_endpoint_gone_at_the_vendor_is_pruned_by_the_poller(wire):
    assert wire['mention_prunes'], 'a 404 from the push service did not remove the device (mention path)'
