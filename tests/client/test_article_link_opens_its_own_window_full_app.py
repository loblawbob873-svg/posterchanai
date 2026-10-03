"""An article linked from a Social post opens as its OWN document, not over the timeline.

Reported: "Clicking on an article link in a Social post. The article link should open in a new window
otherwise you sometimes get back navigation issues or new social posts loading over it."

The cause was that the article reader painted into whatever window held the shared feed -- the
Social window -- and pushed no history, so the next timeline redraw could land on top of it, Back
skipped past it, and its own button always went to the Articles list. Drives the SHIPPED bundle with
a real signed post (`nostr:naddr1…`) and a real signed NIP-23 article on the fixture relay:

  * desktop: clicking the article card opens a NEW window holding the article, Social's window keeps
    its timeline, and the article's Back closes just that window;
  * phone layout (no windows): the article gets its own address (its naddr) and Back returns to the
    timeline it was opened from -- not to the Articles list.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r'''(()=>{
  const author=new Uint8Array(32).fill(7), me=new Uint8Array(32).fill(1);
  const now=Math.floor(Date.now()/1000);
  const art=NostrTools.finalizeEvent({kind:30023,created_at:now-60,content:'# Body heading\n\nThe article body.',
    tags:[['d','field-notes'],['title','Field Notes From The Desk'],['summary','A test article']]},author);
  const naddr=NostrTools.nip19.naddrEncode({kind:30023,pubkey:art.pubkey,identifier:'field-notes'});
  const post=NostrTools.finalizeEvent({kind:1,created_at:now-5,content:'Read this: nostr:'+naddr,tags:[]},me);
  window.__events=[post,art]; window.__art={id:art.id,naddr,post:post.id};
  return true;})()'''

CARD = "!!document.querySelector('.note .naddrlink')"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_on_the_desktop_an_article_opens_in_a_window_of_its_own():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js(SEED)
        await b.js("__PC.switchView('global');true")
        await b.until(CARD)
        got['before'] = await b.js("document.querySelectorAll('.osw').length")
        got['social'] = await b.js("(()=>{const w=document.querySelector('.note .naddrlink').closest('.osw');"
                                   "w.dataset.testSocial='1';return !!w})()")
        await b.js("document.querySelector('.note .naddrlink').click();true")
        await b.until("[...document.querySelectorAll('.osw')].some(w=>w.querySelector('.article-view'))")
        await asyncio.sleep(.4)
        got['after'] = await b.js(r'''(()=>{const art=[...document.querySelectorAll('.osw')].find(w=>w.querySelector('.article-view'));
          const soc=document.querySelector('.osw[data-test-social]');
          return {windows:document.querySelectorAll('.osw').length,
            sameWindow: art===soc,
            title:(art.querySelector('.av-title')||{}).textContent,
            winTitle:(art.querySelector('.osw-title, .osw-bar')||{}).textContent||'',
            socialStillTimeline: !!soc && !soc.querySelector('.article-view'),
            back:!!art.querySelector('#art-back .pc-nav-label')}})()''')
        await b.js("[...document.querySelectorAll('.osw')].find(w=>w.querySelector('.article-view')).querySelector('#art-back').click();true")
        await asyncio.sleep(.8)
        got['closed'] = await b.js("({windows:document.querySelectorAll('.osw').length,"
                                   "article:!!document.querySelector('.osw .article-view'),"
                                   "social:!!document.querySelector('.osw[data-test-social]')})")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    a = got['after']
    assert got['social'], 'the post was not in a desktop window'
    assert not a['sameWindow'], 'the article painted over the Social window: %r' % got
    assert a['windows'] == got['before'] + 1, got
    assert a['title'] == 'Field Notes From The Desk', a
    assert 'Article' in a['winTitle'], a
    assert a['socialStillTimeline'], 'the Social window no longer shows its timeline: %r' % a
    assert a['back'], 'the article has no labelled Back in its window'
    assert got['closed'] == {'windows': got['before'], 'article': False, 'social': True}, got


PHONE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_without_windows_an_article_has_an_address_and_back_returns_to_the_timeline():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js(SEED)
        await b.js("__PC.switchView('global');true")
        await b.until(CARD)
        got['inWindow'] = await b.js("!!document.querySelector('.osw .note .naddrlink')")
        await b.js("document.querySelector('.note .naddrlink').click();true")
        await b.until("!!document.querySelector('#feed .article-view')")
        got['path'] = await b.js("location.pathname")
        got['naddr'] = await b.js("__art.naddr")
        await b.js("document.getElementById('art-back').click();true")
        await b.until("!document.querySelector('#feed .article-view')")
        await asyncio.sleep(.6)
        got['back'] = await b.js("({view:__PC.VIEW, card:!!document.querySelector('#feed .note .naddrlink'),"
                                 "articlesList:!!document.getElementById('art-list')})")

    asyncio.run(desktop.with_browser('online', '', check, PHONE))
    assert not got['inWindow'], 'the phone run is in desktop mode'
    assert got['path'].endswith('/' + got['naddr']), 'the article pushed no address of its own: %r' % got
    assert not got['back']['articlesList'], 'Back went to the Articles list instead of where the reader was: %r' % got
    assert got['back']['card'], 'Back did not return to the timeline holding the post: %r' % got


# PosterChanOS: every app is its own window (`?pcwin=`), PCOS is off inside it, and only the desktop
# (a live same-origin `window.opener`) opens windows -- so what is recorded is the request reaching it.
OS_DESKTOP = ("window.__asked=[];window.__saved=[];Object.defineProperty(window,'opener',{configurable:true,value:{closed:false,"
              "__PC:{openThread:id=>__asked.push(id)},Store:{saveEvent:e=>__saved.push(e.kind)},"
              "PCOSWin:{enabled:()=>true,routable:()=>true,open:()=>({})}}});true")
OS_SEED = r'''(()=>{
  const author=new Uint8Array(32).fill(7);
  const art=NostrTools.finalizeEvent({kind:30023,created_at:Math.floor(Date.now()/1000)-60,content:'# Body\n\nThe article body.',
    tags:[['d','agent-zero-handbook'],['title','agent_zero Handbook'],['summary','A test article']]},author);
  window.__events=[art]; window.__art={id:art.id};
  return true;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_on_posterchanos_an_article_in_the_social_window_asks_the_desktop_for_its_own_window():
    """'clicking on articles on the global timeline is not opening in new window on desktop/OS' --
    measured on the real shell: the Social window painted the article over its own timeline."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin') && !!window.__PC")
        await b.js(OS_DESKTOP)
        await b.js(OS_SEED)
        await b.js("__PC.switchView('global');true")
        await b.until("!!document.querySelector('#feed .article-card')")
        await b.js("document.querySelector('#feed .article-card .art-title').click();true")
        await asyncio.sleep(.8)
        got['asked'] = await b.js("__asked")
        got['saved'] = await b.js("__saved")
        got['id'] = await b.js("__art.id")
        got['social'] = await b.js("({view:__PC.VIEW, article:!!document.querySelector('#feed .article-view'),"
                                   "card:!!document.querySelector('#feed .article-card')})")

    asyncio.run(desktop.with_browser('online', '?pcwin=global', check, ''))
    assert got['asked'] == [got['id']], 'the desktop was not asked to open the article: %r' % got
    assert 30023 in got['saved'], 'the desktop was not handed the article (its window would say "Post"): %r' % got
    assert got['social'] == {'view': 'global', 'article': False, 'card': True}, \
        'the article painted over the Social window: %r' % got
