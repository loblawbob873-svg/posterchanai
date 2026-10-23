"""Git: the two buttons no test pressed (2026-10-22: every button gets a test).

  * Create repo → the first-push quick start → '#qs-done' "Done": closes the quick start and leaves you on the repo
    list (the repo is NOT opened), with the new repo already announced (kind 30617).
  * '#repo-retry' "Try again": when no relay answered the repo-list query the list says so (never "no repos"), and
    Try again asks the relays again and shows the repos they now return.
Shipped bundle; the git host is the stub from test_git_repo_buttons_full_app, the relay socket a fixture.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_git_repo_buttons_full_app import GIT


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SENT = ("window.__publishOK=true; window.__sent=[]; const __s=WebSocket.prototype.send;"
        "WebSocket.prototype.send=function(r){try{const m=JSON.parse(r);if(m[0]==='EVENT')__sent.push(m[1]);}catch(_){}return __s.call(this,r)}; 1")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_quick_start_done_closes_it_and_stays_on_the_repo_list():
    async def check(b):
        await desktop.login(b)
        await b.js(SENT)
        await b.js("__PC.switchView('repos'); 1")
        await b.until("!!document.querySelector('#repo-create')")
        await b.js("document.querySelector('#repo-create').click(); 1")
        await b.until("!!document.querySelector('#cr-go')")
        await b.js("(()=>{document.querySelector('#cr-d').value='done-app';document.querySelector('#cr-name').value='Done App';"
                   "document.querySelector('#cr-go').click();})(); 1")
        await b.until("!!document.querySelector('#qs-done')")
        assert "done-app" in await b.js("document.querySelector('#qs-done').closest('.modal').textContent")
        await b.js("document.querySelector('#qs-done').click(); 1")
        await b.until("!document.querySelector('#qs-done') && !document.querySelector('#qs-open')")
        await asyncio.sleep(.4)
        # Done is not "Open repo": still the list, no repo view.
        assert not await b.js("!!document.querySelector('#repo-back')"), "Done opened the repo"
        assert await b.js("!!document.querySelector('#repo-create')")
        assert await b.js("__sent.some(e=>e.kind===30617&&e.tags.some(t=>t[0]==='clone'&&/done-app\\.git$/.test(t[1])))"), "the repo was not announced"
    asyncio.run(desktop.with_browser("online", "", check, GIT))


# No relay answers the repo-list query while __gitDown: an unanswered query resolves [] with complete:false.
DOWN = r"""(()=>{window.__gitDown=true;window.__gitAsks=0;const q=Relay.query.bind(Relay);
  Relay.query=(f,...r)=>{if((f||[]).some(x=>(x.kinds||[]).includes(30617))){__gitAsks++;
    if(__gitDown)return Promise.resolve(Object.assign([],{complete:false}));}return q(f,...r);};
  const sk=new Uint8Array(32).fill(9);
  window.__repo=NostrTools.finalizeEvent({kind:30617,created_at:Math.floor(Date.now()/1000)-60,content:'',
    tags:[['d','found-later'],['name','found-later'],['description','came back with the relays']]},sk);
  window.__events=[__repo];return 1;})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_try_again_asks_the_relays_again_and_shows_the_repos():
    async def check(b):
        await desktop.login(b)
        await b.js(DOWN)
        await b.js("__PC.switchView('repos'); 1")
        await b.until("!!document.querySelector('#repo-retry')")
        assert "Could not reach the relays for the repo list" in await b.js("document.querySelector('#feed').innerText")
        assert not await b.js("/No git repos found/.test(document.querySelector('#feed').innerText)")
        # The relays are back: Try again asks them again and the repo appears.
        await b.js("window.__gitDown=false; window.__asked=__gitAsks; document.querySelector('#repo-retry').click(); 1")
        await b.until("__gitAsks>__asked")
        await b.until("[...document.querySelectorAll('.repo-card')].some(c=>c.textContent.includes('found-later'))")
        assert not await b.js("!!document.querySelector('#repo-retry')")
    asyncio.run(desktop.with_browser("online", "", check, GIT))
