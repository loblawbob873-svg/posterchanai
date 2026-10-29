"""Tapping a contact SHOWS it ("android contacts app: it goes into edit mode soon as you click on a
contact. we need a nice display mode"). The shipped bundle at phone size, a stubbed contacts API:

  * a tap draws the contact's CARD -- no form fields: photo, name, title/company, every number with its
    label (vCard TYPE, or Apple's `itemN.X-ABLABEL`), email, address, dates, web, chat, people, notes;
  * every control on it does its job: Call is a tel: link, Text opens the APK's Texts (Sms plugin) or,
    with no plugin, the client's Texts screen; Email goes to the client's Mail (or a mailto: link with no
    Mail), an address to a map, a URL out; tapping a value copies it (through copyValue);
  * Edit opens the existing editor, Save comes back to the CARD with the stored value;
  * Back (the arrow and Android's back button) returns to the list at the same scroll and search;
  * a contact with no photo shows initials; "Add contact" still opens the editor directly.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_contacts_buttons_full_app import BOUNDARIES as BASE

SHOTS = Path(__file__).resolve().parents[2] / 'tmp'
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=='
ZOE = '\r\n'.join([
    'BEGIN:VCARD', 'VERSION:3.0', 'UID:zoe', 'FN:Zoe Quinn', 'N:Quinn;Zoe;;;', 'NICKNAME:Z',
    'ORG:Neon Labs', 'TITLE:Lead Hacker', 'TEL;TYPE=CELL:+1 555 0101', 'item1.TEL:+15550202',
    'item1.X-ABLABEL:Boat', 'EMAIL;TYPE=INTERNET,WORK:zoe@neon.example',
    'ADR;TYPE=HOME:;;1 Grid St;Night City;CA;94000;USA', 'BDAY:1990-04-02', 'URL:https://zoe.example/',
    'IMPP:xmpp:zoe@chat.example', 'item2.X-ABRELATEDNAMES:Max', 'item2.X-ABLABEL:_$!<Brother>!$_',
    'NOTE:Met at the con', 'PHOTO;ENCODING=b;TYPE=PNG:' + PNG, 'END:VCARD', ''])

BOUNDARIES = BASE + r'''
window.__back=null;window.__sms=[];window.__mail=[];window.__links=[];
window.Capacitor={isNativePlatform:()=>false,Plugins:{App:{addListener:(n,fn)=>{if(n==='backButton')window.__back=fn;return {remove(){}};}}}};
const filler=[...Array(40).keys()].map(i=>{const n=String(i).padStart(2,'0');
  return {uid:'p'+n,ics:['BEGIN:VCARD','VERSION:3.0','UID:p'+n,'FN:A'+n+' Person','N:Person;A'+n+';;;','TEL:+1555100'+n,'END:VCARD',''].join('\r\n')};});
window.__cards.main=window.__cards.main.concat(filler,[{uid:'zoe',ics:%ZOE%}]);
// Every link the card follows (tel:, mailto:, a map, a web page) is recorded instead of leaving the page.
addEventListener('click',e=>{const a=e.target&&e.target.closest&&e.target.closest('a[href]');
  if(a&&!a.href.startsWith(location.origin)){e.preventDefault();__links.push({href:a.href,blank:a.target==='_blank'});}},true);
const cardsFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=new URL(String(url),location.href);
 if(u.pathname==='/api/contacts/cards'&&String(opts.method||'').toUpperCase()==='PUT'){
   const b=JSON.parse(opts.body);__cards[b.book]=(__cards[b.book]||[]).filter(c=>c.uid!==b.uid).concat([{uid:b.uid,ics:b.vcf}]);
   __calls.push({op:'PUT /api/contacts/cards',body:b});
   return new Response(JSON.stringify({ok:true}),{status:200,headers:{'Content-Type':'application/json'}});
 }
 return cardsFetch(url,opts);
};
'''.replace('%ZOE%', json.dumps(ZOE))

CARD = "document.querySelector('#feed .ctc')"
ROW = "document.querySelector('.ct-row[data-uid=zoe]')"


async def press(b, sel, i=None):
    q = f"document.querySelectorAll({json.dumps(sel)})[{i}]" if i is not None else f"document.querySelector({json.dumps(sel)})"
    assert await b.js(f"!!{q}"), f'{sel} is not on screen'
    await b.js(f"{q}.click(); 1")


async def open_zoe(b):
    await b.until(f"!!{ROW}")
    await b.js(f"{ROW}.click(); 1")
    await b.until(f"!!{CARD} && !!document.querySelector('#ctc-edit')")


def _run(check):
    asyncio.run(desktop.with_browser('online', '', check, BOUNDARIES))


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_tap_shows_the_contact_and_every_control_works():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('contacts'); 1")
        await b.until(f"!!{ROW}")
        # The list scrolled down to her, so Back has a position to keep.
        await b.js(f"{ROW}.scrollIntoView({{block:'center'}}); 1")
        await asyncio.sleep(.3)
        top = await b.js("document.querySelector('#feed').scrollTop")
        assert top > 200, top
        await open_zoe(b)

        # A CARD, not a form.
        card = await b.js(f"""(()=>{{const c={CARD};const t=s=>[...c.querySelectorAll(s)].map(e=>e.textContent.trim());
          return {{inputs:c.querySelectorAll('input,textarea,select').length, modal:!!document.querySelector('#modal-root .modal'),
            name:t('.ctc-name'), org:t('.ctc-org'), photo:(c.querySelector('img.ctc-face')||{{}}).src||'',
            tels:t('.ctc-tel .ctc-v'), telLabels:t('.ctc-tel .ctc-l'), emails:t('.ctc-email .ctc-v'), emailLabels:t('.ctc-email .ctc-l'),
            adr:t('.ctc-adr .ctc-v'), dates:t('.ctc-date .ctc-l'), urls:t('.ctc-url .ctc-v'), ims:t('.ctc-im .ctc-v'),
            rel:t('.ctc-rel'), note:t('.ctc-note .ctc-v'), sections:t('.ctc-sh'),
            wide:document.documentElement.scrollWidth>innerWidth+1}}}})()""")
        assert card['inputs'] == 0 and not card['modal'], card
        assert card['name'] == ['Zoe Quinn'], card
        assert '“Z”' in card['org'] and 'Lead Hacker · Neon Labs' in card['org'], card
        assert card['photo'].startswith('data:image/png;base64,'), card
        assert card['tels'] == ['+1 555 0101', '+15550202'] and card['telLabels'] == ['mobile', 'Boat'], card
        assert card['emails'] == ['zoe@neon.example'] and card['emailLabels'] == ['work'], card
        assert card['adr'] == ['1 Grid St\nNight City CA 94000\nUSA'], card
        assert card['dates'] == ['birthday'] and card['urls'] == ['https://zoe.example/'], card
        assert card['ims'] == ['zoe@chat.example'] and card['rel'] == ['MaxBrother'], card
        assert card['note'] == ['Met at the con'], card
        assert card['sections'] == ['Phone', 'Email', 'Address', 'Dates', 'Web', 'Chat', 'People', 'Notes'], card
        assert not card['wide'], card
        SHOTS.mkdir(exist_ok=True)
        await desktop_shot(b, SHOTS / 'contact-card-390.png')

        # Call: a real tel: link, encoded (a `#` would otherwise be cut off as a fragment).
        await press(b, '.ctc-call', 1)
        await press(b, '#ctc-qcall')
        # Email, a map and the web page.
        await b.js("document.querySelector('.nav-item[data-view=\"mail\"]')?.classList.add('hidden'); 1")
        await press(b, '.ctc-mail', 0)
        await press(b, '.ctc-map', 0)
        await press(b, '.ctc-open', 0)
        links = await b.js("__links")
        assert links == [
            {'href': 'tel:%2B15550202', 'blank': False}, {'href': 'tel:%2B1%20555%200101', 'blank': False},
            {'href': 'mailto:zoe@neon.example', 'blank': False},
            {'href': 'https://www.openstreetmap.org/search?query=1%20Grid%20St%2C%20Night%20City%20CA%2094000%2C%20USA', 'blank': True},
            {'href': 'https://zoe.example/', 'blank': True}], links
        # With Mail on this instance, Email composes there instead.
        await b.js("(()=>{let n=document.querySelector('.nav-item[data-view=\"mail\"]');if(!n){n=document.createElement('button');"
                   "n.className='nav-item';n.dataset.view='mail';document.body.appendChild(n);}n.classList.remove('hidden');"
                   "__PC.openMailto=h=>{__mail.push(h);};})(); 1")
        await press(b, '#ctc-qmail')
        assert await b.js("__mail") == ['mailto:zoe@neon.example']

        # Text in the APK: the Sms plugin opens the conversation.
        await b.js("Capacitor.Plugins.Sms={openConversation:async o=>{__sms.push(o.address);return {opened:true};}}; 1")
        await press(b, '.ctc-text', 1)
        await press(b, '#ctc-qtext')
        await b.until("__sms.length===2")
        assert await b.js("__sms") == ['+15550202', '+1 555 0101']

        # Tapping a value copies it.
        for sel, want in (('.ctc-tel', '+1 555 0101'), ('.ctc-email', 'zoe@neon.example'), ('.ctc-adr', '1 Grid St\nNight City CA 94000\nUSA'),
                          ('.ctc-url', 'https://zoe.example/'), ('.ctc-im', 'zoe@chat.example'), ('.ctc-rel', 'Max'),
                          ('.ctc-note', 'Met at the con'), ('.ctc-date', None)):
            n = await b.js("__copied.length")
            await press(b, sel, 0)
            await b.until(f"__copied.length==={n + 1}")
            got = await b.js("__copied[__copied.length-1]")
            assert (got == want) if want else ('1990' in got), (sel, got)

        # Edit -> Save -> back on the CARD, showing the stored value.
        await press(b, '#ctc-edit')
        await b.until("!!document.querySelector('#cc-save') && !!document.querySelector('#cc-org')")
        await b.js("document.querySelector('#cc-org').value='Neon Labs 2'; document.querySelector('#cc-save').click(); 1")
        await b.until(f"!document.querySelector('#cc-save') && !!{CARD} && {CARD}.innerText.includes('Neon Labs 2')")
        put = await b.js("__calls.filter(c=>c.op==='PUT /api/contacts/cards').pop().body.vcf")
        assert 'ORG:Neon Labs 2' in put and 'item1.X-ABLABEL:Boat' in put and 'PHOTO;' in put, put
        assert await b.js(f"{CARD}.querySelectorAll('input,textarea').length") == 0

        # Back -> the list, where it was.
        await press(b, '#ctc-back')
        await b.until(f"!{CARD} && !!{ROW}")
        await asyncio.sleep(.3)
        assert abs(await b.js("document.querySelector('#feed').scrollTop") - top) <= 2

        # Android's back button does the same, and keeps the search.
        await b.js("(()=>{const q=document.querySelector('#ct-q');q.value='quinn';q.dispatchEvent(new Event('input',{bubbles:true}));})(); 1")
        await b.until("document.querySelectorAll('.ct-row').length===1")
        await open_zoe(b)
        assert await b.js("typeof __back") == 'function'
        await b.js("__back(); 1")
        await b.until(f"!{CARD} && document.querySelectorAll('.ct-row').length===1")
        assert await b.js("document.querySelector('#ct-q').value") == 'quinn'

        # Text on the web (no Sms plugin): the client's own Texts screen, with the number.
        await open_zoe(b)
        await b.js("delete Capacitor.Plugins.Sms; 1")
        await press(b, '.ctc-text', 0)
        await b.until("__PC.VIEW==='texts'")
        assert await b.js("window.__PC_SMS_OPEN_ADDRESS||__sms.length") in ('+1 555 0101', 2)
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_photo_shows_initials_and_add_contact_still_opens_the_editor():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 360, 'height': 780, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('contacts'); 1")
        await b.until("!!document.querySelector('.ct-row[data-uid=alice]')")
        await b.js("document.querySelector('.ct-row[data-uid=alice]').click(); 1")
        await b.until(f"!!{CARD}")
        assert await b.js(f"{CARD}.querySelector('.ctc-init').textContent") == 'AS'
        assert await b.js(f"!{CARD}.querySelector('img.ctc-face')")
        assert await b.js(f"[...{CARD}.querySelectorAll('.ctc-sh')].map(e=>e.textContent)") == ['Phone']
        await press(b, '#ctc-back')
        await b.until("!!document.querySelector('#ct-new')")
        await press(b, '#ct-new')
        await b.until("!!document.querySelector('#cc-save')")
        assert await b.js(f"!{CARD}")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


async def desktop_shot(b, path):
    import base64
    r = await b.call('Page.captureScreenshot', {'format': 'png'})
    path.write_bytes(base64.b64decode(r['data']))
