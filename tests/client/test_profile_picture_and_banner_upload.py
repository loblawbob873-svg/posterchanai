"""Edit Profile: the picture AND the banner are uploaded from the device -- no URL needed.

Asked for: "Make profile editing easier for users by allowing them to upload the banner and picture without
entering a url." The sheet had two URL boxes and one "Upload pic" button at the bottom; a banner could only be a
link to an image hosted somewhere else. The SHIPPED editProfile runs in Chrome; uploadBlob is held open until the
test releases it, so the in-flight state is observable:

  * both images preview at once, before the upload answers, and the status says it is uploading;
  * Save is held while an upload is in flight (publishing then would save the OLD picture over the new one);
  * once both land, Save publishes a kind 0 carrying both uploaded URLs;
  * a failed upload says so and leaves the field as it was.
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

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _run(fail_banner=False):
    edit_at = APP.index("function editProfile(")
    if APP[:edit_at].endswith("async "):
        edit_at -= len("async ")
    edit = APP[edit_at:APP.index("\n  // Show the relays", edit_at)]
    functions = "\n".join((extract("_profileMusicFields"), extract("_profileMusicHtml"),
                           extract("_bindPaymentTargetEditor"), extract("_kind0Tags"), edit))
    script = f'''
    const ME={{pubkey:'a'.repeat(64)}},LOGO='',ClientSettings={{get:()=>false,set(){{}}}};
    const painted={{name:'Alice',picture:'https://old.example/pic.png',banner:''}};
    const Store={{saveProfile(){{}},profile(){{return null}},profileEmojis(){{return null}}}};
    window.Relay={{ready:async()=>{{}},query:async()=>[]}};
    const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    const niceNip05=()=>null;
    const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
    const xmrOf=()=>'',bchDirect=()=>'',isXmrAddr=()=>false,isBchAddr=()=>false;
    let published=null; const toasts=[], held={{}};
    const toast=s=>toasts.push(s),renderMe=()=>{{}},renderProfileView=()=>{{}};
    // Blossom, held open per file name until the test lets it answer.
    const uploadBlob=f=>new Promise((ok,no)=>{{held[f.name]={{ok:()=>ok('https://blossom.example/'+f.name),no:()=>no(new Error('refused'))}};}});
    async function publish(kind,content,tags){{published={{kind,content:JSON.parse(content)}};return {{ok:true}}}}
    function closeModal(){{const n=document.querySelector('.modal-bg');if(n)n.remove()}}
    function modal(html,mount){{closeModal();const bg=document.createElement('div');bg.className='modal-bg';bg.innerHTML='<div class="modal glass">'+html+'</div>';document.body.appendChild(bg);mount(bg.firstElementChild)}}
    {state_shim(functions)}
    {functions}
    const tick=()=>new Promise(r=>setTimeout(r,20));
    const choose=(sel,name)=>{{const bin=Uint8Array.from(atob('{PNG}'),c=>c.charCodeAt(0));
      const dt=new DataTransfer();dt.items.add(new File([bin],name,{{type:'image/png'}}));
      const i=document.querySelector(sel);i.files=dt.files;i.dispatchEvent(new Event('change'));}};
    const state=()=>({{pic:$('#pf-pic').value,banner:$('#pf-banner').value,saveDisabled:$('#pf-save').disabled,
      status:$('#pf-media-status').textContent,picPreview:($('#pf-pic-prev').hidden?'':$('#pf-pic-prev').src.slice(0,5)),
      bannerPreview:$('#pf-banner-prev').style.backgroundImage.slice(0,9),
      uploadButtons:[!!$('#pf-up'),!!$('#pf-banner-up')],urlsFolded:!$('.pf-media-urls').open}});
    (async()=>{{
      await editProfile(painted);
      const opened=state();
      choose('#pf-file','me.png'); choose('#pf-banner-file','wide.png'); await tick();
      const uploading=state();
      $('#pf-save').click(); await tick();
      const earlySave=published;
      held['me.png'].ok(); await tick();
      const one=state();
      {'held["wide.png"].no()' if fail_banner else 'held["wide.png"].ok()'}; await tick();
      const done=state();
      $('#pf-save').click(); await tick(); await tick();
      out.textContent=JSON.stringify({{opened,uploading,earlySave,one,done,published,toasts}});
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
def test_picture_and_banner_upload_preview_hold_save_and_publish():
    got = _run()
    o = got["opened"]
    assert o["uploadButtons"] == [True, True], ("no upload for the picture or the banner", o)
    assert o["urlsFolded"], ("the URL boxes are still the first thing shown", o)
    assert o["picPreview"] == "https", ("the current picture is not previewed", o)
    u = got["uploading"]
    assert u["picPreview"] == "blob:" and u["bannerPreview"] == 'url("blob', ("the chosen images were not shown at once", u)
    assert u["saveDisabled"] and "Uploading" in u["status"], ("no sign of the upload, or Save not held", u)
    assert got["earlySave"] is None, "Save published while the pictures were still uploading"
    assert got["one"]["saveDisabled"], "Save was freed while the banner was still uploading"
    d = got["done"]
    assert d["pic"] == "https://blossom.example/me.png" and d["banner"] == "https://blossom.example/wide.png", d
    assert not d["saveDisabled"], d
    meta = got["published"]["content"]
    assert meta["picture"] == "https://blossom.example/me.png", meta
    assert meta["banner"] == "https://blossom.example/wide.png", meta


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
def test_a_failed_banner_upload_says_so_and_keeps_the_old_value():
    got = _run(fail_banner=True)
    d = got["done"]
    assert "failed" in d["status"].lower(), d
    assert d["banner"] == "" and d["pic"] == "https://blossom.example/me.png", d
    assert not d["saveDisabled"], ("a failed upload left Save held for ever", d)
    assert got["published"]["content"]["banner"] == "", got["published"]
