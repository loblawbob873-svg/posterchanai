"""Texts ✨ offers a few different replies in a popup, and the one you pick goes into the composer.

Asked for: "improve the ai reply for SMS to give you a few choices in maybe a menu popup". Driven in
the REAL bundled Texts renderer at phone and desktop width; only /api/texts/ai-reply is a fixture.
Picking only ever fills the composer — nothing is sent — and dismissing the menu changes nothing.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_ai_reply_full_app import SEED, NET, _ready

CHOICES = ["Yes! See you at 7",
           "Sounds great — want me to bring anything? I can grab dessert on the way over if that helps",
           "Can we push it to 7:30?"]

ANSWER = r"""
__PC.authFetch=async(url,opts={})=>{
  const body=JSON.parse(opts.body||'{}');
  if(body.probe)return new Response(JSON.stringify({ok:true,allowed:true}),{status:200});
  aiCalls.push(body);
  return new Response(JSON.stringify({ok:true,content:CHOICES[0],choices:CHOICES}),{status:200});
};
"""

POP = r"""(()=>{const p=document.querySelector('.menu-pop');if(!p)return null;const r=p.getBoundingClientRect();
  const items=[...p.querySelectorAll('button[data-m]')];
  return {texts:items.map(b=>b.textContent),x:r.left,right:r.right,top:r.top,bottom:r.bottom,vw:innerWidth,vh:innerHeight,
          wraps:items.map(b=>getComputedStyle(b).whiteSpace),tallest:Math.max(...items.map(b=>b.getBoundingClientRect().height))};})()"""


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_sparkle_offers_choices_and_the_pick_fills_the_composer(phone):
    async def check(b):
        w, h = (390, 844) if phone else (1280, 900)
        await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
        await _ready(b)
        await b.js("window.CHOICES=" + json.dumps(CHOICES) + ";" + ANSWER)
        await b.js("document.querySelector('#sms-ai').click()")
        await b.until("!!document.querySelector('.menu-pop')")
        await asyncio.sleep(.4)            # a phone's menu is a bottom sheet that slides up for 0.2s
        assert (await b.js("aiCalls[0]"))['count'] == 3
        pop = await b.js(POP)
        assert pop['texts'] == CHOICES, pop
        assert pop['x'] >= 0 and pop['right'] <= pop['vw'] + 0.5 and pop['bottom'] <= pop['vh'] + 0.5, pop
        assert set(pop['wraps']) == {'normal'}, 'a long reply is one clipped line'
        # The app's own font, not the browser's default serif (menus hang off <html>, not <body>).
        font = await b.js("getComputedStyle(document.querySelector('.menu-pop button[data-m]')).fontFamily")
        assert font.split(',')[0].strip().strip('\"\'') == await b.js("getComputedStyle(document.body).fontFamily.split(',')[0].trim().replace(/[\"']/g,'')"), font
        assert await b.js("document.querySelector('#sms-in').value") == '', 'something was filled before a pick'
        await b.js("document.querySelectorAll('.menu-pop button[data-m]')[1].click()")
        await b.until("document.querySelector('#sms-in').value.length>0")
        assert await b.js("document.querySelector('#sms-in').value") == CHOICES[1]
        assert await b.js("sends.length") == 0, 'a pick sent the text'
        assert not await b.js("!!document.querySelector('.menu-pop')")
        await b.until("!document.querySelector('#sms-ai').disabled")
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_dismissing_the_choices_changes_nothing():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
        await _ready(b)
        await b.js("window.CHOICES=" + json.dumps(CHOICES) + ";" + ANSWER)
        await b.js("const i=document.querySelector('#sms-in');i.value='my own words';i.dispatchEvent(new Event('input'))")
        await b.js("document.querySelector('#sms-ai').click()")
        await b.until("!!document.querySelector('.menu-pop')")
        await b.js("document.querySelector('.sms-msgs,#feed').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        await b.until("!document.querySelector('.menu-pop')")
        await b.until("!document.querySelector('#sms-ai').disabled")
        assert await b.js("document.querySelector('#sms-in').value") == 'my own words'
        assert await b.js("sends.length") == 0
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
