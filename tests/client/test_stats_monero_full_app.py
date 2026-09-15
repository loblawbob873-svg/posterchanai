"""Real bundled UI renders local Monero activity and changes range correctly."""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.test_stats_monero_zaps import snapshot


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_server_stats_monero_cards_follow_range_and_distinguish_lightning():
    async def check(browser):
        await desktop.login(browser)
        await browser.until("!!document.querySelector('#os-start')")
        await browser.js("document.querySelector('#os-start').click()")
        await browser.until("!!document.querySelector('#os-startmenu [data-view=stats]')")
        await browser.js("document.querySelector('#os-startmenu [data-view=stats]').click()")
        await browser.until("document.querySelector('.st-wrap')?.textContent.includes('Monero zaps')")
        for key, expected in [('hour',3),('minute',2),('day',4)]:
            target = await browser.js("""(()=>{
              const button=document.querySelector('[data-range="""+key+""" ]');
              button.scrollIntoView({block:'center',inline:'center'});
              const r=button.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
              return {x,y,hit:button.contains(document.elementFromPoint(x,y))};
            })()""")
            assert target['hit'], 'range control is covered or clipped'
            for event_type in ('mousePressed','mouseReleased'):
                await browser.call('Input.dispatchMouseEvent',dict(type=event_type,
                    x=target['x'],y=target['y'],button='left',clickCount=1))
            cards = await browser.js("[...document.querySelectorAll('.st-card')].map(e=>({label:e.querySelector('.st-lbl')?.textContent,value:e.querySelector('.st-num')?.textContent,visible:e.getBoundingClientRect().width>0}))")
            monero = [c for c in cards if c['label']=='Monero zaps']
            assert monero == [dict(label='Monero zaps',value=str(expected),visible=True)]
            assert [c['value'] for c in cards if c['label']=='Lightning zaps'] == ['1']
            tiles = await browser.js("[...document.querySelectorAll('.st-tile')].map(e=>({label:e.querySelector('.st-tlbl').textContent,value:e.querySelector('.st-tval').textContent}))")
            assert [t['value'] for t in tiles if t['label'].startswith('Monero zaps ')] == [str(expected)]
        # During a rolling upgrade an old API payload must say unavailable,
        # rather than silently presenting an unsupported count as zero.
        await browser.js("Object.values(__statsFixture.windows).forEach(w=>delete w.series.monero_zaps);PCStats.render()")
        await browser.until("[...document.querySelectorAll('.st-tile')].some(e=>e.textContent.includes('Monero zaps')&&e.querySelector('.st-tval').textContent==='—')")
    init = 'window.__statsFixture='+json.dumps({'windows':snapshot()})+';'+r'''
const statsFetch=window.fetch;
window.fetch=(url,opts)=>String(url).includes('/client/server-stats')
  ? Promise.resolve(new Response(JSON.stringify(__statsFixture),{status:200,headers:{'Content-Type':'application/json'}}))
  : statsFetch(url,opts);
'''
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))
