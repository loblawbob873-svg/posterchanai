"""A link to one of this instance's own articles, in a Social post, opens IN the app.

Reported: "Social -> Clicking on a article link in a Social post. The article link should open in a
new window otherwise you sometimes get back navigation issues or new social posts loading over it."
Such a post carries `https://<instance>/naddr1…`, which linkify renders as a plain target=_blank
link: in the APK that NAVIGATES the one WebView (the client reloads onto the URL, Back loses the
app's history, and boot routing paints the article over the feed) and on the desktop it went to the
external browser. Now the click is taken by the app:

  * on PosterChanOS the article opens in ITS OWN post window, and the Social window is untouched;
  * in the classic client it opens in the reader with a history entry, and Back returns;
  * a link to anything else on the same host is NOT intercepted.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

SETUP = r"""(async()=>{
  const T=NostrTools, sk=T.generateSecretKey(), pk=T.getPublicKey(sk);
  const art=T.finalizeEvent({kind:30023, created_at:Math.floor(Date.now()/1000), content:'# Hello\n\nThe body of the article.',
    tags:[['d','my-article'],['title','A Test Article']]}, sk);
  Store.saveEvent(art);
  const _q=Relay.query.bind(Relay);
  Relay.query=async(f)=>{ if((f||[]).some(x=>(x.kinds||[]).includes(30023))) return [art]; return []; };
  const naddr=T.nip19.naddrEncode({kind:30023,pubkey:pk,identifier:'my-article'});
  const html=__PC.linkify('read this '+location.origin+'/'+naddr+' and '+location.origin+'/static/other.html');
  const host=document.createElement('article'); host.className='note'; host.id='fx-note';
  host.innerHTML='<div class="body"><div class="txt">'+html+'</div></div>';
  (document.querySelector('#feed')||document.body).prepend(host);
  window.__art=art; return naddr;
})()"""

CLICK = r"""((sel)=>{ const a=[...document.querySelectorAll('#fx-note a[href]')].find(x=>x.href.includes(sel));
  if(!a) return {found:false};
  const ev=new MouseEvent('click',{bubbles:true,cancelable:true,view:window}); a.dispatchEvent(ev);
  return {found:true, prevented:ev.defaultPrevented, href:a.href}; })"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_posterchanos_an_article_link_opens_its_own_window():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js(SETUP)
        other = await b.js(CLICK + "('/static/other.html')")
        assert other["found"] and not other["prevented"], "an ordinary link on this host must not be taken"
        got = await b.js(CLICK + "('naddr1')")
        assert got["found"], "linkify did not render the article link as a link"
        assert got["prevented"], "the article link still left the app"
        await b.until("[...document.querySelectorAll('.osw .av-title')].some(x=>x.textContent==='A Test Article')")

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_in_the_classic_client_an_article_link_opens_the_reader_and_back_returns():
    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await asyncio.sleep(.5)
        await b.js("__PC.switchView('global')")
        await asyncio.sleep(.5)
        await b.js(SETUP)
        got = await b.js(CLICK + "('naddr1')")
        assert got["found"] and got["prevented"], got
        await b.until("!!document.querySelector('#feed .article-view .av-title')")
        assert await b.js("document.querySelector('#feed .av-title').textContent") == "A Test Article"
        assert "naddr1" in await b.js("location.pathname"), "the reader must have its own history entry"
        await b.js("history.back()")
        await b.until("!document.querySelector('#feed .article-view')")

    asyncio.run(desktop.with_browser("online", "", check))
