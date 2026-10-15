"""Git -> a file -> Download and Raw work in the DESKTOP APP, where the page is not served by the instance.

Reported 2026-10-15 on PosterChanOS: "git -> raw button on files does nothing", "downloading
badpost-facepalm.png from Git ... preview says that file is not an image this browser can decode". The
desktop loads the client from `app://posterchan`; a public repo's Raw and Download were plain links to
`/client/git/raw|download?...`, which name a path inside the app's OWN bundle -- answered with the client
shell, so Download saved an HTML page under the PNG's name and Raw opened a window of the timeline. The
bundled client (window.__PC_API_BASE__ set, the exact desktop build) must fetch the bytes from the instance
and save THOSE, and Raw must open the instance's absolute URL.
"""
import asyncio
import base64
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')

GIT = r"""
const __gitFetch = window.fetch;
window.__gitReqs = [];
window.fetch = (u, o) => {
  const s = String(u);
  if (s.includes('/client/config')) return __gitFetch(u, o).then(r => r.json()).then(j => new Response(JSON.stringify(Object.assign(j,
      {git_host_base:'https://fixture.invalid/git'})), {status:200, headers:{'Content-Type':'application/json'}}));
  if (s.includes('/client/git/')) {
    __gitReqs.push(s);
    const path = s.split('?')[0].split('/client/git/')[1];
    const json = o => new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}});
    if (path === 'download' || path === 'raw') {
      const b = Uint8Array.from(atob('PNGB64'), c => c.charCodeAt(0));
      return Promise.resolve(new Response(b, {status:200, headers:{'Content-Type':'image/png'}}));
    }
    if (path === 'blob') return Promise.resolve(json({ok:true, binary:true, size:PNGLEN}));
    if (path === 'tree') return Promise.resolve(json({ok:true, entries:[{name:'face.png', path:'face.png', type:'blob', size:PNGLEN}]}));
    return Promise.resolve(json({ok:true}));
  }
  return __gitFetch(u, o);
};
window.__saved = []; window.__opened = [];
const __click = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function(){
  if (this.download) { const href = this.href, name = this.download;
    window.__saved.push(new Promise(res => { const x = new XMLHttpRequest(); x.open('GET', href); x.responseType = 'arraybuffer';
      x.onload = () => res({name, href, status:x.status, bytes:Array.from(new Uint8Array(x.response || new ArrayBuffer(0)))});
      x.onerror = () => res({name, href, error:true, bytes:[]}); x.send(); }));
    return; }
  return __click.call(this);
};
const __open = window.open;
window.open = function(u){ window.__opened.push(String(u||'')); return null; };
""".replace('PNGB64', base64.b64encode(PNG).decode()).replace('PNGLEN', str(len(PNG)))

SETUP = r"""
(() => {
  const me = __PC.me().pubkey, now = Math.floor(Date.now()/1000);
  const repo = { id: 'c'.repeat(64), kind: 30617, pubkey: me, created_at: now - 3600, sig: 'e'.repeat(128), content: '',
    tags: [['d','demo'],['name','demo'],['clone', 'https://fixture.invalid/git/' + me + '/demo.git']] };
  __PC.openRepo(repo);
})()
"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_download_saves_the_files_bytes_and_raw_opens_the_instance_url():
    got = {}

    async def check(b):
        await desktop.login(b)
        assert await b.js("typeof window.__PC_API_BASE__ !== 'undefined'"), 'not the bundled desktop build'
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        # Open the file the way a reader does: its row in the Files tab.
        await b.until("!!document.querySelector('.rv-tab[data-tab=\"files\"]')")
        await b.js("document.querySelector('.rv-tab[data-tab=\"files\"]').click()")
        await b.until("!![...document.querySelectorAll('[data-path]')].find(x=>x.dataset.path==='face.png')")
        await b.js("[...document.querySelectorAll('[data-path]')].find(x=>x.dataset.path==='face.png').click()")
        await b.until("!!document.querySelector('.fb-fvname')")
        got['links'] = await b.js("[...document.querySelectorAll('.fb-fvacts a[href]')].map(a=>a.getAttribute('href'))")
        await b.js("document.querySelector('#fv-dl')?.click(); document.querySelector('#fv-raw')?.click()")
        await b.until("window.__saved.length>0 || window.__opened.length>0")
        await asyncio.sleep(.5)
        got['saved'] = await b.js("Promise.all(window.__saved)")
        got['opened'] = await b.js("window.__opened")
        got['origin'] = await b.js("__PC.serverOrigin ? __PC.serverOrigin() : ''")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check, GIT))
    assert not [h for h in got['links'] if h.startswith('/')], ('a link relative to the app bundle', got['links'])
    assert len(got['saved']) == 1 and got['saved'][0]['name'] == 'face.png', got
    assert bytes(got['saved'][0]['bytes']) == PNG, 'the saved file is not the repo file'
    assert len(got['opened']) == 1 and got['opened'][0].startswith('http') and '/client/git/raw?' in got['opened'][0], got['opened']
