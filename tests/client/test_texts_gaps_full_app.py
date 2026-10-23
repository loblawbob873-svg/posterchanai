"""TEXTS ON THE DESKTOP, THE FLOWS NO TEST HAD PRESSED: PICTURES BOTH WAYS, ARCHIVE / BLOCK / DELETE, SEARCH.

tests/client/test_texts_flows_full_app.py drives the conversation list, a reply and a live arrival. This
drives the rest of the screen the same way -- a fresh Chrome profile is a fresh device, a real NIP-01 relay
and a real instance (drive index + Blossom) behind it (tests/client/wallpaper_harness.py) -- and every
phone-side event or answer is made by the SHIPPED Java on a JVM, never typed by the test:

  history        `SmsSweep.run` with SmsArchive's sealing; a picture's bytes stored as real drive
                 ciphertext (`SyncCrypto`); contact names from the phone's address book
                 (tests/sms_java_phone.py `sweep`, its PhoneBook a map)
  a new text     `SmsOutbox.archiveIncoming` (tests/sms_java_event.py)
  a send         `SmsOutbox.perform` over what the desktop published -- for a picture, the REAL `SyncStore`
                 / `SyncNet` / `SyncCrypto` fetch it from this instance's Blossom and open it with the
                 account's drive key; the radio is a recorder (tests/sms_java_phone.py `perform`)
  an archive     `SmsArchived.absorb` over the relay's events, then `ArchivedThreads.hidden` -- what the
                 phone's native thread list asks (tests/sms_java_phone.py `archived`)
  a block        the phone's WebView (shipped sms.js in tests/client/sms_sim.js, a handset) receives the
                 desktop's own document and calls `SmsPlugin.setBlocked` -> `Blocklist.set` on a JVM, and
                 its answer is published back to the relay the desktop is reading

Scenarios (each its own account, three at a time):
  1  PICTURES   a received photo shows on a fresh device and opens full-size; a photo picked in the
                composer reaches the phone's radio as that MMS (bytes, type, name, number); an upload
                that fails and a request the relay refuses each keep the draft and say so, and the same
                press then sends it once.
  2  ARCHIVE    archived from the row menu: the phone's own list hides it, a reload and a fresh device
                show it archived, unarchive (from that fresh device) brings it back on the phone; a
                refused record is said and changes nothing.
     BLOCK      blocked from the row menu: the phone's real blocked-numbers list holds it and the desktop
                says "Blocked" -- after a reload and on a fresh device; unblock empties the phone's list;
                a refused document is said and blocks nothing.
     DELETE     a message deleted from the thread is gone after a reload and on a fresh device, and the
                phone's next sweep does not bring it back; a refused delete is said and keeps it.
  3  SEARCH     a word in the OLDEST page of a long history; a contact's name (also when the newest text
                carries none); "no results" said as such -- and when the history could not be read, said
                as THAT, not as a clean "no match".

Fail-first / mutation runs: PC_WALLPAPER_OVERRIDE="/static/js/client/sms.js=/tmp/broken-sms.js".
"""
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from tests.client import wallpaper_harness as H
from tests.client import test_texts_flows_full_app as F

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
import sms_java_phone  # noqa: E402
from app.services.nostr.nip44 import encrypt_self  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required'),
    pytest.mark.skipif(not sms_java_phone.available(), reason='JDK + android.jar required'),
]

FEATURE_COVERS = ('texts',)
FIRST_FILL = 240           # accounts 240, 241, ... -- no other suite's key
SCENARIOS = {}
COST = {'a_word_in_the_oldest_page_and_a_contacts_name_are_found': 60,
        'a_picture_picked_in_the_composer_reaches_the_phone_as_that_mms': 40,
        'a_picture_that_cannot_go_stays_in_the_composer_says_so_and_sends_once_later': 40}
MK = bytes(range(32))
SIM = ROOT / 'tests/client/sms_sim.js'

ALICE, BOB, CAROL, DAVE = '+15550240001', '+15550240002', '+15550240003', '+15550240004'
open_texts, open_thread, until, BUBBLES, LIST, _ms = F.open_texts, F.open_thread, F.until, F.BUBBLES, F.LIST, F._ms


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def png(w, h, seed):
    """A real PNG (so <img> decodes it and naturalWidth is its width), recognisable by its seed."""
    rows = []
    for y in range(h):
        row = bytearray(b'\x00')
        for x in range(w):
            row += bytes(((x * 7 + y * 3 + seed) & 0xFF, (x * seed) & 0xFF, (y + seed) & 0xFF))
        rows.append(bytes(row))
    raw = b''.join(rows)

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


RECEIVED = png(320, 200, 5)        # what Alice sent the phone
PICKED = png(240, 160, 9)          # what the person picks on the desktop


def b64(b):
    return base64.b64encode(b).decode()


def _drive_key(pk, sec):
    """The account's drive key, wrapped to itself, as the files index carries it -- what the desktop
    encrypts an upload with and what the phone's SyncStore was configured with at sign-in."""
    H.STATE['indexes'][pk] = {'mk': encrypt_self(bytes.fromhex(sec), json.dumps({'k': b64(MK)}))}


@pytest.fixture(scope='module')
def ran(request, tmp_path_factory):
    return F.run_scenarios(request, tmp_path_factory, SCENARIOS, FIRST_FILL, COST, setup=_drive_key)


def _history():
    return [
        {'address': BOB, 'body': 'B1 your package is at the door', 'date': _ms(50), 'type': 1},
        {'address': ALICE, 'body': 'A1 are we still on for tonight', 'date': _ms(30), 'type': 1},
        {'address': ALICE, 'body': 'A2 yes, 7pm', 'date': _ms(29), 'type': 2},
        {'address': ALICE, 'body': 'A3 see you there', 'date': _ms(28), 'type': 1},
    ]


def _row(snippet):
    return ("[...document.querySelectorAll('#feed .sms-thread')].find(x=>x.querySelector('.sms-snip').textContent===%s)"
            % json.dumps(snippet))


async def row_menu(b, snippet, item):
    """Right-click the conversation, then press the menu item whose label starts with `item`."""
    await until(b, F._has_snippet(snippet))
    await b.js("%s.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))" % _row(snippet))
    await until(b, "!!document.querySelector('.menu-pop button[data-m]')")
    await b.js("[...document.querySelectorAll('.menu-pop button[data-m]')].find(x=>x.textContent.startsWith(%s)).click()"
               % json.dumps(item))


async def confirm(b):
    await until(b, "!!document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')")
    await b.js("document.querySelector('.uiconfirm-bg [data-uc=\"1\"]').click()")


async def toast(b, pattern, seconds=30):
    await until(b, "window.__toasts.some(t=>%s.test(t))" % pattern, seconds)


async def relay_wait(S, prefix, n=1, seconds=30):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while len(S.relay_docs(prefix)) < n and loop.time() < deadline:
        await asyncio.sleep(.2)
    return S.relay_docs(prefix)


def everything(S):
    return [e for e in H.STATE['events'] if e.get('pubkey') == S.pk]


def instance_base():
    return 'http://127.0.0.1:%d' % H.HTTP.server_address[1]


async def configure_phone(S):
    """What SyncPlugin.configure leaves on the signed-in phone: this instance, its Blossom, the drive key."""
    await asyncio.to_thread(S.java.ph.run, 'configure', S.sec, S.state,
                            {'api': instance_base(), 'media': instance_base() + '/blossom',
                             'mk': H.STATE['indexes'][S.pk]['mk']})


IMG_BYTES = ("(async sel=>{const i=document.querySelector(sel);if(!i||!i.complete||!i.naturalWidth)return null;"
             "const a=new Uint8Array(await (await fetch(i.src)).arrayBuffer());let s='';"
             "for(let k=0;k<a.length;k+=8192)s+=String.fromCharCode(...a.subarray(k,k+8192));"
             "return {w:i.naturalWidth,h:i.naturalHeight,b64:btoa(s)}})")


async def pick_file(b, data, name, mime):
    """The person picks a file in the composer's file dialog (the real <input type=file>'s change)."""
    await b.js("(()=>{const bin=atob(%s),a=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)a[i]=bin.charCodeAt(i);"
               "const dt=new DataTransfer();dt.items.add(new File([a],%s,{type:%s}));const p=document.querySelector('#sms-file');"
               "p.files=dt.files;p.dispatchEvent(new Event('change',{bubbles:true}));return true})()"
               % (json.dumps(b64(data)), json.dumps(name), json.dumps(mime)))
    await until(b, "!!document.querySelector('.sms-attachment-draft') && "
                   "document.querySelector('.sms-attachment-draft').textContent.includes(%s)" % json.dumps(name))


async def type_and_send(b, text):
    await b.js("(()=>{const i=document.querySelector('#sms-in');i.value=%s;"
               "i.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#sms-send').click()})()"
               % json.dumps(text))


# =============================================================================================== 1 PICTURES

@scenario
async def a_picture_the_phone_received_shows_on_a_fresh_device_and_opens_full_size(S):
    await S.sweep(_history() + [{'address': ALICE, 'body': 'P1 look at this', 'date': _ms(5), 'type': 1, 'mms': True,
                                 'parts': [{'ct': 'image/png', 'name': 'IMG_0001.png', 'b64': b64(RECEIVED)}]}])
    b = await S.device()                          # nothing cached: everything below was read back
    await open_texts(b)
    await open_thread(b, 'P1 look at this')
    sel = '#sms-msgs .bubble img.sms-att-img'
    await until(b, "(()=>{const i=document.querySelector(%s);return !!i&&i.complete&&i.naturalWidth>0})()" % json.dumps(sel), 60)
    shown = await b.js("%s(%s)" % (IMG_BYTES, json.dumps(sel)))
    assert (shown['w'], shown['h']) == (320, 200), shown and (shown['w'], shown['h'])
    assert base64.b64decode(shown['b64']) == RECEIVED, 'the thread shows different bytes from the photo Alice sent'
    bubble = await b.js("(()=>{const i=document.querySelector(%s).closest('.bubble');return {dir:i.classList.contains('in')?'in':'out',"
                        "text:(i.querySelector('.b-txt')||{}).textContent||''}})()" % json.dumps(sel))
    assert bubble == {'dir': 'in', 'text': 'P1 look at this'}, bubble
    # Pressing the picture opens it full-size in the app's lightbox -- the original, not a thumbnail.
    await b.js("document.querySelector(%s).click()" % json.dumps(sel))
    lb = '.lightbox img'
    await until(b, "(()=>{const i=document.querySelector(%s);return !!i&&i.complete&&i.naturalWidth>0})()" % json.dumps(lb), 20)
    full = await b.js("%s(%s)" % (IMG_BYTES, json.dumps(lb)))
    assert (full['w'], full['h']) == (320, 200), (full['w'], full['h'])
    assert base64.b64decode(full['b64']) == RECEIVED, 'the lightbox opened something other than the photo'
    assert not await b.js("window.__errors.filter(e=>!/ResizeObserver/.test(e))")


def test_a_picture_the_phone_received_shows_on_a_fresh_device_and_opens_full_size(ran):
    ran('a_picture_the_phone_received_shows_on_a_fresh_device_and_opens_full_size')


async def _picture_thread(S):
    await S.sweep(_history())
    await configure_phone(S)
    b = await S.device()
    await open_texts(b)
    await open_thread(b, 'A3 see you there')
    return b


def _the_mms(radio, caption):
    assert len(radio) == 1, ('one press must be one MMS', [{k: v for k, v in r.items() if k != 'b64'} for r in radio])
    mms = radio[0]
    assert {k: v for k, v in mms.items() if k != 'b64'} == {'kind': 'mms', 'to': ALICE, 'body': caption,
                                                              'mime': 'image/png', 'name': 'beach.png'}, \
        {k: v for k, v in mms.items() if k != 'b64'}
    assert base64.b64decode(mms['b64']) == PICKED, 'the phone sent different bytes from the ones picked'


@scenario
async def a_picture_picked_in_the_composer_reaches_the_phone_as_that_mms(S):
    b = await _picture_thread(S)
    await pick_file(b, PICKED, 'beach.png', 'image/png')
    await type_and_send(b, 'P2 for you')
    asked = await relay_wait(S, 'pcai:smsout:')
    assert len(asked) == 1, ('no send request reached the relay', await b.js(F.STATE_JS))
    await toast(b, "/waiting for your phone/")
    # The composer is cleared only now that the request is out.
    assert await b.js("!document.querySelector('.sms-attachment-draft') && document.querySelector('#sms-in').value===''")
    await until(b, "(%s).some(x=>x.dir==='out'&&x.text==='P2 for you'&&/sending/.test(x.meta))" % BUBBLES)
    # The phone performs every Texts event its sockets hold -- the MMS branch fetching the picture from
    # this instance's Blossom and opening it with the drive key, as on the handset.
    done = await S.perform(everything(S))
    _the_mms(done['radio'], 'P2 for you')
    again = await S.perform(done['published'] + everything(S))
    assert again['radio'] == [], 'a replay of the same request sent the picture again'


def test_a_picture_picked_in_the_composer_reaches_the_phone_as_that_mms(ran):
    ran('a_picture_picked_in_the_composer_reaches_the_phone_as_that_mms')


@scenario
async def a_picture_that_cannot_go_stays_in_the_composer_says_so_and_sends_once_later(S):
    b = await _picture_thread(S)
    await pick_file(b, PICKED, 'beach.png', 'image/png')
    kept = ("(()=>{const d=document.querySelector('.sms-attachment-draft');return !!d&&d.textContent.includes('beach.png')"
            "&&document.querySelector('#sms-in').value==='P3 the beach'&&!document.querySelector('#sms-send').disabled})()")
    # (a) the drive cannot store the picture: nothing is asked of the phone, and it says so.
    H.STATE['upload_fail'].add(S.pk)
    await type_and_send(b, 'P3 the beach')
    await toast(b, "/attachment/i")
    said = [t for t in await b.js("window.__toasts") if 'attachment' in t.lower()]
    assert said and 'nothing was sent' in said[-1].lower(), ('the failure does not say the picture was not sent', said)
    assert await b.js(kept), ('the picked picture or the caption was thrown away', await b.js(F.STATE_JS))
    assert not S.relay_docs('pcai:smsout:'), 'a send request went out for a picture that was never stored'
    assert not [x for x in await b.js(BUBBLES) if x['text'] == 'P3 the beach'], 'a bubble for a picture nobody will send'
    # (b) stored now, but no relay takes the request: still kept, still said.
    H.STATE['upload_fail'].discard(S.pk)
    H.STATE['refuse_docs'].add((S.pk, 'pcai:smsout:'))
    await b.js("window.__toasts.length=0;document.querySelector('#sms-send').click()")
    await toast(b, "/could not reach your relay/")
    assert await b.js(kept), ('the picked picture or the caption was thrown away', await b.js(F.STATE_JS))
    assert (await S.perform(everything(S)))['radio'] == []
    # (c) the same press, once everything answers: one MMS of exactly that picture.
    H.STATE['refuse_docs'].discard((S.pk, 'pcai:smsout:'))
    await b.js("document.querySelector('#sms-send').click()")
    await toast(b, "/waiting for your phone/")
    assert len(await relay_wait(S, 'pcai:smsout:')) == 1
    assert await b.js("!document.querySelector('.sms-attachment-draft') && document.querySelector('#sms-in').value===''")
    done = await S.perform(everything(S))
    _the_mms(done['radio'], 'P3 the beach')


def test_a_picture_that_cannot_go_stays_in_the_composer_says_so_and_sends_once_later(ran):
    ran('a_picture_that_cannot_go_stays_in_the_composer_says_so_and_sends_once_later')


# ======================================================================================= 2 ARCHIVE / BLOCK / DELETE

async def phone_hides(S, *convs):
    """The phone's native list: is each (address, newest date) hidden, after absorbing the relay's events?"""
    out = await asyncio.to_thread(S.java.ph.run, 'archived', S.sec, S.state,
                                  {'events': everything(S), 'ask': [list(c) for c in convs]})
    return out['hidden']


def _snips(rows):
    return [r['snip'] for r in rows]


@scenario
async def archive_and_unarchive_reach_the_phone_and_every_device(S):
    hist = _history()
    await S.sweep(hist)
    alice, bob = (ALICE, hist[3]['date']), (BOB, hist[0]['date'])
    d = H.Device(sk_fill=S.fill)
    b = await d.__aenter__()
    S.open.append(d)
    await open_texts(b)
    await row_menu(b, 'A3 see you there', 'Archive')
    await toast(b, "/Archived on all your devices/")
    await until(b, "!(%s).some(x=>x.snip==='A3 see you there') && /Archived \\(1\\)/.test(document.querySelector('#sms-arc-open').textContent)"
                % LIST)
    assert len(await relay_wait(S, 'pcai:smsarc:')) == 1
    assert await phone_hides(S, alice, bob) == [True, False], "the phone's own list does not hide what the desktop archived"
    # A reload, and a device that has never seen it, both read it back as archived.
    await d.reload()
    await open_texts(b)
    await until(b, F._has_snippet('B1 your package is at the door') + " && !!document.querySelector('#sms-arc-open')")
    assert _snips(await b.js(LIST)) == ['B1 your package is at the door'], await b.js(LIST)
    c = await S.device()
    await open_texts(c)
    await until(c, "!!document.querySelector('#sms-arc-open') && /Archived \\(1\\)/.test(document.querySelector('#sms-arc-open').textContent)")
    assert _snips(await c.js(LIST)) == ['B1 your package is at the door'], await c.js(LIST)
    # Unarchive, from that other device's Archived view: back in the list there, and on the phone.
    await c.js("document.querySelector('#sms-arc-open').click()")
    await until(c, "!!document.querySelector('#sms-arc-back')")
    await row_menu(c, 'A3 see you there', 'Unarchive')
    await toast(c, "/Moved back to your conversations/")
    await until(c, "(%s).map(x=>x.snip).join('|')==='A3 see you there|B1 your package is at the door'" % LIST)
    assert await phone_hides(S, alice, bob) == [False, False], 'the phone still hides a conversation unarchived elsewhere'
    # ...and the first device, live.
    await until(b, F._has_snippet('A3 see you there'))
    # A refused record: said, nothing hidden here or on the phone.
    H.STATE['refuse_docs'].add((S.pk, 'pcai:smsarc:'))
    await row_menu(c, 'B1 your package is at the door', 'Archive')
    await toast(c, "/could not archive -- nothing was changed/")
    assert _snips(await c.js(LIST)) == ['A3 see you there', 'B1 your package is at the door'], await c.js(LIST)
    assert not await c.js("!!document.querySelector('#sms-arc-open')")
    assert await phone_hides(S, alice, bob) == [False, False]


def test_archive_and_unarchive_reach_the_phone_and_every_device(ran):
    ran('archive_and_unarchive_reach_the_phone_and_every_device')


def handset(S, events, available=True):
    """The phone's WebView (shipped sms.js on a handset, tests/client/sms_sim.js) receives these events and
    applies the block list with the SHIPPED SmsPlugin.setBlocked on a JVM; returns what it published."""
    argv = S.java.ph.argv('block', S.sec, S.state, 'true' if available else 'false')
    r = subprocess.run(['node', str(SIM), json.dumps({'realKey': S.sec, 'isPhone': True, 'telephony': True,
                                                      'relay': events, 'rawEvents': events, 'blockArgv': argv,
                                                      'steps': ['absorbRaw', 'settle', 'settle', 'settle']})],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    return [e for e in out['relayFull'] if H._d(e) == 'pcai:smsblocks'], out


def phone_blocked(S):
    return json.load(open(S.state)).get('blocked', [])


BLOCKED = ("(()=>{const r=%s;return r?((r.querySelector('.sms-blocked')||{}).textContent||''):null})()")


@scenario
async def block_and_unblock_reach_the_phones_list_and_every_device(S):
    await S.sweep(_history())
    d = H.Device(sk_fill=S.fill)
    b = await d.__aenter__()
    S.open.append(d)
    await open_texts(b)
    await row_menu(b, 'B1 your package is at the door', 'Block')
    await confirm(b)
    await toast(b, "/Blocked/")
    asked = await relay_wait(S, 'pcai:smsblocks')
    assert len(asked) == 1, await b.js(F.STATE_JS)
    await until(b, "/Blocked — waiting for your phone/.test(%s)" % (BLOCKED % _row('B1 your package is at the door')))
    # The phone gets the desktop's own document; its real blocked-numbers code takes the number.
    answer, _ = await asyncio.to_thread(handset, S, asked)
    assert phone_blocked(S) == [BOB], "the phone's blocked-numbers list does not hold the number"
    assert len(answer) == 1 and answer[0]['id'] != asked[0]['id'], 'the phone never answered'
    await F.publish(answer[0])
    await until(b, "(%s)==='⊘ Blocked'" % (BLOCKED % _row('B1 your package is at the door')))
    # After a reload, and on a device that has never seen it: Blocked.
    await d.reload()
    await open_texts(b)
    await until(b, "(%s)==='⊘ Blocked'" % (BLOCKED % _row('B1 your package is at the door')))
    c = await S.device()
    await open_texts(c)
    await until(c, "(%s)==='⊘ Blocked'" % (BLOCKED % _row('B1 your package is at the door')))
    assert await c.js(BLOCKED % _row('A3 see you there')) == '', 'another conversation shows as blocked'
    # Unblock from that device: the phone's list lets the number go.
    await row_menu(c, 'B1 your package is at the door', 'Unblock')
    await toast(c, "/Unblocked/")
    await until(c, "(%s)===''" % (BLOCKED % _row('B1 your package is at the door')))
    latest = sorted(S.relay_docs('pcai:smsblocks'), key=lambda e: e['created_at'])[-1:]
    await asyncio.to_thread(handset, S, latest)
    assert phone_blocked(S) == [], 'the phone still blocks a number unblocked on the desktop'
    # A refused document: said, and nothing is blocked -- not here, not after a reload.
    H.STATE['refuse_docs'].add((S.pk, 'pcai:smsblocks'))
    await c.js("window.__toasts.length=0")
    await row_menu(c, 'A3 see you there', 'Block')
    await confirm(c)
    await toast(c, "/could not block/i")
    said = await c.js("window.__toasts")
    assert not [t for t in said if t.startswith('Blocked')], ('a refused block was reported as done', said)
    await until(c, "(%s)===''" % (BLOCKED % _row('A3 see you there')))
    H.STATE['refuse_docs'].discard((S.pk, 'pcai:smsblocks'))
    e = await S.device()
    await open_texts(e)
    await until(e, F._has_snippet('A3 see you there') + " && PCSms._state().loading===false")
    assert await e.js(BLOCKED % _row('A3 see you there')) == ''
    latest = sorted(S.relay_docs('pcai:smsblocks'), key=lambda e: e['created_at'])[-1:]
    await asyncio.to_thread(handset, S, latest)
    assert phone_blocked(S) == []


def test_block_and_unblock_reach_the_phones_list_and_every_device(ran):
    ran('block_and_unblock_reach_the_phones_list_and_every_device')


async def bubble_menu_delete(b, text):
    await b.js("(()=>{const el=[...document.querySelectorAll('#sms-msgs .bubble[data-doc]')].find(x=>"
               "(x.querySelector('.b-txt')||{}).textContent===%s);"
               "el.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))})()" % json.dumps(text))
    await until(b, "!!document.querySelector('.sms-msg-actions button')")
    await b.js("[...document.querySelectorAll('.sms-msg-actions button')].find(x=>x.textContent.trim()==='Delete').click()")
    await confirm(b)


def _texts(bubbles):
    return [x['text'] for x in bubbles]


@scenario
async def a_deleted_message_stays_deleted_everywhere_and_a_refused_delete_keeps_it(S):
    rows = _history()
    await S.sweep(rows)
    d = H.Device(sk_fill=S.fill)
    b = await d.__aenter__()
    S.open.append(d)
    await open_texts(b)
    await open_thread(b, 'A3 see you there')
    await bubble_menu_delete(b, 'A2 yes, 7pm')
    await toast(b, "/deleted from your archive/")
    await until(b, "(%s).map(x=>x.text).join('|')==='A1 are we still on for tonight|A3 see you there'" % BUBBLES)
    # The phone's next pass over the same provider does not publish it back over the deletion.
    again = await asyncio.to_thread(S.java.ph.run, 'sweep', S.sec, S.state, {'mk': b64(MK), 'rows': rows})
    assert again['events'] == [], ('the phone re-archived what was deleted', len(again['events']))
    await d.reload()
    await open_texts(b)
    await open_thread(b, 'A3 see you there')
    await until(b, "PCSms._state().loading===false")
    assert _texts(await b.js(BUBBLES)) == ['A1 are we still on for tonight', 'A3 see you there'], await b.js(BUBBLES)
    c = await S.device()
    await open_texts(c)
    await open_thread(c, 'A3 see you there')
    await until(c, "PCSms._state().loading===false")
    assert _texts(await c.js(BUBBLES)) == ['A1 are we still on for tonight', 'A3 see you there'], await c.js(BUBBLES)
    # A delete the relay refuses: said, and the message is still there -- here and on another device.
    H.STATE['refuse_docs'].add((S.pk, 'pcai:sms:'))
    await c.js("window.__toasts.length=0")
    await bubble_menu_delete(c, 'A1 are we still on for tonight')
    await toast(c, "/not deleted|could not delete|nothing was changed/i")
    said = await c.js("window.__toasts")
    assert not [t for t in said if t.startswith('deleted')], ('a refused delete was reported as done', said)
    assert _texts(await c.js(BUBBLES)) == ['A1 are we still on for tonight', 'A3 see you there'], await c.js(BUBBLES)
    H.STATE['refuse_docs'].discard((S.pk, 'pcai:sms:'))
    e = await S.device()
    await open_texts(e)
    await open_thread(e, 'A3 see you there')
    await until(e, "PCSms._state().loading===false")
    assert _texts(await e.js(BUBBLES)) == ['A1 are we still on for tonight', 'A3 see you there'], await e.js(BUBBLES)


def test_a_deleted_message_stays_deleted_everywhere_and_a_refused_delete_keeps_it(ran):
    ran('a_deleted_message_stays_deleted_everywhere_and_a_refused_delete_keeps_it')


# ================================================================================================ 3 SEARCH

LONG = 600                   # well past the archive read's first page (256)


def _long_history():
    """Alice: LONG messages, the OLDEST carrying the one rare word; Bob; Dave, whose newest text will
    arrive live with no name on it (archiveIncoming knows no contacts)."""
    base = _ms(60 * 24 * 30)
    rows = [{'address': ALICE, 'body': 'Z0 the ferry to zanzibar leaves at noon', 'date': base, 'type': 1}]
    rows += [{'address': ALICE, 'body': 'filler message %d' % i, 'date': base + (i + 1) * 60_000, 'type': 1 + (i % 2)}
             for i in range(LONG - 1)]
    rows += [{'address': BOB, 'body': 'B0 ' + 'a long one about the delivery window and the driver and the gate code ' * 2
              + 'and the spare key is under the kumquat pot', 'date': _ms(55), 'type': 1},
             {'address': BOB, 'body': 'B1 your package is at the door', 'date': _ms(50), 'type': 1},
             {'address': DAVE, 'body': 'D1 band practice moved', 'date': _ms(40), 'type': 1}]
    return rows


NAMES = {ALICE: 'Alice Liddell', BOB: 'Bob Builder', DAVE: 'Dave Grohl'}


async def search(b, q):
    await b.js("(()=>{const i=document.querySelector('#sms-q');i.focus();i.value=%s;"
               "i.dispatchEvent(new Event('input',{bubbles:true}))})()" % json.dumps(q))
    await until(b, "PCSms._state().q===%s" % json.dumps(q), 10)
    await asyncio.sleep(.2)


WHO = "[...document.querySelectorAll('#feed .sms-thread .sms-who')].map(x=>x.textContent)"
EMPTY = "((document.querySelector('#feed .sms-empty')||{}).textContent||'')"


@scenario
async def a_word_in_the_oldest_page_and_a_contacts_name_are_found(S):
    out = await asyncio.to_thread(S.java.ph.run, 'sweep', S.sec, S.state,
                                  {'mk': b64(MK), 'rows': _long_history(), 'names': NAMES})
    assert not out.get('error') and len(out['events']) == LONG + 3, (out.get('error'), len(out['events']))
    await F.publish_many(out['events'])
    S.phone_ids.update(e['id'] for e in out['events'])
    await S.incoming(DAVE, 'D2 hi again, new number who dis')        # the newest of Dave's: no name on it
    b = await S.device()
    await open_texts(b)
    await until(b, "PCSms._state().loading===false && PCSms._state().msgs.size>=%d" % (LONG + 4), 90)
    # Each conversation is shown by its contact's name -- Dave's too, though his newest text carries none.
    assert sorted(await b.js(WHO)) == ['Alice Liddell', 'Bob Builder', 'Dave Grohl'], await b.js(WHO)
    await search(b, 'zanzibar')
    assert await b.js(WHO) == ['Alice Liddell'], ('a word in the oldest page was not found', await b.js(WHO))
    await search(b, 'kumquat')          # far into a long message, not in its 90-character snippet
    assert await b.js(WHO) == ['Bob Builder'], ('a word late in a long message was not found', await b.js(WHO))
    await search(b, 'liddell')
    assert await b.js(WHO) == ['Alice Liddell'], await b.js(WHO)
    await search(b, 'Grohl')
    assert await b.js(WHO) == ['Dave Grohl'], ('a contact is not found by name', await b.js(WHO))
    await search(b, 'qwertyuiop')
    assert await b.js(WHO) == []
    said = await b.js(EMPTY)
    assert 'No messages match' in said and 'qwertyuiop' in said, ('"no results" not said as such', said)
    assert 'No messages here yet' not in said and 'incomplete' not in said and 'could not' not in said, said
    # The search found the oldest message; opening the conversation from it opens AT it -- drawn,
    # scrolled into view and marked -- not on the newest page with the find behind "Show older".
    await search(b, 'zanzibar')
    await b.js("document.querySelector('#feed .sms-thread').click()")
    await until(b, "!!document.querySelector('#sms-msgs')")
    await until(b, "(%s).some(x=>x.text==='Z0 the ferry to zanzibar leaves at noon')" % BUBBLES, 20)
    seen = await b.js("(()=>{const l=document.querySelector('#sms-msgs .sms-msgs')||document.querySelector('.sms-msgs');"
                      "const e=[...l.querySelectorAll('.bubble[data-doc]')].find(x=>(x.querySelector('.b-txt')||{}).textContent"
                      "==='Z0 the ferry to zanzibar leaves at noon');const a=l.getBoundingClientRect(),r=e.getBoundingClientRect();"
                      "return {inView:r.bottom>a.top&&r.top<a.bottom,hit:e.classList.contains('sms-hit')}})()")
    assert seen == {'inView': True, 'hit': True}, ('the found message is not where the conversation opened', seen)


def test_a_word_in_the_oldest_page_and_a_contacts_name_are_found(ran):
    ran('a_word_in_the_oldest_page_and_a_contacts_name_are_found')


@scenario
async def no_results_while_the_history_is_unreadable_is_said_as_that(S):
    await S.sweep(_history())
    H.STATE['hold_docs'].add((S.pk, 'pcai-sms'))
    b = await S.device()
    await open_texts(b)
    await S.incoming(CAROL, 'C1 call me when you can')
    await until(b, F._has_snippet('C1 call me when you can'))
    await until(b, "/Message history is incomplete/.test(document.querySelector('#feed').innerText)", 40)
    await search(b, 'zanzibar')
    assert await b.js(WHO) == []
    said = await b.js(EMPTY)
    assert 'No messages match' in said and 'zanzibar' in said, said
    assert 'could not be read' in said, ('a search of a history that could not be read reads as a clean "no match"', said)
    assert 'No messages here yet' not in said, said
    # What WAS read is still searched.
    await search(b, 'call me')
    assert len(await b.js(WHO)) == 1


def test_no_results_while_the_history_is_unreadable_is_said_as_that(ran):
    ran('no_results_while_the_history_is_unreadable_is_said_as_that')


if __name__ == '__main__':
    sys.exit(pytest.main([os.path.abspath(__file__), '-q']))
