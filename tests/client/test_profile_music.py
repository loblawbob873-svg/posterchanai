"""Real profile-music lifecycle in Chromium, including the narrow-phone editor."""
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")
TRACK = "https://cdn.example.test/audio/night-drive.mp3"


def extract(name):
    pos = APP.index("function " + name + "(")
    brace = APP.index("{", pos)
    depth, quote, escaped, line_comment, block_comment = 0, None, False, False, False
    for i in range(brace, len(APP)):
        c, nxt = APP[i], APP[i:i + 2]
        if line_comment:
            if c == "\n": line_comment = False
            continue
        if block_comment:
            if nxt == "*/": block_comment = False
            continue
        if quote:
            if escaped: escaped = False
            elif c == "\\": escaped = True
            elif c == quote: quote = None
            continue
        if nxt == "//": line_comment = True; continue
        if nxt == "/*": block_comment = True; continue
        if c in "'\"`": quote = c
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: return APP[pos:i + 1]
    raise AssertionError(name)


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
@pytest.mark.parametrize("width", [1280, 360])
def test_own_profile_music_edit_save_reopen_and_play_lifecycle(width):
    edit_at = APP.index("function editProfile(")
    edit = APP[edit_at:APP.index("\n  // Show the relays", edit_at)]
    functions = "\n".join((extract("_profileMusicFields"), extract("_profileMusicHtml"), edit))
    script = f'''
    const ME={{pubkey:'a'.repeat(64)}},LOGO='',ClientSettings={{get:()=>false,set(){{}}}};
    let profile={{name:'Alice',about:'hello'}},published=null,toasts=[];
    const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
    const xmrOf=()=>'',bchDirect=()=>'',isXmrAddr=()=>false,isBchAddr=()=>false;
    const toast=s=>toasts.push(s),uploadBlob=async()=>'',renderMe=()=>{{}};
    const Store={{saveProfile(e){{profile=JSON.parse(e.content)}}}};
    async function publish(kind,content){{published={{kind,content}};return {{ok:true}}}}
    function closeModal(){{const n=document.querySelector('.modal-bg');if(n)n.remove()}}
    function modal(html,mount){{closeModal();const bg=document.createElement('div');bg.className='modal-bg';bg.innerHTML='<div class="modal glass">'+html+'</div>';document.body.appendChild(bg);mount(bg.firstElementChild)}}
    function renderProfileView(){{const page=document.querySelector('#page');page.innerHTML='<div class="prof"><button id="edit-prof">Edit</button><div id="prof-music">'+_profileMusicHtml(profile)+'</div></div>';page.querySelector('#edit-prof').onclick=()=>editProfile(profile)}}
    {functions}
    (async()=>{{
      renderProfileView();document.querySelector('#edit-prof').click();
      const add=document.querySelector('#pf-music-add'),up=document.querySelector('#pf-music-up');
      const ar=add.getBoundingClientRect(),ur=up.getBoundingClientRect(),mb=document.querySelector('.modal').getBoundingClientRect();
      const initiallyVisible=getComputedStyle(add).display!=='none'&&getComputedStyle(up).display!=='none'&&ar.top>=0&&ar.bottom<=innerHeight&&ur.right<=innerWidth;
      add.click();const row=document.querySelector('.pf-music-row');row.querySelector('.pf-music-title').value='Night Drive';row.querySelector('.pf-music-url').value={json.dumps(TRACK)};
      document.querySelector('#pf-save').click();await new Promise(r=>setTimeout(r,30));
      const audio=document.querySelector('#prof-music audio'),playable=!!audio&&audio.controls&&audio.preload==='none'&&audio.src==={json.dumps(TRACK)};
      document.querySelector('#edit-prof').click();const reopened=document.querySelector('.pf-music-row');
      const persisted=!!reopened&&reopened.querySelector('.pf-music-title').value==='Night Drive'&&reopened.querySelector('.pf-music-url').value==={json.dumps(TRACK)};
      out.textContent=JSON.stringify({{initiallyVisible,persisted,playable,kind:published&&published.kind,fields:published&&JSON.parse(published.content).fields,over:document.documentElement.scrollWidth-innerWidth,modalLeft:mb.left,modalRight:mb.right,toasts}});
    }})().catch(e=>{{out.textContent=JSON.stringify({{error:String(e),stack:e.stack}})}});
    '''
    # Headless Chrome clamps OS windows below 500px. Constrain the page/backdrop to the requested
    # CSS viewport; 360 also activates the shipped <=600px rules in that 500px engine viewport.
    html = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{{margin:0;width:{width}px;max-width:{width}px;height:100%}}{CSS}.modal-bg{{width:{width}px}}.modal-bg>.modal{{width:min(720px,calc(100% - 24px))}}</style><main id="page"></main><pre id="out"></pre><script>{script}</script>'''
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "profile-music.html"; page.write_text(html)
        done = subprocess.run([CHROME,"--headless=new","--no-sandbox","--disable-gpu",f"--window-size={width},800","--force-device-scale-factor=1","--virtual-time-budget=1000","--dump-dom",page.as_uri()],text=True,capture_output=True,timeout=30)
    assert done.returncode == 0, done.stderr[-1200:]
    match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S); assert match
    assert match.group(1), done.stderr[-3000:] + done.stdout[-3000:]
    got = json.loads(unescape(match.group(1))); assert "error" not in got, got
    assert got["initiallyVisible"] is True
    assert got["persisted"] is True and got["playable"] is True
    assert got["kind"] == 0 and got["fields"] == [["🎶Night Drive", TRACK]]
    assert got["modalLeft"] >= 0 and got["modalRight"] <= width + 1
    assert "profile saved" in got["toasts"]


def test_music_editor_is_before_long_profile_fields_for_phone_discoverability():
    body = APP[APP.index("function editProfile"):APP.index("async function showRelays")]
    assert body.index('class="fld pf-music-editor"') < body.index('id="pf-nip05"')
    assert body.index('class="fld pf-music-editor"') < body.index('id="pf-about"')
    assert body.count('id="pf-music-list"') == 1
