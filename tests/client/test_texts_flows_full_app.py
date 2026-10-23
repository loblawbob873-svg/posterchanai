"""TEXTS ON THE DESKTOP, DRIVEN THROUGH ITS OWN CONTROLS, FED BY THE PHONE'S OWN JAVA.

The Texts screen (static/js/client/sms.js) on a laptop shows what the Android phone archives, and sends
by leaving a request the phone performs. Its only browser test so far rang a notification; nothing pressed
the conversation list, opened a thread, sent from the composer or watched a text land in an open thread.

Every phone-side event here is made by the SHIPPED Java, run on a JVM -- never typed by the test:
  * history      `SmsSweep.run` with SmsArchive's sealing (tests/sms_java_phone.py `sweep`);
  * a new text   `SmsOutbox.archiveIncoming`, what SignerRelayService publishes when one arrives
                 (tests/sms_java_event.py);
  * a send       `SmsOutbox.perform` over the request the desktop actually published, its radio a
                 recorder (tests/sms_java_phone.py `perform`), and the signed receipt it returns.
They reach the browser the way they reach a real desktop: published to a real NIP-01 relay
(tests/client/wallpaper_harness.py), which serves the archive read and forwards live events.

Scenarios (each its own account, three at a time):
  * a FRESH device (nothing cached) opens Texts from the sidebar: the conversation list in the phone's
    order with the newest message as each snippet; a thread opens with its bubbles in order and direction;
  * a reply typed in the composer and sent: the phone's radio is handed exactly that text for that
    number, the bubble goes from "sending" to sent on the receipt, and the phone's own archive copy of
    the sent text does not make it appear twice -- not live, and not on a fresh device;
  * a text arriving with a thread open shows in that thread live, without eating the half-typed reply;
    one from a new number tops the list;
  * SLOW: the relay answers the archive read late -- the screen says it is loading (not "no messages"),
    then shows the conversations without being reopened;
  * NEVER: the relay never answers the archive read -- an explicit "history is incomplete", no endless
    spinner, nothing written, and a live text still arrives.

Fail-first / mutation runs: PC_WALLPAPER_OVERRIDE="/static/js/client/sms.js=/tmp/broken-sms.js" serves a
deliberately broken copy of the shipped file.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest
import websockets

from tests.client import wallpaper_harness as H

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sms_java_event  # noqa: E402
import sms_java_phone  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required'),
    pytest.mark.skipif(not sms_java_phone.available(), reason='JDK + android.jar required'),
]

FEATURE_COVERS = ('texts',)
LANES = 3
FIRST_FILL = 140           # accounts 140, 141, ... -- no other suite's key
SCENARIOS = {}
COST = {'a_reply_typed_on_the_desktop_is_sent_by_the_phone_once': 40}
MK = base64.b64encode(bytes(range(32))).decode()

ALICE, BOB, CAROL, DAVE = '+15550142001', '+15550142002', '+15550142003', '+15550142004'


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


class Java:
    """The phone, compiled once: `incoming` (archiveIncoming) and `phone` (sweep / perform)."""

    def __init__(self):
        self.ev = sms_java_event.Builder()
        self.ph = sms_java_phone.Phone()

    def close(self):
        self.ev.close()
        self.ph.close()


class Scenario:
    def __init__(self, name, fill, java, tmp):
        self.name, self.fill, self.pk = name, fill, _pubkey(fill)
        self.sec = ('%02x' % fill) * 32
        self.java, self.open = java, []
        self.phone_ids = set()                                     # what the PHONE published, not the desktop
        self.state = str(Path(tmp) / ('phone-%d.json' % fill))      # one phone, kept across its runs

    async def device(self):
        d = H.Device(sk_fill=self.fill)
        b = await d.__aenter__()
        self.open.append(d)
        assert await b.js('__PC.me().pubkey') == self.pk
        return b

    async def close(self):
        for d in reversed(self.open):
            await d.__aexit__()

    # ---- the phone (shipped Java)
    async def sweep(self, rows):
        """The phone archives these provider rows (SmsSweep) and publishes them to the relay."""
        out = await asyncio.to_thread(self.java.ph.run, 'sweep', self.sec, self.state, {'mk': MK, 'rows': rows})
        assert not out.get('error'), out
        for sha, b64 in (out.get('blobs') or {}).items():      # its pictures, as drive ciphertext
            H.STATE['blobs'][sha] = base64.b64decode(b64)
        await publish_many(out['events'])
        self.phone_ids.update(ev['id'] for ev in out['events'])
        return out['events']

    async def incoming(self, sender, body, when_ms=None):
        now = int(time.time() * 1000)
        ev = await asyncio.to_thread(self.java.ev.event, self.sec, sender, body, when_ms or now, now)
        await publish(ev)
        self.phone_ids.add(ev['id'])
        return ev

    async def perform(self, events):
        """What SignerRelayService.smsOutbox does with every Texts event its relay sockets deliver."""
        return await asyncio.to_thread(self.java.ph.run, 'perform', self.sec, self.state, events)

    def relay_docs(self, prefix):
        return [e for e in H.STATE['events'] if e.get('pubkey') == self.pk and H._d(e).startswith(prefix)]

    def sms_pubs(self):
        """Texts documents this account's DESKTOP sent the relay (the phone's own are not counted)."""
        return [e for e in H.STATE['pubs'] if e.get('pubkey') == self.pk and e['id'] not in self.phone_ids
                and any(t[:2] == ['l', 'pcai-sms'] for t in e.get('tags', []))]


async def publish(ev):
    """Publish to the node relay as another client would -- live subscriptions get it forwarded."""
    await publish_many([ev])


async def publish_many(evs):
    """The same, over one connection (a long history is hundreds of documents)."""
    async with websockets.connect('ws://127.0.0.1:%d' % H.RELAY.port) as ws:
        for ev in evs:
            await ws.send(json.dumps(['EVENT', ev]))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if m[0] == 'OK' and m[1] == ev['id']:
                    assert m[2], m
                    break


def run_scenarios(request, tmp_path_factory, scenarios, first_fill, cost, lanes=LANES, setup=None):
    """Run every wanted scenario of the calling module, `lanes` at a time, each on its own account
    (`first_fill` + its index). Shared by tests/client/test_texts_gaps_full_app.py. `setup(pk, sec)` runs
    per account before any scenario (e.g. its drive index)."""
    wanted = [i.name[len('test_'):] for i in request.session.items
              if getattr(i, 'module', None) is request.module and i.name.startswith('test_')]
    assert not [w for w in wanted if w not in scenarios]
    java = Java()
    tmp = tmp_path_factory.mktemp('texts')
    H.start()
    H.OVERRIDES.clear()
    for pair in filter(None, os.environ.get('PC_WALLPAPER_OVERRIDE', '').split(',')):
        path, _, src = pair.partition('=')
        H.OVERRIDES[path] = src
    orig_layout_req, orig_match = H._layout_req, H._match
    # A SLOW relay, per account: a Texts read by an account in STATE['slow_sms'] is HELD until release()
    # (the harness's 'late' hold, which no scenario here uses for the desktop layout).
    H.STATE['hold_layout'] = 'late'
    H._layout_req = lambda filters: any(
        'pcai-sms' in (f.get('#l') or []) and set(f.get('authors') or ()) & H.STATE.get('slow_sms', set())
        for f in filters)

    def match(f, ev):
        """The node relay's PosterChanDB paging extension `_cursor` (posterchandb/store.py: rows at the cursor's
        second page on `id < cursor`), which Texts' archive read relies on. The harness relay ignores unknown
        filter keys -- as a relay without the extension does, which Texts reports as an incomplete history."""
        if not orig_match(f, ev):
            return False
        cur = f.get('_cursor')
        if isinstance(cur, list) and len(cur) == 2:
            c0, c1 = int(cur[0]), str(cur[1])
            return ev['created_at'] < c0 or (ev['created_at'] == c0 and ev['id'] < c1)
        return True
    H._match = match
    fills = {n: first_fill + i for i, n in enumerate(scenarios)}
    members = {_pubkey(f) for f in fills.values()}
    for f in fills.values():
        if setup:
            setup(_pubkey(f), ('%02x' % f) * 32)

    def session(h, method, path, body):
        """The node's app session + membership answer, for these accounts (Texts is an instance app)."""
        p = path.split('?')[0]
        if p == '/api/auth/nostr-login' and method == 'POST':
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
            if pk not in members:
                return False
            h._json({'access_token': 'tok-' + pk, 'token_type': 'bearer', 'user': {'id': 1, 'nostr_npub': pk}})
            return True
        if p == '/api/instance-welcome/access' and method == 'GET':
            pk = (h.headers.get('authorization') or '').removeprefix('Bearer tok-').strip()
            if pk not in members:
                return False
            h._json({'pubkey': pk, 'qualified': True})
            return True
        return False
    H.ROUTES.append(session)
    queue = sorted(wanted, key=lambda n: -cost.get(n, 20))
    outcome = {}

    async def lane():
        while queue:
            name = queue.pop(0)
            S = Scenario(name, fills[name], java, tmp)
            try:
                await scenarios[name](S)
                outcome[name] = None
            except BaseException as e:      # noqa: B036 -- re-raised by that scenario's own test
                outcome[name] = e
            finally:
                try:
                    await S.close()
                except Exception:
                    pass

    async def main():
        await asyncio.gather(*(lane() for _ in range(lanes)))
    try:
        asyncio.run(main())
    finally:
        H._layout_req, H._match = orig_layout_req, orig_match
        H.ROUTES.remove(session)
        java.close()

    def check(name):
        assert name in outcome, ('scenario never ran', name)
        if outcome[name] is not None:
            raise outcome[name]
    return check



@pytest.fixture(scope='module')
def ran(request, tmp_path_factory):
    return run_scenarios(request, tmp_path_factory, SCENARIOS, FIRST_FILL, COST)


# ------------------------------------------------------------------------------------------- the screen

STATE_JS = ("({errors:window.__errors,toasts:window.__toasts,view:__PC.VIEW,"
            "feed:(document.querySelector('#feed')||{}).innerText})")


async def until(b, expr, seconds=40):
    await H.wait(b, expr, seconds, state=STATE_JS)


async def open_texts(b):
    """The sidebar's Texts button, as a person presses it."""
    await b.js("document.querySelector('.nav-item[data-view=\"texts\"]').click()")
    await until(b, "__PC.VIEW==='texts' && !!document.querySelector('#feed .sms-wrap')")


LIST = ("[...document.querySelectorAll('#feed .sms-thread')].map(x=>({who:x.querySelector('.sms-who').textContent,"
        "snip:x.querySelector('.sms-snip').textContent}))")
BUBBLES = ("[...document.querySelectorAll('#sms-msgs .bubble')].map(x=>({dir:x.classList.contains('in')?'in':'out',"
           "text:(x.querySelector('.b-txt')||{}).textContent||'',meta:(x.querySelector('.b-meta')||{}).textContent||''}))")


def _has_snippet(snippet):
    return ("[...document.querySelectorAll('#feed .sms-thread .sms-snip')].some(x=>x.textContent===%s)"
            % json.dumps(snippet))


async def open_thread(b, snippet):
    """Press the conversation whose newest message is `snippet`."""
    await until(b, _has_snippet(snippet))
    await b.js("[...document.querySelectorAll('#feed .sms-thread')].find(x=>x.querySelector('.sms-snip').textContent===%s)"
               ".click()" % json.dumps(snippet))
    await until(b, "!!document.querySelector('#sms-msgs') && !!document.querySelector('#sms-in')")


def _ms(minutes_ago):
    return int(time.time() * 1000) - int(minutes_ago * 60_000)


def _history():
    return [
        {'address': BOB, 'body': 'B1 your package is at the door', 'date': _ms(50), 'type': 1},
        {'address': ALICE, 'body': 'A1 are we still on for tonight', 'date': _ms(30), 'type': 1},
        {'address': ALICE, 'body': 'A2 yes, 7pm', 'date': _ms(29), 'type': 2},
        {'address': ALICE, 'body': 'A3 see you there', 'date': _ms(28), 'type': 1},
    ]


# ------------------------------------------------------------------------------------------- scenarios

@scenario
async def a_fresh_device_lists_the_phones_conversations_and_opens_one(S):
    await S.sweep(_history())
    await S.incoming(CAROL, 'C1 call me when you can', _ms(5))
    b = await S.device()                          # a fresh profile: nothing cached
    await open_texts(b)
    await until(b, _has_snippet('B1 your package is at the door') + " && " + _has_snippet('C1 call me when you can')
                + " && " + _has_snippet('A3 see you there'))
    rows = await b.js(LIST)
    assert [r['snip'] for r in rows] == ['C1 call me when you can', 'A3 see you there',
                                         'B1 your package is at the door'], rows
    await until(b, "PCSms._state().loading===false")
    assert 'incomplete' not in await b.js("document.querySelector('#feed').innerText"), 'a whole read called incomplete'
    await open_thread(b, 'A3 see you there')
    bubbles = await b.js(BUBBLES)
    assert [(x['dir'], x['text']) for x in bubbles] == [
        ('in', 'A1 are we still on for tonight'), ('out', 'A2 yes, 7pm'), ('in', 'A3 see you there')], bubbles
    assert not any('sending' in x['meta'] or 'not sent' in x['meta'] for x in bubbles), bubbles
    await b.js("document.querySelector('#sms-back').click()")
    await until(b, "!document.querySelector('#sms-msgs') && document.querySelectorAll('#feed .sms-thread').length===3")
    assert not await b.js("window.__errors.filter(e=>!/ResizeObserver/.test(e))")


def test_a_fresh_device_lists_the_phones_conversations_and_opens_one(ran):
    ran('a_fresh_device_lists_the_phones_conversations_and_opens_one')


REPLY = 'On my way / "the usual place" é'


@scenario
async def a_reply_typed_on_the_desktop_is_sent_by_the_phone_once(S):
    await S.sweep(_history())
    b = await S.device()
    await open_texts(b)
    await open_thread(b, 'A3 see you there')
    await b.js("(()=>{const i=document.querySelector('#sms-in');i.value=%s;"
               "i.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#sms-send').click()})()"
               % json.dumps(REPLY))
    # The desktop has no radio: it leaves a request on the relay and shows the text as sending.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    while not S.relay_docs('pcai:smsout:') and loop.time() < deadline:
        await asyncio.sleep(.2)
    asked = S.relay_docs('pcai:smsout:')
    assert len(asked) == 1, ('no send request reached the relay', await b.js(STATE_JS))
    await until(b, "(%s).some(x=>x.dir==='out'&&x.text===%s&&/sending/.test(x.meta))" % (BUBBLES, json.dumps(REPLY)))
    # The phone performs exactly what the relay handed it -- every Texts event it holds, as its sockets would.
    everything = [e for e in H.STATE['events'] if e.get('pubkey') == S.pk]
    done = await S.perform(everything)
    assert done['radio'] == [{'kind': 'sms', 'to': ALICE, 'body': REPLY}], done
    assert len(done['published']) == 1, done
    receipt = done['published'][0]
    await publish(receipt)
    # ...and the desktop's bubble settles to sent, live.
    await until(b, "(()=>{const o=(%s).filter(x=>x.dir==='out'&&x.text===%s);"
                   "return o.length===1&&!/sending|not sent/.test(o[0].meta)})()" % (BUBBLES, json.dumps(REPLY)))
    # A replay of the same request (a second relay, a reconnect) is not sent again.
    again = await S.perform([receipt] + everything)
    assert again['radio'] == [], again
    # The phone's own archive of the sent text -- the provider row, at the moment the radio took it.
    sent_at = (await b.js("(async()=>JSON.parse(await __PC.nip44dec(__PC.me().pubkey,%s)))()"
                          % json.dumps(receipt['content'])))['at']
    await S.sweep([{'address': ALICE, 'body': REPLY, 'date': int(sent_at), 'type': 2}])
    await until(b, "PCSms._state().msgs.size>=5")      # the archived copy has been absorbed
    mine = [x for x in await b.js(BUBBLES) if x['dir'] == 'out' and x['text'] == REPLY]
    assert len(mine) == 1, ('the sent text shows twice', mine)
    # A fresh device reads all of it back: once, sent.
    c = await S.device()
    await open_texts(c)
    await open_thread(c, REPLY)
    mine = [x for x in await c.js(BUBBLES) if x['dir'] == 'out' and x['text'] == REPLY]
    assert len(mine) == 1 and not any(w in mine[0]['meta'] for w in ('sending', 'not sent')), mine


def test_a_reply_typed_on_the_desktop_is_sent_by_the_phone_once(ran):
    ran('a_reply_typed_on_the_desktop_is_sent_by_the_phone_once')


@scenario
async def a_reply_the_relay_refuses_stays_in_the_composer_and_sends_on_retry(S):
    await S.sweep(_history())
    H.STATE['refuse_docs'].add((S.pk, 'pcai:smsout:'))            # no relay will take the send request
    b = await S.device()
    await open_texts(b)
    await open_thread(b, 'A3 see you there')
    send = ("(()=>{const i=document.querySelector('#sms-in');i.value=%s;"
            "i.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#sms-send').click()})()"
            % json.dumps(REPLY))
    await b.js(send)
    await until(b, "window.__toasts.some(t=>/could not reach your relay/.test(t))")
    assert await b.js("document.querySelector('#sms-in').value") == REPLY, 'the typed reply was thrown away'
    assert not [x for x in await b.js(BUBBLES) if x['text'] == REPLY], 'a bubble for a text nobody will send'
    assert await b.js("!document.querySelector('#sms-send').disabled"), 'Send stayed disabled'
    assert (await S.perform([e for e in H.STATE['events'] if e.get('pubkey') == S.pk]))['radio'] == []
    # The relay takes it now: the same press sends it, once.
    H.STATE['refuse_docs'].discard((S.pk, 'pcai:smsout:'))
    await b.js("document.querySelector('#sms-send').click()")
    await until(b, "(%s).some(x=>x.dir==='out'&&x.text===%s&&/sending/.test(x.meta))" % (BUBBLES, json.dumps(REPLY)))
    assert await b.js("document.querySelector('#sms-in').value") == ''
    done = await S.perform([e for e in H.STATE['events'] if e.get('pubkey') == S.pk])
    assert done['radio'] == [{'kind': 'sms', 'to': ALICE, 'body': REPLY}], done


def test_a_reply_the_relay_refuses_stays_in_the_composer_and_sends_on_retry(ran):
    ran('a_reply_the_relay_refuses_stays_in_the_composer_and_sends_on_retry')


@scenario
async def a_text_arriving_with_a_thread_open_shows_live(S):
    await S.sweep(_history())
    b = await S.device()
    await open_texts(b)
    await open_thread(b, 'A3 see you there')
    await b.js("(()=>{const i=document.querySelector('#sms-in');i.focus();i.value='half typ';"
               "i.dispatchEvent(new Event('input',{bubbles:true}))})()")
    await S.incoming(ALICE, 'A4 running late, order for me')
    await until(b, "(%s).some(x=>x.dir==='in'&&x.text==='A4 running late, order for me')" % BUBBLES)
    bubbles = await b.js(BUBBLES)
    assert [x['text'] for x in bubbles][-2:] == ['A3 see you there', 'A4 running late, order for me'], bubbles
    assert await b.js("document.querySelector('#sms-in').value") == 'half typ', 'the half-typed reply was lost'
    # A text from somebody new tops the list.
    await S.incoming(DAVE, 'D1 new number, it is Dave')
    await b.js("document.querySelector('#sms-back').click()")
    await until(b, _has_snippet('D1 new number, it is Dave'))
    rows = await b.js(LIST)
    assert [r['snip'] for r in rows][:2] == ['D1 new number, it is Dave', 'A4 running late, order for me'], rows


def test_a_text_arriving_with_a_thread_open_shows_live(ran):
    ran('a_text_arriving_with_a_thread_open_shows_live')


@scenario
async def a_slow_relay_shows_loading_then_the_conversations(S):
    await S.sweep(_history())
    H.STATE.setdefault('slow_sms', set()).add(S.pk)
    b = await S.device()
    await open_texts(b)
    # While the read is outstanding the screen says so -- never "no messages", never a bare spinner.
    feed = await b.js("document.querySelector('#feed').innerText")
    assert 'No messages here yet' not in feed and 'no SIM' not in feed, feed
    H.STATE['slow_sms'].discard(S.pk)
    H.RELAY.release()
    H.STATE['hold_layout'] = 'late'          # release() clears it; other accounts' reads stay unheld anyway
    await until(b, _has_snippet('A3 see you there') + " && " + _has_snippet('B1 your package is at the door'))
    feed = await b.js("document.querySelector('#feed').innerText")
    assert 'Loading messages' not in feed and 'incomplete' not in feed, feed


def test_a_slow_relay_shows_loading_then_the_conversations(ran):
    ran('a_slow_relay_shows_loading_then_the_conversations')


@scenario
async def a_relay_that_never_answers_says_so_and_writes_nothing(S):
    await S.sweep(_history())
    H.STATE['hold_docs'].add((S.pk, 'pcai-sms'))
    b = await S.device()
    await open_texts(b)
    await until(b, "/Message history is incomplete/.test(document.querySelector('#feed').innerText)", 40)
    assert not await b.js("!!document.querySelector('#feed .spinner')"), await b.js(STATE_JS)
    assert S.sms_pubs() == [], [H._d(e) for e in S.sms_pubs()]
    # The live subscription still works: a text arriving now is shown.
    await S.incoming(CAROL, 'C9 are you there')
    await until(b, _has_snippet('C9 are you there'))
    assert S.sms_pubs() == [], [H._d(e) for e in S.sms_pubs()]


def test_a_relay_that_never_answers_says_so_and_writes_nothing(ran):
    ran('a_relay_that_never_answers_says_so_and_writes_nothing')


if __name__ == '__main__':
    sys.exit(pytest.main([os.path.abspath(__file__), '-q']))
