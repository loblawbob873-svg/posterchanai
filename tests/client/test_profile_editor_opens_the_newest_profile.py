"""Edit Profile opens the NEWEST profile, never the copy the page was painted with.

Reported from the PosterChanOS desktop: an About line with "https://poster.place" was saved (from
another device) and shown in the profile header, but Edit Profile opened without it. The page paints
from the cache first and patches its header when the relay answers; the Edit button kept the object
it was PAINTED with. A kind-0 is replaceable and Save publishes the whole form, so the next Save
would have written the stale About back over the new one.

The SHIPPED editProfile runs in Chrome with a Store that keeps the newest kind-0 by created_at (as
store.js does) and a relay that answers with the newer profile -- or cannot be reached at all.
"""
from html import unescape
import json
from pathlib import Path
import re
import subprocess
import tempfile

import pytest

from tests.client.test_profile_music import APP, CHROME, extract
from tests.client_source import state_shim

NEW_ABOUT = "Creator of PosterChan at https://poster.place, the best Nostr Experience!"


def _run(relay_mode):
    edit_at = APP.index("function editProfile(")
    if APP[:edit_at].endswith("async "):
        edit_at -= len("async ")
    edit = APP[edit_at:APP.index("\n  // Show the relays", edit_at)]
    functions = "\n".join((extract("_profileMusicFields"), extract("_profileMusicHtml"),
                           extract("_bindPaymentTargetEditor"), extract("_kind0Tags"), edit))
    script = f'''
    const ME={{pubkey:'a'.repeat(64)}},LOGO='',ClientSettings={{get:()=>false,set(){{}}}};
    const painted={{name:'Alice',about:'old about'}};
    const recs={{}};let published=null,toasts=[];
    const Store={{
      saveProfile(e){{const cur=recs[e.pubkey];if(cur&&cur.created_at>=e.created_at)return;recs[e.pubkey]={{created_at:e.created_at,meta:JSON.parse(e.content)}}}},
      profile(pk){{return recs[pk]?recs[pk].meta:null}}, profileEmojis(){{return null}} }};
    Store.saveProfile({{pubkey:ME.pubkey,created_at:100,content:JSON.stringify(painted)}});   // what the page painted from
    const NEWER={{pubkey:ME.pubkey,kind:0,created_at:200,content:JSON.stringify({{name:'Alice',about:{json.dumps(NEW_ABOUT)}}})}};
    window.Relay = {json.dumps(relay_mode)}==='down'
      ? {{ready:()=>new Promise(()=>{{}}), query:()=>new Promise(()=>{{}})}}
      : {{ready:async()=>{{}}, query:async()=>[NEWER]}};
    const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
    const xmrOf=()=>'',bchDirect=()=>'',isXmrAddr=()=>false,isBchAddr=()=>false;
    const toast=s=>toasts.push(s),uploadBlob=async()=>'',renderMe=()=>{{}},renderProfileView=()=>{{}};
    async function publish(kind,content,tags){{published={{kind,content,tags}};return {{ok:true}}}}
    function closeModal(){{const n=document.querySelector('.modal-bg');if(n)n.remove()}}
    function modal(html,mount){{closeModal();const bg=document.createElement('div');bg.className='modal-bg';bg.innerHTML='<div class="modal glass">'+html+'</div>';document.body.appendChild(bg);mount(bg.firstElementChild)}}
    {state_shim(functions)}
    {functions}
    (async()=>{{
      const t0=performance.now();
      await editProfile(painted);          // exactly what the stale Edit button passes
      const shown=document.querySelector('#pf-about').value, took=performance.now()-t0;
      document.querySelector('#pf-save').click();await new Promise(r=>setTimeout(r,30));
      out.textContent=JSON.stringify({{shown,took,saved:published&&JSON.parse(published.content).about}});
    }})().catch(e=>{{out.textContent=JSON.stringify({{error:String(e),stack:e.stack}})}});
    '''
    html = f'<!doctype html><main id="page"></main><pre id="out"></pre><script>{script}</script>'
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "edit.html"
        page.write_text(html)
        done = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                               "--virtual-time-budget=15000", "--dump-dom", page.as_uri()],
                              text=True, capture_output=True, timeout=60)
    m = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    assert m and m.group(1), done.stderr[-2000:]
    got = json.loads(unescape(m.group(1)))
    assert "error" not in got, got
    return got


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
def test_the_editor_opens_and_saves_the_newest_profile_not_the_painted_one():
    got = _run("up")
    assert got["shown"] == NEW_ABOUT, "Edit Profile opened the stale painted About: %r" % got
    assert got["saved"] == NEW_ABOUT, "Save wrote the stale About back over the newer one: %r" % got


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
def test_an_unreachable_relay_still_opens_the_editor_from_the_freshest_cached_copy():
    got = _run("down")
    assert got["shown"] == "old about", got           # the only copy there is
    assert got["took"] < 10000, "a dead relay held the editor closed: %r" % got
