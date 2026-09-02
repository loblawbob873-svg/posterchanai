"""EVERY DESKTOP WIDGET MUST SHOW SOMETHING THE MOMENT IT IS MOUNTED.

Reported as "really double check the widgets" / "the widgets are in a shit shape now". Fourteen
widgets shipped with two tests between them, and a widget that mounts to an empty tile fails in the
quietest way there is: no throw, no log, just a blank rectangle on the desktop.

Rendered all fourteen side by side and looked at them. Twelve were fine — a header and "reading…",
or "loading…", or a real control. Two showed nothing at all:

  * `clock` drew empty elements and waited for its first refresh tick, so a freshly added or
    redrawn clock was a blank tile with a lone ＋ in the corner. Its refresh needs NO network — a
    Date and `_clockFace` — so there was never a reason to wait: a clock that cannot say the time
    immediately is not a clock.
  * `stats` mounted `<div class="wgt-st"></div>` and nothing else, and its refresh deliberately
    draws nothing when the stats have not been fetched (five authoritative-looking zeroes would be
    worse). Between them, a completely empty tile.

This mounts each widget in a REAL browser DOM, because the stub used elsewhere returns throwaway
nodes from `querySelector` — a widget that fills itself through one, as the clock does, cannot be
measured there at all. "Shows something" counts placeholder text too: `weather` is an input and
`note` is a textarea, and their placeholders are not innerText.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome"))

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")


def mount_all(os_js: str = "") -> dict:
    js = os_js or OS_JS
    page = """<!doctype html><meta charset="utf-8"><style>html,body{margin:0}%s</style>
<div id="host"></div><pre id="out"></pre>
<script>window.__PC={ $:(s,r)=>(r||document).querySelector(s), $$:(s,r)=>[...(r||document).querySelectorAll(s)],
  enc:String, toast(){}, VIEW:'home', isView:()=>false, viewer:()=>({}),
  communityStats:()=>null, relayQuery:async()=>[] };
window.ClientSettings={get:(k,d)=>d,set(){}};</script>
<script>%s</script>
<script>requestAnimationFrame(()=>{
  const reg=(window.PCOS&&window.PCOS.__widgets)?window.PCOS.__widgets():null;
  if(!reg){ out.textContent=JSON.stringify({error:'no registry'}); return; }
  const res={};
  for(const k of Object.keys(reg)){
    const d=document.createElement('div'); d.className='os-wgt-body';
    d.style.cssText='width:440px;height:210px'; host.appendChild(d);
    let threw=null;
    try{ reg[k].mount(d, {key:k,size:'m',cfg:{}}); }catch(e){ threw=String(e&&e.message||e).slice(0,90); }
    /* Placeholder text counts: `weather` is an input and `note` a textarea, and a placeholder is
       not innerText. A control the user can type into is not a blank tile. */
    const ph=[...d.querySelectorAll('[placeholder]')].map(n=>n.getAttribute('placeholder')||'').join(' ');
    res[k]={threw, shows:((d.innerText||'')+' '+ph).replace(/\\s+/g,' ').trim().slice(0,70)};
  }
  out.textContent=JSON.stringify(res);
});</script>""" % (CSS, js)
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "w.html"
        html.write_text(page, encoding="utf-8")
        done = subprocess.run(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=900,700",
             "--virtual-time-budget=2500", "--dump-dom", html.as_uri()],
            capture_output=True, text=True, timeout=180)
        assert done.returncode == 0, done.stderr[-1000:]
        found = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert found, done.stdout[-1000:]
        return json.loads(unescape(found.group(1)))


@pytest.fixture(scope="module")
def widgets():
    got = mount_all()
    assert "error" not in got, got
    return got


def test_the_registry_is_reachable_and_complete(widgets):
    """A check that can only see two widgets passes vacuously about the other twelve."""
    assert len(widgets) >= 14, f"only {len(widgets)} widgets found: {sorted(widgets)}"
    for expected in ("clock", "stats", "cpu", "monero", "weather", "note"):
        assert expected in widgets


def test_no_widget_throws_on_mount(widgets):
    broken = {k: v["threw"] for k, v in widgets.items() if v["threw"]}
    assert not broken, f"widgets threw on mount: {broken}"


def test_every_widget_shows_something(widgets):
    """THE REPORT. An empty tile is the quietest failure on the desktop."""
    blank = sorted(k for k, v in widgets.items() if not v["shows"])
    assert not blank, f"these widgets mount to a completely blank tile: {blank}"


def test_the_clock_says_the_time_immediately(widgets):
    """It needs no network — a Date and a formatter. Waiting for a tick made it a blank tile with
    a lone ＋ in the corner."""
    assert re.search(r"\d{1,2}:\d{2}", widgets["clock"]["shows"]), (
        f"the clock shows no time on mount: {widgets['clock']['shows']!r}")


def test_the_stats_widget_says_it_is_reading(widgets):
    """Its refresh draws nothing until the stats are fetched, and that is deliberate — so mount has
    to say something, or the tile is empty."""
    assert widgets["stats"]["shows"], "the stats widget is still a blank tile"


def test_these_checks_can_fail():
    """MUTATION: put both widgets back the way they were and watch the blank tiles reappear."""
    broken = OS_JS.replace("        try{ WIDGETS.clock.refresh(el, w || {}); }catch(_){ }\n", "", 1)
    broken = broken.replace(
        """mount(el){ el.innerHTML = '<div class="wgt-st"><div class="wgt-dim">reading\\u2026</div></div>'; }""",
        """mount(el){ el.innerHTML = '<div class="wgt-st"></div>'; }""", 1)
    assert broken != OS_JS, "could not rebuild the pre-fix widgets — re-read this test"
    got = mount_all(broken)
    assert "error" not in got, got
    blank = {k for k, v in got.items() if not v["shows"]}
    assert "stats" in blank, "the stats widget no longer mounts blank, so this check proves nothing"
    assert not re.search(r"\d{1,2}:\d{2}", got["clock"]["shows"]), (
        "the clock still shows a time without the mount-time refresh")
