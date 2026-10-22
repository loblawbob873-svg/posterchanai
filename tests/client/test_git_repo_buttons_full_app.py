"""A repo's buttons in Git, pressed in the SHIPPED bundle against a stubbed git host:

  * the branch picker switches the files to the picked branch;
  * a file's History opens the commits of that file; a commit opens, Copy copies its sha, Back returns;
  * Edit → Cancel leaves the file as it was; Edit → Commit sends the new content;
  * New file → a path → an empty editor, Cancel closes it;
  * Delete (confirmed) sends a delete of that file;
  * Source → .zip asks the host for the archive of the current branch;
  * ← Repos returns to the repo list.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SHA = "a1b2c3d4" * 5

GIT = r"""
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
const __gitFetch = window.fetch;
window.__gitReqs = []; window.__edits = []; window.__copied = []; window.__opened = []; window.__saved = [];
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__copied.push(String(t));}}});}catch(_){}
window.open = function(u){ window.__opened.push(String(u||'')); return null; };
const __aclick = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function(){ if(this.download||/archive/.test(this.href)){ __saved.push(this.href); return; } return __aclick.call(this); };
window.fetch = (u, o) => {
  const s = String(u);
  if (s.includes('/client/config')) return __gitFetch(u, o).then(r => r.json()).then(j => new Response(JSON.stringify(Object.assign(j,
      {git_host_base:'https://fixture.invalid/git'})), {status:200, headers:{'Content-Type':'application/json'}}));
  if (!s.includes('/client/git/')) return __gitFetch(u, o);
  __gitReqs.push(s);
  const q = new URL(s, location.href), route = q.pathname.split('/client/git/')[1];
  const json = o => Promise.resolve(new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}}));
  if (route === 'edit') { __edits.push(JSON.parse(o.body)); return json({ok:true, short:'feed123'}); }
  if (route === 'refs') return json({ok:true, default:'main', branches:[{name:'main'},{name:'dev'}], tags:[]});
  if (route === 'tree') return json({ok:true, entries:[{name:'README.md', path:'README.md', type:'blob', size:6}]});
  if (route === 'paths') return json({ok:true, paths:['README.md']});
  if (route === 'blob') return json({ok:true, text:'hello\n', size:6});
  if (route === 'log') return json({ok:true, commits:[{sha:'SHA', short:'a1b2c3d', subject:'first words', author:'me', at:1700000000}]});
  if (route === 'commit') return json({ok:true, sha:'SHA', short:'a1b2c3d', subject:'first words', author:'me', at:1700000000,
      files:[{path:'README.md', patch:'@@ -0,0 +1 @@\n+hello', additions:1, deletions:0}], file_count:1, additions:1, deletions:0, parents:[]});
  return json({ok:true});
};
""".replace("SHA", SHA)

SETUP = r"""
(() => {
  const me = __PC.me().pubkey, now = Math.floor(Date.now()/1000);
  const repo = { id: 'c'.repeat(64), kind: 30617, pubkey: me, created_at: now - 3600, sig: 'e'.repeat(128), content: '',
    tags: [['d','demo'],['name','demo'],['clone', 'https://fixture.invalid/git/' + me + '/demo.git']] };
  __PC.openRepo(repo); return 1;
})()
"""


async def open_readme(b):
    await b.until("!!document.querySelector('.fb-row[data-path=\"README.md\"]')")
    await b.js("document.querySelector('.fb-row[data-path=\"README.md\"]').click(); 1")
    await b.until("!!document.querySelector('#fv-edit')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_every_repo_button_does_its_job():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; 1")
        await b.js(SETUP)
        await b.until("!!document.querySelector('#rv-refpick') && document.querySelector('#rv-refname').textContent==='main'")

        # Branch picker, from the Files tab.
        await b.js("document.querySelector('.rv-tab[data-tab=\"files\"]').click(); 1")
        await b.until("!!document.querySelector('.fb-row[data-path=\"README.md\"]')")
        await b.js("document.querySelector('#rv-refpick').click(); 1")
        await b.until("!!document.querySelector('[data-m=\"b:dev\"]')")
        await b.js("document.querySelector('[data-m=\"b:dev\"]').click(); 1")
        await b.until("document.querySelector('#rv-refname').textContent==='dev' && __gitReqs.some(r=>r.includes('/tree?')&&r.includes('ref=dev'))")

        # History of a file -> a commit -> copy -> back.
        await open_readme(b)
        await b.js("document.querySelector('#fv-hist').click(); 1")
        await b.until("!!document.querySelector('.cm-row') && __gitReqs.some(r=>r.includes('/log?')&&r.includes('README.md'))")
        await b.js("document.querySelector('.cm-row').click(); 1")
        await b.until("!!document.querySelector('#cmv-copy')")
        await b.js("document.querySelector('#cmv-copy').click(); 1")
        await b.until(f"__copied.includes('{SHA}')")
        await b.js("document.querySelector('#cmv-back').click(); 1")
        await b.until("!document.querySelector('#cmv-back') && !!document.querySelector('.cm-row')")

        # Edit -> Cancel; Edit -> Commit.
        await b.js("document.querySelector('.rv-tab[data-tab=\"files\"]').click(); 1")
        await open_readme(b)
        await b.js("document.querySelector('#fv-edit').click(); 1")
        await b.until("!!document.querySelector('#fe-cancel')")
        await b.js("document.querySelector('#fe-cancel').click(); 1")
        await b.until("!document.querySelector('#fe-cancel') && !!document.querySelector('#fv-edit')")
        assert await b.js("__edits.length") == 0
        await b.js("document.querySelector('#fv-edit').click(); 1")
        await b.until("!!document.querySelector('#fe-save')")
        await b.js("(()=>{const t=document.querySelector('#rv-fileview textarea');t.value='hello again\\n';t.dispatchEvent(new Event('input'));"
                   "document.querySelector('#fe-save').click();})(); 1")
        await b.until("__edits.length===1")
        edit = await b.js("__edits[0]")
        assert edit["path"] == "README.md" and edit["content"] == "hello again\n" and not edit["delete"], edit
        assert edit["ref"] == "dev", edit

        # New file -> path -> empty editor -> Cancel.
        await b.until("!!document.querySelector('#fb-new')")
        await b.js("document.querySelector('#fb-new').click(); 1")
        await b.until("!!document.querySelector('.uiprompt-in')")
        await b.js("(()=>{const i=document.querySelector('.uiprompt-in');i.value='docs/new.md';"
                   "document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click();})(); 1")
        await b.until("!!document.querySelector('#fe-cancel') && document.querySelector('#rv-fileview textarea').value===''")
        await b.js("document.querySelector('#fe-cancel').click(); 1")
        await b.until("!document.querySelector('#fe-cancel')")

        # Delete (confirmed).
        await open_readme(b)
        await b.js("document.querySelector('#fv-del').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until("__edits.length===2")
        assert await b.js("__edits[1].delete") is True and await b.js("__edits[1].path") == "README.md"

        # Source -> .zip.
        await b.js("document.querySelector('#rv-dlsrc').click(); 1")
        await b.until("!!document.querySelector('[data-m=\"zip\"]')")
        await b.js("document.querySelector('[data-m=\"zip\"]').click(); 1")
        await b.until("[...__gitReqs,...__opened,...__saved].some(r=>r.includes('archive')&&r.includes('fmt=zip')&&r.includes('ref=dev'))")

        # Back to the repo list.
        await b.js("document.querySelector('#repo-back').click(); 1")
        await b.until("!document.querySelector('#repo-back') && (!!document.querySelector('#repo-new') || !!document.querySelector('#repo-create'))")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, GIT))
