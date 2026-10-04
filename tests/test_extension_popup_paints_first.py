"""The extension popup draws the logins at once, from what it last showed -- it does not wait for the background.

"need to make the browser extensions more responsive". Every row came from the background, which is an
event page / service worker that is ASLEEP when the toolbar button is pressed; it evaluates the Nostr
bundle and the vault core and reads the vault before it answers, and until then the popup was a header
and nothing. The popup now paints a session-storage snapshot of what it last drew (titles, usernames,
hosts -- never a password or a secret) and swaps in the live answer.

Shipped popup.html/popup.js in headless Chrome, the extension APIs stubbed, the background answering
only after 1.5 s (a cold wake). Each rule is a case: painted early when there is a snapshot; the safe
default (no vault tabs) without one; a snapshot from ANOTHER site never shows its matches here; and the
snapshot written carries no secret.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")

STUBS = r"""
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message)));
const ITEMS=[{id:'a',title:'GitHub',username:'me@example.com',_match:'exact',hasTotp:true,password:'LEAK'}];
const ALL=ITEMS.concat([{id:'c',title:'Bank',username:'acct',host:'bank.example.com',hasTotp:true}]);
window.__session=%(session)s; window.__writes=[];
const later=(v)=>new Promise(r=>setTimeout(()=>r(v),1500));
window.browser={
  runtime:{ sendMessage:async m=>{
    if(m.type==='state') return later({paired:true,mode:'rw',count:2,status:'ready',lastSync:0});
    if(m.type==='matches') return later({items:ITEMS});
    if(m.type==='all') return later({items:ALL});
    return {ok:true}; }, getManifest:()=>({version:'9.9.9'}) },
  tabs:{ query:async()=>[{id:1,url:%(url)r}], sendMessage:async()=>{} },
  storage:{ session:{
    get:async k=>({[k]:__session[k]}),
    set:async o=>{Object.assign(__session,o);__writes.push(JSON.parse(JSON.stringify(o)));},
    remove:async k=>{delete __session[k];} } },
};
"""

PROBE = r"""<script>
const vis=el=>!!(el&&el.getClientRects().length);
const snap=()=>({items:[...document.querySelectorAll('#list .item b')].map(b=>b.textContent),
  list:vis(document.getElementById('pane-list')), logins:vis(document.getElementById('tab-list')),
  pair:vis(document.getElementById('pane-pair')), text:(document.getElementById('list')||{}).textContent||''});
const out={};
setTimeout(()=>{out.early=snap();},300);
setTimeout(()=>{out.late=snap();out.writes=__writes;out.errors=__errors;document.title='RESULT'+JSON.stringify(out);},3600);
</script>"""

SNAP = {"pcpwSnap": {"v": 1, "paired": True, "mode": "rw", "count": 2, "status": "ready", "host": "github.com",
                     "matches": [{"id": "a", "title": "GitHub", "username": "me@example.com", "host": "",
                                  "_match": "exact", "hasTotp": True}],
                     "all": [{"id": "a", "title": "GitHub", "username": "me@example.com", "host": "github.com",
                              "hasTotp": True}]}}


def _run(session, url):
    html = (EXT / "popup.html").read_text()
    html = html.replace('<script src="popup.js"></script>',
                        "<script>" + STUBS % {"session": json.dumps(session), "url": url} + "</script>\n"
                        '<script src="popup.js"></script>\n' + PROBE)
    d = tempfile.mkdtemp()
    tmp = EXT / ".popup-paint-check.html"
    try:
        tmp.write_text(html)
        p = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=6000",
                            f"--user-data-dir={d}", "--dump-dom", "file://" + str(tmp)],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"RESULT(\{.*?\})</title>", p.stdout, re.S)
        assert m, p.stdout[-500:]
        return json.loads(m.group(1).replace("&amp;", "&").replace("&quot;", '"'))
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not CHROME, reason="Chrome required")
def test_with_a_snapshot_the_logins_are_on_screen_before_the_background_answers():
    r = _run(SNAP, "https://github.com/login")
    assert r["errors"] == [], r["errors"]
    assert r["early"]["items"] == ["GitHub"] and r["early"]["list"] and r["early"]["logins"], r["early"]
    assert r["late"]["items"] == ["GitHub"], r["late"]


@pytest.mark.skipif(not CHROME, reason="Chrome required")
def test_without_one_the_vault_stays_hidden_until_the_background_says_paired_and_then_a_safe_snapshot_is_kept():
    r = _run({}, "https://github.com/login")
    assert r["early"]["items"] == [] and not r["early"]["logins"], ("vault tabs shown before pairing was confirmed", r["early"])
    assert r["late"]["items"] == ["GitHub"], r["late"]
    written = json.dumps(r["writes"])
    assert r["writes"] and "LEAK" not in written and "password" not in written, written


@pytest.mark.skipif(not CHROME, reason="Chrome required")
def test_a_snapshot_from_another_site_does_not_claim_matches_here():
    r = _run(SNAP, "https://bank.example.com/")
    assert r["early"]["items"] == [] and "Checking this site" in r["early"]["text"], r["early"]


def test_the_in_page_styles_use_no_custom_properties_a_page_could_override():
    """content.css lands inside every website. A `var(--x)` there resolves against the PAGE's own
    custom properties, so a site could recolour (or hide) the autofill menu -- the theme is literals."""
    css = re.sub(r"/\*.*?\*/", "", (EXT / "content.css").read_text(), flags=re.S)
    assert "var(" not in css and "--" not in css.replace("-->", ""), "content.css uses custom properties"
