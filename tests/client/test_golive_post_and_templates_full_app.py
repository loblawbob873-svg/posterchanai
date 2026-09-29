"""Go Live: the announcement post is editable, and a set-up can be saved as a template and reused.

Asked for: "Allow ability to edit the live stream post before you Go Live. If you can, have
templates that you can re-use. Make sure UI good on mobile too."

Runs the real Go Live sheet in the bundled client (only the ingest endpoint and the relay's answers
are fixtures) and asserts what a person sees and what gets published:

  * the sheet opens with the post box filled with the default text;
  * a template saved from the sheet is published to the ACCOUNT as an encrypted kind-30078
    `pcai:livetemplates` (the name is not readable in the event) and appears in the menu;
  * picking the template fills title / description / tags / post back in;
  * the kind-1 announcement is the EDITED text with {title}/{link} filled in, and still carries the
    stream's nostr: address, which is what makes it embed the player;
  * at phone width the template row and the post box fit the screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_live_stream_details_full_app import INGEST

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


FILL = """(()=>{const r=document;
  r.querySelector('#gl-title').value='Drawing anime';
  r.querySelector('#gl-summary').value='Sketching tonight';
  r.querySelector('#gl-tags').value='anime, art';
  r.querySelector('#gl-post').value='Come hang out: {title}\\nwatch here {link} #art';
})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_post_is_editable_and_a_template_is_saved_reused_and_published():
    async def check(b):
        await desktop.login(b)
        await b.js("""window.__ev=[]; Relay.publish=async (ev)=>{ __ev.push(ev); return {ok:true, msg:''}; };
                      Relay.query=async (f)=>{ return []; };""")
        await b.js("document.getElementById('nav-golive').click()")
        await b.until("!!document.querySelector('#gl-post')")
        assert await b.js("document.querySelector('#gl-post').value") == "🔴 I’m live now: {title}\n\n▶ Watch: {link}", \
            "the post box must open with the default announcement"
        assert await b.js("!document.querySelector('.gl-tpl').classList.contains('hidden')"), "no template controls"

        # --- phone width: the new rows fit ---------------------------------------------------------
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 360, "height": 780, "deviceScaleFactor": 3, "mobile": True})
        await asyncio.sleep(.4)
        await b.js("document.querySelector('#gl-tpl-save').click()")
        fit = await b.js("""(()=>{const W=innerWidth, r=e=>e.getBoundingClientRect();
          const els=['#gl-tpl-sel','#gl-tpl-save','#gl-tpl-name','#gl-tpl-ok','#gl-post'].map(s=>document.querySelector(s));
          return {W, bad:els.filter(e=>!e||r(e).right>W+1||r(e).left<-1||r(e).width<40).map(e=>e&&e.id),
                  modalOverflow:document.querySelector('.modal').scrollWidth>document.querySelector('.modal').clientWidth+1}})()""")
        assert not fit["bad"] and not fit["modalOverflow"], fit
        await b.call("Emulation.clearDeviceMetricsOverride", {})
        await b.js("document.querySelector('#gl-tpl-cancel').click()")

        # --- save a template -----------------------------------------------------------------------
        await b.js(FILL)
        await b.js("document.querySelector('#gl-tpl-save').click()")
        await b.js("document.querySelector('#gl-tpl-name').value='Art night'")
        await b.js("document.querySelector('#gl-tpl-ok').click()")
        await b.until("__ev.some(e=>e.kind===30078&&e.tags.some(t=>t[0]==='d'&&t[1]==='pcai:livetemplates'))")
        doc = await b.js("__ev.find(e=>e.kind===30078)")
        assert "Art night" not in doc["content"] and "Drawing" not in doc["content"], "templates must be encrypted"
        assert await b.js("[...document.querySelectorAll('#gl-tpl-sel option')].map(o=>o.value)") == ["", "Art night"]

        # --- reuse it ------------------------------------------------------------------------------
        await b.js("""(()=>{const r=document; r.querySelector('#gl-title').value='x'; r.querySelector('#gl-summary').value='';
          r.querySelector('#gl-post').value=''; const s=r.querySelector('#gl-tpl-sel'); s.value='Art night';
          s.dispatchEvent(new Event('change'));})()""")
        got = await b.js("""({t:document.querySelector('#gl-title').value, s:document.querySelector('#gl-summary').value,
                             g:document.querySelector('#gl-tags').value, p:document.querySelector('#gl-post').value})""")
        assert got == {"t": "Drawing anime", "s": "Sketching tonight", "g": "anime, art",
                       "p": "Come hang out: {title}\nwatch here {link} #art"}, got

        # --- go live: the edited post is what is published ------------------------------------------
        await b.js("""(()=>{const obs=document.querySelector('input[name=gl-src][value=obs]');
          if(obs){ obs.checked=true; obs.dispatchEvent(new Event('change',{bubbles:true})); }
          document.querySelector('#gl-go').click();})()""")
        await b.until("__ev.some(e=>e.kind===1)")
        note = await b.js("__ev.find(e=>e.kind===1)")
        c = note["content"]
        assert c.startswith("Come hang out: Drawing anime\nwatch here http"), c
        assert "#art" in c and "{link}" not in c and "{title}" not in c, c
        assert "\n\nnostr:naddr1" in c, "the post must keep the stream's nostr: address (the embedded player)"
        assert any(t[0] == "a" and t[1].startswith("30311:") for t in note["tags"]), note["tags"]

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check, extra_init=INGEST))


def _node(js):
    import json
    import subprocess
    r = subprocess.run(["node", "-e", js, str(ROOT / "static/js/client/livetemplates.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_post_keeps_the_stream_address_and_fills_the_blanks():
    out = _node(r"""const T=require(process.argv[1]);
    process.stdout.write(JSON.stringify({
      blank:T.renderPost('   ',{title:'T',link:'https://x/L',naddr:'naddr1abc'}),
      custom:T.renderPost('On now: {title}',{title:'Chess',link:'https://x/L',naddr:'naddr1abc'}),
      kept:T.renderPost('see nostr:naddr1abc',{title:'T',link:'L',naddr:'naddr1abc'}),
      untitled:T.renderPost('{title}',{title:'',naddr:'n'})}))""")
    assert out["blank"] == "🔴 I’m live now: T\n\n▶ Watch: https://x/L\n\nnostr:naddr1abc"
    assert out["custom"] == "On now: Chess\n\nnostr:naddr1abc", "deleting the link must not drop the embed"
    assert out["kept"] == "see nostr:naddr1abc", "the address must not be added twice"
    assert out["untitled"].startswith("Live stream")


def test_templates_follow_the_replaceable_doc_rule():
    """Nothing is published until a relay has ANSWERED a read; a change made while the relays could
    not be read is kept on the device and applied to the account copy once they can; a delete stays
    deleted after merging with the account copy."""
    out = _node(r"""const T=require(process.argv[1]);
    (async()=>{
      let answer=null, pubs=[], local=[];
      const acct=[{name:'Chess',title:'chess',t:5},{name:'Art',title:'art',t:4}];
      const s=T.store({owner:'me', query:async()=>answer,
        publish:async(k,c,t)=>{pubs.push({k,c:JSON.parse(c),t}); return {ok:true};},
        enc:async(pk,t)=>t, dec:async(pk,t)=>t,
        local:{get:()=>local, set:l=>{local=l;}}});
      // relays down: kept locally, not published
      const r1=await s.save(l=>T.upsert(l,{name:'Music',title:'m'},10));
      const offline={synced:r1.synced, pubs:pubs.length, names:local.map(x=>x.name)};
      // relays answer with the account copy: the device's template merges in, the delete sticks
      answer=[{created_at:1, content:JSON.stringify({v:1,templates:acct})}];
      const r2=await s.save(l=>T.remove(l,'Art'));
      process.stdout.write(JSON.stringify({offline, synced:r2.synced, published:pubs.map(p=>p.c.templates.map(x=>x.name)),
        d:pubs[0]&&pubs[0].t, cap:T.normalize(Array.from({length:30},(_,i)=>({name:'n'+i,t:i}))).length}));
    })();""")
    assert out["offline"] == {"synced": False, "pubs": 0, "names": ["Music"]}, "published over an unread account doc"
    assert out["synced"] and out["published"] == [["Music", "Chess"]], out
    assert out["d"] == [["d", "pcai:livetemplates"]]
    assert out["cap"] == 20


def test_the_doc_is_pinned_and_carried_like_every_private_doc():
    store = (ROOT / "static/js/client/store.js").read_text()
    app = (ROOT / "static/js/client/app.js").read_text()
    assert "t[1] === 'pcai:livetemplates'" in store, "evicted from the cache it reads as 'no templates'"
    assert "/^pcai:livetemplates$/" in app, "left behind on a relay change it reads as 'no templates'"
    assert "livetemplates.js" in (ROOT / "templates/client.html").read_text()
