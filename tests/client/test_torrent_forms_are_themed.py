"""Torrents → Add torrent and 📡 Feeds draw their fields in the app's theme.

"add torrent looks the same, URL fields make the UI look terrible. same for Torrent Feeds": every
field in both dialogs was a bare <input> — the browser's white default box on a dark modal — and the
Subscribe button sat squeezed in a grid cell beside a field. Rendered here with the SHIPPED
client.css and torrents.js (real openAdd / openFeeds, a stub modal host and /api/torrent/feeds) at a
desktop and a phone width; every text field must be themed (not a light box), fit its dialog, and
Subscribe must be on its own row.
"""
import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = next((shutil.which(c) for c in ("google-chrome-stable", "google-chrome", "chromium") if shutil.which(c)), None)

SETUP = r"""
window.__PC={toast(){},modal(h,cb){const m=document.createElement('div');m.className='modal-box';m.innerHTML=h;
  document.getElementById('host').replaceChildren(m);cb&&cb(m);},closeModal(){document.getElementById('host').replaceChildren();},
  uiConfirm:async()=>true,
  authFetch:async(u)=>new Response(JSON.stringify({enabled:true,interval_minutes:30,max_per_poll:5,
    feeds:[{id:'f1',url:'https://showrss.info/user/1.rss',title:'Shows',include:'',exclude:'',enabled:true}]}),{status:200})};
"""
MEASURE = r"""
const lum=c=>{const m=c.match(/[\d.]+/g).map(Number);const f=v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4};
  return .2126*f(m[0])+.7152*f(m[1])+.0722*f(m[2])};
const fields=[...document.querySelectorAll('#host input:not([type=checkbox])')].map(i=>{const r=i.getBoundingClientRect(),
  box=i.closest('.modal-box').getBoundingClientRect(),cs=getComputedStyle(i);
  return {id:i.id,themed:i.classList.contains('input'),bgLum:lum(cs.backgroundColor)*(parseFloat((cs.backgroundColor.match(/,\s*([\d.]+)\)$/)||[0,1])[1])),
    fits:r.left>=box.left-1&&r.right<=box.right+1,w:Math.round(r.width)}});
"""


def _run(width):
    page = f"""<!doctype html><meta name=viewport content="width=device-width"><style>{(ROOT/'static/css/client.css').read_text()}</style>
<body style="width:{width}px"><div id="host"></div><pre id="r"></pre>
<script>{SETUP}</script><script>{(ROOT/'static/js/client/torrents.js').read_text()}</script>
<script>(async()=>{{const out={{}};
 await PCTorrents.openAdd();{{{MEASURE}out.add=fields;}}
 await PCTorrents.openFeeds();await new Promise(r=>setTimeout(r,200));{{{MEASURE}out.feeds=fields;}}
 const sub=document.getElementById('tmx-nadd'),url=document.getElementById('tmx-nurl');
 out.subOwnRow=!!(sub&&url&&sub.getBoundingClientRect().top>=url.getBoundingClientRect().bottom);
 document.getElementById('r').textContent=JSON.stringify(out);}})().catch(e=>document.getElementById('r').textContent=JSON.stringify({{error:String(e)}}))</script>"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.html"; p.write_text(page)
        o = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=3000",
                            f"--window-size={width},900", "--dump-dom", p.as_uri()], capture_output=True, text=True, timeout=60).stdout
    return json.loads(html.unescape(re.search(r'<pre id="r">(.*?)</pre>', o, re.S).group(1)))


@pytest.mark.skipif(CHROME is None, reason="Chrome is required")
@pytest.mark.parametrize("width", [1280, 390])
def test_add_torrent_and_feed_fields_are_themed_and_fit(width):
    r = _run(width)
    assert "error" not in r, r
    assert r["add"] and r["feeds"], r
    for f in r["add"] + r["feeds"]:
        assert f["themed"], f"{width}px: #{f['id']} is a bare browser input"
        assert f["bgLum"] < 0.2, f"{width}px: #{f['id']} is drawn as a light box on the dark dialog ({f})"
        assert f["fits"], f"{width}px: #{f['id']} runs outside its dialog ({f})"
    assert r["subOwnRow"], "Subscribe must sit on its own row, not squeezed beside a field"
