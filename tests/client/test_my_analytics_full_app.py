"""My Analytics shows the numbers a person's relays return (2026-10-22: every screen gets a browser test).

The screen is computed in the browser from relay answers: your posts, then the reactions/replies/reposts/zaps that
reference them, then tips to you. Fed a known set of events through the real __PC.relayQuery seam, it must SAY the
right numbers, re-ask when the range changes, offer a working retry when the relays fail, and greet an account with
no posts. Real login, DOM and user-analytics.js; only relayQuery is a fixture.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FEED = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''

SETUP = r'''(()=>{
  const me=__PC.me().pubkey, now=Math.floor(Date.now()/1000), other='b'.repeat(64);
  const post=(id,at)=>({id,pubkey:me,kind:1,created_at:at,content:'post '+id,tags:[],sig:'s'});
  const on=(id,kind,extra)=>({id:id,pubkey:other,kind,created_at:now-60,content:extra&&extra.content||'+',tags:[['e',extra.e],['p',me],...((extra&&extra.tags)||[])],sig:'s'});
  window.__ua={mode:'data',asks:[]};
  const P1='1'.repeat(64),P2='2'.repeat(64);
  __PC.relayQuery=async(filters)=>{
    __ua.asks.push(JSON.parse(JSON.stringify(filters)));
    if(__ua.mode==='fail') throw new Error('relays down');
    const f=filters[0];
    if(__ua.mode==='empty') return [];
    if(f.authors) return [post(P1,now-3600),post(P2,now-7200)];
    if(f['#e']) return [on('r1'.padEnd(64,'0'),7,{e:P1}),on('r2'.padEnd(64,'0'),7,{e:P1}),on('r3'.padEnd(64,'0'),7,{e:P2}),
                        on('re'.padEnd(64,'0'),1,{e:P1,content:'nice'}),on('rp'.padEnd(64,'0'),6,{e:P2})];
    if(f['#t']) return [{id:'tp'.padEnd(64,'0'),pubkey:other,kind:1,created_at:now-30,content:'tip',sig:'s',
                         tags:[['p',me],['e',P1],['t','monerotip'],['amount_xmr','0.01']]}];
    return [];
  };
  __PC.switchView('analytics'); return 1;
})()'''

METRICS = "Object.fromEntries([...document.querySelectorAll('.ua-metric')].map(m=>[m.querySelector('span').textContent.trim(),m.querySelector('strong').textContent.replace(/\\s+/g,' ').trim()]))"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_my_analytics_says_the_right_numbers_and_every_control_works():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.ua-metric').length>0")
        m = await b.js(METRICS)
        assert m["Posts"] == "2", m
        assert m["Reactions"] == "3", m
        assert m["Replies"] == "1", m                      # the tip is NOT a reply
        assert m["Reposts"] == "1", m
        assert m["Monero tips"].startswith("0.01"), m
        # The range buttons re-ask the relays for that window.
        before = await b.js("__ua.asks.length")
        await b.js("document.querySelector('[data-days=\"7\"]').click(); 1")
        await b.until(f"__ua.asks.length>{before} && document.querySelector('[data-days=\"7\"]').classList.contains('on')")
        since = await b.js("__ua.asks[__ua.asks.length-1][0].since||__ua.asks.find((a,i)=>i>=%d&&a[0].authors)[0].since" % before)
        assert abs((await b.js("Math.floor(Date.now()/1000)")) - since - 7 * 86400) < 120, since
        # Relays failing: an error card with a retry that works.
        await b.js("__ua.mode='fail'; document.querySelector('[data-days=\"30\"]').click(); 1")
        await b.until("!!document.querySelector('#ua-retry')")
        await b.js("__ua.mode='data'; document.querySelector('#ua-retry').click(); 1")
        await b.until("document.querySelectorAll('.ua-metric').length>0")
        # No posts: the greeting, never a page of zeros.
        await b.js("__ua.mode='empty'; document.querySelector('[data-days=\"90\"]').click(); 1")
        await b.until("/Publish your first post/.test(document.querySelector('#feed').innerText)")
    asyncio.run(desktop.with_browser('online', '', check, FEED))
