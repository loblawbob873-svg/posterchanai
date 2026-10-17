"""SEVERAL EMOJI REACTIONS PER PERSON ON ONE POST, through the real bundled Social UI.

What the user SEES: a bar of emoji chips under the post (grouped by emoji, counted by people, yours
highlighted); the react button adds another emoji instead of taking the only one back; a chip
removes exactly that one reaction (a NIP-09 kind-5 naming only it, `k`=7) and the other chips stay.
A refused deletion leaves the chip where it was. The bar fits a 390px phone and a desktop card.

Only the network boundary is a fixture (the fake relay records every EVENT it is sent)."""
import asyncio
import base64
import os
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

SHOTS = Path(os.environ.get('PC_SHOT_DIR', '/tmp'))


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CARD = "document.querySelector('.note[data-id=\"'+__post.id+'\"]')"


def _chips(b):
    return b.js(CARD + "&&[..." + CARD + ".querySelectorAll('.rx-chip')].map(c=>({k:c.dataset.rx,n:c.querySelector('.rx-n').textContent,on:c.classList.contains('on')}))")


async def _pick(b, emoji):
    await b.js(CARD + ".querySelector('.act[data-a=react]').click()")
    sel = ".emoji-pop [data-e=\"%s\"]" % emoji
    await b.until("!!document.querySelector('%s')" % sel)
    await b.js("document.querySelector('%s').dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}))" % sel)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_two_emoji_on_one_post_and_remove_one(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 640})
        await desktop.login(b)
        await b.js(r'''(()=>{
          const author=new Uint8Array(32).fill(1),stranger=new Uint8Array(32).fill(3),now=Math.floor(Date.now()/1000)-60;
          // My own post: Home shows it with no follow list to fake; reacting to it is the same code path.
          window.__post=NostrTools.finalizeEvent({kind:1,created_at:now,content:'React to me twice',tags:[]},author);
          // A stranger already put a 🔥 on it: my 🔥 must JOIN that chip, not make a second one.
          window.__theirs=NostrTools.finalizeEvent({kind:7,created_at:now+1,content:'🔥',tags:[['e',__post.id],['p',__post.pubkey]]},stranger);
          Store.saveEvent(__post);Store.saveEvent(__theirs);__publishOK=true;
          ['👍','🎉','🤔','💯','👀','😮','🙏','🚀','💀','🥳','😎','🤯','✨','👏'].forEach((x,i)=>{
            Store.saveEvent(NostrTools.finalizeEvent({kind:7,created_at:now+2+i,content:x,tags:[['e',__post.id],['p',__post.pubkey]]},new Uint8Array(32).fill(10+i)));});
          __PC.timelineTop('home');
        })()''')
        await b.until("!!" + CARD + "&&!!" + CARD + ".querySelector('.rx-chip')")
        first = await _chips(b)
        assert len(first) == 15 and {'k': '🔥', 'n': '1', 'on': False} in first and not any(c['on'] for c in first), first

        await _pick(b, '🔥')
        await b.until("__published.filter(e=>e.kind===7).length===1")
        await b.until(CARD + ".querySelectorAll('.rx-chip.on').length===1")
        await _pick(b, '😂')
        await b.until("__published.filter(e=>e.kind===7).length===2")
        await b.until(CARD + ".querySelectorAll('.rx-chip.on').length===2")
        sent = await b.js("__published.filter(e=>e.kind===7).map(e=>e.content)")
        assert sorted(sent) == sorted(['🔥', '😂']), sent
        assert await b.js("__published.filter(e=>e.kind===5).length") == 0, 'adding a second emoji took the first one back'
        chips = await _chips(b)
        mine = {c['k']: (c['n'], c['on']) for c in chips if c['on']}
        assert mine == {'🔥': ('2', True), '😂': ('1', True)}, chips
        assert chips[0]['k'] == '🔥', 'the most-used emoji leads the bar'

        # Overflow: a post with many different emoji wraps inside its card on a phone AND on a desktop.
        fit = await b.js("(()=>{const n=" + CARD + ",bar=n.querySelector('.rx-bar'),nr=n.getBoundingClientRect(),br=bar.getBoundingClientRect();"
                         "const chips=[...bar.querySelectorAll('.rx-chip')].map(c=>c.getBoundingClientRect());"
                         "return {doc:document.documentElement.scrollWidth<=innerWidth+1,bar:bar.scrollWidth<=bar.clientWidth+1,"
                         "inside:br.right<=nr.right+1&&br.left>=nr.left-1,chipsInside:chips.every(r=>r.right<=nr.right+1&&r.left>=nr.left-1),n:chips.length}})()")
        assert fit['doc'] and fit['bar'] and fit['inside'] and fit['chipsInside'], fit
        await b.js(CARD + ".scrollIntoView({block:'center'})")
        await asyncio.sleep(.3)
        shot = await b.call('Page.captureScreenshot', {'format': 'png'})
        (SHOTS / f'multireact_{width}.png').write_bytes(base64.b64decode(shot['data']))

        # Remove ONE: a refused deletion keeps the chip lit; an accepted one removes only 😂.
        laugh = await b.js("__published.find(e=>e.kind===7&&e.content==='😂').id")
        fire = await b.js("__published.find(e=>e.kind===7&&e.content==='🔥').id")
        await b.js("__publishOK=false;" + CARD + ".querySelector('.rx-chip[data-rx=\"😂\"]').click()")
        await b.until("__published.filter(e=>e.kind===5).length===1")
        await b.until("!" + CARD + ".querySelector('.rx-chip[data-rx=\"😂\"]').disabled")
        assert await b.js("Store.has(%r)" % laugh)
        assert await b.js(CARD + ".querySelector('.rx-chip[data-rx=\"😂\"]').classList.contains('on')")
        await b.js("__publishOK=true;" + CARD + ".querySelector('.rx-chip[data-rx=\"😂\"]').click()")
        await b.until("__published.filter(e=>e.kind===5).length===2")
        await b.until("!" + CARD + ".querySelector('.rx-chip[data-rx=\"😂\"]')")
        dele = await b.js("__published.filter(e=>e.kind===5)[1]")
        assert [t[1] for t in dele['tags'] if t[0] == 'e'] == [laugh], dele['tags']
        assert ['k', '7'] in dele['tags'], dele['tags']
        assert await b.js("Store.has(%r)" % fire), 'removing one emoji removed the other'
        chips = {c['k']: (c['n'], c['on']) for c in await _chips(b)}
        assert chips.get('🔥') == ('2', True), chips
        assert '😂' not in chips
        # Clicking a stranger's chip ADDS that emoji for me (a new kind-7 of it).
        await b.js(CARD + ".querySelector('.rx-chip[data-rx=\"👍\"]').click()")
        await b.until("__published.filter(e=>e.kind===7).length===3")
        assert await b.js("__published.filter(e=>e.kind===7)[2].content") == '👍'
        await b.until(CARD + ".querySelector('.rx-chip[data-rx=\"👍\"]').classList.contains('on')")
        assert await b.js(CARD + ".querySelector('.rx-chip[data-rx=\"👍\"] .rx-n').textContent") == '2'
    extra = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''
    asyncio.run(desktop.with_browser('online', '', check, extra))
