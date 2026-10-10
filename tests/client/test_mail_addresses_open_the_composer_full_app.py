"""Email: clicking an email address in a message opens THIS app's composer, addressed to it.

"Clicking an email address in an Email message should bring up the composer." From/To were one plain
string, a plain-text body had no address links at all, and a mailto: link in an HTML body was handed
to the browser -- which, in the APK and the desktop shell, has no mail handler, so it did nothing.

Drives the SHIPPED bundle with real mouse clicks, at desktop and phone widths:

  * the From address, a To address, a bare address and a `mailto:` in a plain-text body each open
    the composer with that address in To (a mailto's `?subject=` fills the subject);
  * a `mailto:` link inside an HTML body (a sandboxed frame) does the same, and opens no window;
  * the sender's NAME still opens the sender card, and clicking an address does not fold the
    message away.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.__opened=[];const _wo=window.open;window.open=(...a)=>{__opened.push(String(a[0]));return null;};
const originalFetch=window.fetch;
const base={account:'me@home.test',folder:'INBOX',ts:100,read:true,attachments:0,preview:'…'};
window.__inbox=[
 Object.assign({},base,{uid:'1',subject:'Plain one',from:'Alice Smith <alice@x.test>',from_email:'alice@x.test',
   to:'me@home.test, "Bob, Jr." <bob@y.test>',
   body_text:'Please write to carol@z.test (or mailto:dave@w.test?subject=Hello%20there) today.'}),
 Object.assign({},base,{uid:'2',ts:90,subject:'Html one',from:'news@n.test',from_email:'news@n.test',to:'me@home.test',
   body_text:'', body_html:'<p style="font-size:20px">Questions? <a id="ml" href="mailto:help@n.test?subject=Order%2042">Mail Erin</a></p>'})];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
const p=u.pathname;
if(p.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(p.endsWith('/folders'))return reply({folders:['INBOX','Sent'],sent:'Sent'});
if(p.endsWith('/messages'))return reply({messages:u.searchParams.get('folder')==='INBOX'?__inbox:[],next_until:0});
if(p.endsWith('/message'))return reply({message:__inbox.find(y=>y.uid===u.searchParams.get('uid'))});
if(p.endsWith('/thread'))return reply({messages:[]});
return reply({ok:true});};
__PC.switchView('mail');'''

COMPOSER = ("(()=>{const t=document.querySelector('#cm-to');const s=document.querySelector('#cm-subj');"
            "return t?{to:t.value,subj:s?s.value:''}:null})()")


async def close_composer(b):
    await b.js("(window.__PC&&__PC.closeModal?__PC.closeModal():document.querySelector('#modal-root').innerHTML=''); true")
    await b.until("!document.querySelector('#cm-to')")


async def composer_after(b, selector):
    await b.until(f"!!document.querySelector({selector!r})")
    await b.js(f"document.querySelector({selector!r}).scrollIntoView({{block:'center'}}); true")
    # The first line box: a long link WRAPS on a phone, and the centre of its whole rectangle can
    # fall between its two halves.
    pt = await b.js(f"(()=>{{const r=document.querySelector({selector!r}).getClientRects()[0];return {{x:r.x+Math.min(r.width/2,20),y:r.y+r.height/2}}}})()")
    assert await b.js(f"document.elementFromPoint({pt['x']},{pt['y']})?.closest({selector!r})!==null"), selector
    await b.call('Input.dispatchMouseEvent', dict(type='mousePressed', button='left', clickCount=1, **pt))
    await b.call('Input.dispatchMouseEvent', dict(type='mouseReleased', button='left', clickCount=1, **pt))
    await b.until("!!document.querySelector('#cm-to')")
    got = await b.js(COMPOSER)
    await close_composer(b)
    return got


async def open_row(b, subject, phone):
    if phone and await b.js("document.querySelector('#mail-read').classList.contains('has-open')"):
        await click(b, '#mail-back')
    await b.js(f"[...document.querySelectorAll('#mail-items .mail-item')].find(e=>e.textContent.includes({subject!r})).querySelector('.mi-content').click(); true")
    await b.until(f"(document.querySelector('#mail-read .mr-subj')||{{}}).textContent==={subject!r}")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_every_address_in_a_message_opens_the_composer(width):
    phone = width < 600
    out = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=phone))
        await desktop.login(b)
        await b.js(STUB)
        await b.until("document.querySelectorAll('#mail-items .mail-item').length===2")
        await open_row(b, 'Plain one', phone)

        out['from'] = await composer_after(b, '#mail-read a.mm-from-addr')
        out['folded'] = await b.js("!document.querySelector('#mail-read .mail-msg').classList.contains('open')")
        out['to'] = await composer_after(b, '#mail-read .mm-to a[data-mailto="bob@y.test"]')
        out['bare'] = await composer_after(b, '#mail-read .mail-text a[data-mailto="carol@z.test"]')
        out['mailto'] = await composer_after(b, '#mail-read .mail-text a[data-mailto="dave@w.test"]')
        out['body_text'] = await b.js("document.querySelector('#mail-read .mail-text').textContent")

        # The name is still the sender card.
        await click(b, '#mail-read b.mm-sender')
        await b.until("!!document.querySelector('#msc-write')")
        await b.js("(window.__PC&&__PC.closeModal?__PC.closeModal():document.querySelector('#modal-root').innerHTML=''); true")
        await b.until("!document.querySelector('#msc-write')")

        # An HTML body: the mailto: is inside a sandboxed frame.
        await open_row(b, 'Html one', phone)
        await b.until("(()=>{const f=document.querySelector('#mail-read iframe.mail-html');return !!(f&&f.contentDocument&&f.contentDocument.querySelector('#ml'))})()")
        await asyncio.sleep(.3)
        await b.js("document.querySelector('#mail-read iframe.mail-html').scrollIntoView({block:'center'}); true")
        await asyncio.sleep(.2)
        pt = await b.js("(()=>{const f=document.querySelector('#mail-read iframe.mail-html');const fr=f.getBoundingClientRect();"
                        "const r=f.contentDocument.querySelector('#ml').getBoundingClientRect();"
                        # the windowed desktop zooms <body>; a frame's own coordinates are not zoomed
                        "const k=fr.width/(f.offsetWidth||fr.width);return {x:fr.x+(r.x+r.width/2)*k,y:fr.y+(r.y+r.height/2)*k}})()")
        targets = len((await b.call('Target.getTargets'))['targetInfos'])
        await b.call('Input.dispatchMouseEvent', dict(type='mousePressed', button='left', clickCount=1, **pt))
        await b.call('Input.dispatchMouseEvent', dict(type='mouseReleased', button='left', clickCount=1, **pt))
        await b.until("!!document.querySelector('#cm-to')")
        out['html'] = await b.js(COMPOSER)
        await asyncio.sleep(.3)
        out['popups'] = len((await b.call('Target.getTargets'))['targetInfos']) - targets
        out['opened'] = await b.js('__opened')
        out['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
    assert out['from'] == {'to': 'alice@x.test', 'subj': ''}, out
    assert out['folded'] is False, 'clicking an address folded the message away'
    assert out['to']['to'] == 'bob@y.test', out
    assert out['bare']['to'] == 'carol@z.test', out
    assert out['mailto'] == {'to': 'dave@w.test', 'subj': 'Hello there'}, out
    assert 'carol@z.test' in out['body_text'] and 'today.' in out['body_text'], out
    assert out['html'] == {'to': 'help@n.test', 'subj': 'Order 42'}, out
    assert out['popups'] == 0 and out['opened'] == [], ('a mailto: opened a window', out)
    assert not out['errors'], out['errors']
