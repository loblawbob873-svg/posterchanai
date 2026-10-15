"""The PosterChanOS start menu shows WHO YOU ARE in the same frame as the rest of the menu.

"start menu: major lag when the user avatar/account switcher appears in the start menu. this should never
happen, makes 0 sense. Account switcher/avatar should always be there" (2026-10-15). On PosterChanOS the start
menu is its own popup -- a fresh page every time it opens -- and it draws at once, while the saved sign-in is
still being restored. The chip needs `me()`, so it was left out and added by a 150ms poll once the restore
finished: the menu opened, then the avatar and name popped in under the cursor. This opens the popup the way
the OS does -- a page that signed in BEFORE, in the same profile -- and records, with a MutationObserver
installed before any script runs, what the footer held the moment the menu first appeared.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WATCH = r"""window.__seen=null;
  new MutationObserver(()=>{ if(window.__seen) return; const f=document.querySelector('#os-startmenu .os-foot');
    if(!f) return; const a=f.querySelector('.os-acct');
    window.__seen={t:performance.now(),acct:!!a,name:a?a.querySelector('span').textContent.trim():null,
                   img:!!(a&&a.querySelector('img'))}; })
   .observe(document,{subtree:true,childList:true});"""

PROFILE = r'''(async()=>{const me=__PC.me();
  const ev=await __PC.signTemplate({kind:0,pubkey:me.pubkey,created_at:Math.floor(Date.now()/1000),tags:[],
    content:JSON.stringify({name:'Alice Tester',picture:location.origin+'/static/posterchan-relay.png'})});
  Store.saveEvent(ev); Store.saveProfile(ev);
  for(let i=0;i<40&&!Store.profile(me.pubkey);i++) await new Promise(r=>setTimeout(r,50));
  return !!Store.profile(me.pubkey)})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_popup_menu_appears_with_your_avatar_and_name_already_in_it():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-start')")
        assert await b.js(PROFILE), 'the test profile never reached the Store'
        # The desktop draws its own start menu once, as it does on every boot.
        await b.js("document.getElementById('osfr')?.remove();document.getElementById('os-start').click()")
        await b.until("!!document.querySelector('.os-foot .os-acct')")
        url = await b.js("location.href.split('?')[0]")
        await b.call('Page.navigate', {'url': url + '?pcpopup=start'})
        await b.until("!!window.__seen")
        got['first'] = await b.js('window.__seen')
        await b.until("!!document.querySelector('.os-foot .os-acct') && !!(__PC.me&&__PC.me())")
        got['later'] = await b.js("document.querySelector('.os-foot .os-acct span').textContent.trim()")
        got['chips'] = await b.js("document.querySelectorAll('.os-foot .os-acct').length")

    asyncio.run(desktop.with_browser('online', '', check, WATCH))
    first = got['first']
    assert first['acct'], ('the menu appeared without you in it; the chip was added afterwards', got)
    assert first['name'] == 'Alice Tester' and first['img'], ('the first frame must show your name and picture', got)
    assert got['later'] == 'Alice Tester' and got['chips'] == 1, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_remembered_chip_for_another_account_is_replaced_by_the_real_one():
    """The remembered chip is a first frame, never the answer: if it names a different account (a switch in
    another window), the signed-in account replaces it once the popup's own sign-in is back."""
    got = {}
    stale = ("localStorage.setItem('pc_os_me_chip',JSON.stringify({pk:'f'.repeat(64),name:'Somebody Else',src:''}));")

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('.os-foot .os-acct') && __PC.me() && "
                      "document.querySelector('.os-foot .os-acct').dataset.pk===__PC.me().pubkey")
        got['pk'] = await b.js("document.querySelector('.os-foot .os-acct').dataset.pk === __PC.me().pubkey")
        got['name'] = await b.js("document.querySelector('.os-foot .os-acct span').textContent.trim()")
        got['chips'] = await b.js("document.querySelectorAll('.os-foot .os-acct').length")

    asyncio.run(desktop.with_browser('online', '?pcpopup=start', check, stale))
    assert got['pk'] and got['name'] != 'Somebody Else' and got['chips'] == 1, got
