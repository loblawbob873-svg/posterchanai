"""The desktop Monero and Now-playing widgets' buttons are readable icons on EVERY theme.

Reported 2026-10-08: "the monero buttons are hard to read, maybe make white. looks ugly. Music player widget
buttons look ugly". Measured in Chromium before the fix: the music transport was the characters ⏮ ▶ ⏭ ☰ (colour
emoji tiles on one machine, empty boxes on another), the wallet's refresh was a ↻ that drew as an empty box, and the
wallet's labels were the theme's grey-ish --text at 10px. Plain white would have been the obvious fix and is
invisible on the five LIGHT themes (white on a pink/white/beige panel), so this renders the SHIPPED widget code with
the SHIPPED stylesheet under every theme and measures: icon buttons carry a sprite icon and no text, the dark themes
draw the wallet labels white, and every button clears WCAG contrast (4.5:1 text, 3:1 icon) against what is
actually behind it.
"""
import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OSJS = (ROOT / "static/js/client/os.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
SPRITE = (ROOT / "static/js/client/sprite.js").read_text()
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")
THEMES = re.search(r'CLIENT_THEMES = \(([^)]*)\)', (ROOT / "app/schemas.py").read_text()).group(1)
THEMES = [t.strip().strip('"') for t in THEMES.split(",") if t.strip()]
LIGHT = {"cherryblossom", "professional", "win98", "winxp", "animegirl"}


def _block(src, start):
    """The brace-matched object literal that starts at the first `{` after `start` (strings and comments skipped)."""
    i = src.index("{", start)
    depth, q, esc = 0, "", False
    j = i
    while j < len(src):
        c, n = src[j], src[j + 1] if j + 1 < len(src) else ""
        if q:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == q: q = ""
        elif c == "/" and n == "/": j = src.index("\n", j); continue
        elif c == "/" and n == "*": j = src.index("*/", j + 2) + 2; continue
        elif c in "'\"`": q = c
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: return src[i:j + 1]
        j += 1
    raise AssertionError("unbalanced")


def _monero_code():
    start = OSJS.index("function _xmrDisplay(")
    return OSJS[start:OSJS.index("\n  /* WMO weather codes", start)]


def _music_code():
    return "const MUSIC=" + _block(OSJS, OSJS.index("    music: {"))


PAGE_JS = r"""
const $=(s,r=document)=>r.querySelector(s),_wgtFeeds=new Map(),_wgtFeed=async(k,t,f)=>f(),openApp=()=>{},_wgtRefreshOne=()=>{};
const _mmss=s=>{s=Math.floor(s||0);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
const player={now:()=>({title:'Night Drive',next:'City Lights',playing:true,t:72,d:185,pos:1,total:9})};
const PC=()=>({music:()=>player,authFetch:async p=>new Response(JSON.stringify(p.endsWith('/status')?{network:'mainnet',mainnet:true}:
  {wallet_rpc_reachable:true,daemon_connected:true,network:'mainnet',balance:'0.4821',unlocked_balance:'0.4821'}),{status:200})}),_api=p=>p;
MONERO_CODE
MUSIC_CODE
function rgb(s){const m=String(s).match(/[\d.]+/g)||[0,0,0,0];return {r:+m[0],g:+m[1],b:+m[2],a:m.length>3?+m[3]:1};}
function over(top,under){const a=top.a;return {r:top.r*a+under.r*(1-a),g:top.g*a+under.g*(1-a),b:top.b*a+under.b*(1-a),a:1};}
function lum(c){const f=v=>{v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)};return .2126*f(c.r)+.7152*f(c.g)+.0722*f(c.b);}
function ratio(a,b){const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
(async()=>{
  const xmrHost=$('#xmr'),musHost=$('#mus');
  const X=_moneroWidget();X.mount(xmrHost.firstElementChild);await X.refresh(xmrHost.firstElementChild);
  MUSIC.mount(musHost.firstElementChild);MUSIC.refresh(musHost.firstElementChild);
  const rows=[];
  for(const host of [xmrHost,musHost]){
    const panel=rgb(getComputedStyle(host).backgroundColor);
    for(const b of host.querySelectorAll('button')){
      const cs=getComputedStyle(b),fg=rgb(cs.color),bg=over(rgb(cs.backgroundColor),panel);
      rows.push({w:host.id,name:b.getAttribute('aria-label')||b.textContent.trim(),text:b.textContent.trim(),
        icon:!!b.querySelector('svg use'),color:cs.color,contrast:Math.round(ratio(fg,bg)*100)/100});
    }
  }
  out.textContent=JSON.stringify(rows);
})().catch(e=>out.textContent=JSON.stringify({fatal:String(e),stack:e.stack}));
"""


def _measure(theme):
    js = PAGE_JS.replace("MONERO_CODE", _monero_code()).replace("MUSIC_CODE", _music_code())
    attr = "" if theme == "cyberpunk" else f' data-theme="{theme}"'
    html = (f'<!doctype html><html{attr}><head><meta charset="utf-8"><style>{CSS}'
            '.os-wgt{position:relative;width:290px;height:190px}</style></head><body>'
            '<section class="os-wgt" id="xmr" data-type="monero" data-size="m"><div class="os-wgt-body"></div></section>'
            '<section class="os-wgt" id="mus" data-type="music" data-size="m"><div class="os-wgt-body"></div></section>'
            f'<pre id="out"></pre><script>{SPRITE}</script><script>{js}</script></body></html>')
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "w.html"
        page.write_text(html)
        done = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=700,500",
                               "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
                              text=True, capture_output=True, timeout=60)
    m = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    assert m and m.group(1), done.stderr[-800:]
    got = json.loads(unescape(m.group(1)))
    assert "fatal" not in got, got
    return got


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
@pytest.mark.parametrize("theme", THEMES)
def test_widget_buttons_are_icons_and_readable(theme):
    rows = _measure(theme)
    xmr = [r for r in rows if r["w"] == "xmr"]
    mus = [r for r in rows if r["w"] == "mus"]
    assert len(xmr) == 4 and len(mus) == 5, rows
    # Every music control and the wallet's refresh are drawn icons -- never a character that a font may not have.
    for r in mus + [x for x in xmr if x["name"] == "Refresh wallet"]:
        assert r["icon"] and r["text"] == "", (theme, r)
    # The wallet labels are WHITE on the dark themes (what was asked), and never white on a light one.
    for r in xmr:
        if r["name"] == "Refresh wallet":
            continue
        if theme in LIGHT:
            assert r["color"] != "rgb(255, 255, 255)", (theme, r)
        else:
            assert r["color"] == "rgb(255, 255, 255)", (theme, r)
    # WCAG: 4.5:1 for a text label, 3:1 for an icon-only control (1.4.11 non-text contrast).
    for r in rows:
        assert r["contrast"] >= (3 if r["text"] == "" else 4.5), (theme, r)
