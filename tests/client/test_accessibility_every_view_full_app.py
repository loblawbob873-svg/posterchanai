"""Accessibility audit of EVERY view the sidebar lists, at desktop (1280x800) and phone (390x844) size.

Nobody had ever audited the client for a person who cannot see the screen or cannot use a mouse, and
the failures are invisible to anyone who can: a ⋯ button with no name is read out as "button", a
clickable <div> is simply skipped by Tab, an avatar with no alt is read as its file name. This walks
the real bundled client (the view list is read from the shipped sidebar, never a copied list), and in
each view measures what is on screen:

  * name      -- every visible interactive element has an accessible name;
  * icon      -- an icon-only control (svg/emoji, no letters) carries aria-label, not just a tooltip;
  * img       -- an image has alt (alt="" or aria-hidden for decoration);
  * keyboard  -- nothing that looks clickable (cursor:pointer) is a bare element Tab cannot reach;
  * tap       -- on a phone, toolbar/nav controls are at least 32px in both directions;
  * dup-id    -- no id appears twice in the DOM.

Violations are de-duplicated by (category, selector): the shell (header, sidebar, bottom nav) is
reported once under "shell", and a view lists only what it added. The failure names every one.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


# Out of scope, each for a stated reason. Matched as a substring of "category selector".
ALLOW = {
}

INIT = r"""
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
"""

AUDIT = r"""((mobile)=>{
 const INTERACTIVE='button,a[href],input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[role=option],[role=radio],[tabindex]:not([tabindex^="-"])';
 const NATIVE=/^(BUTTON|A|INPUT|SELECT|TEXTAREA|SUMMARY|LABEL|OPTION|VIDEO|AUDIO|IFRAME)$/;
 const out=[];
 const hiddenTree=el=>!!el.closest('[aria-hidden=true],[inert],template');
 const visible=el=>{ const r=el.getBoundingClientRect(); if(r.width<1||r.height<1) return false;
   const s=getComputedStyle(el); return s.visibility!=='hidden' && s.display!=='none' && +s.opacity!==0; };
 const norm=s=>String(s).replace(/[0-9a-f]{8,}/gi,'#').replace(/\d+/g,'#');
 const part=el=>{ let p=el.tagName.toLowerCase();
   if(el.id) return p+'#'+norm(el.id);
   const cls=[...el.classList].filter(c=>!/^(on|active|sel|open|hidden|show)$/.test(c)).slice(0,3);
   if(cls.length) p+='.'+cls.map(norm).join('.');
   for(const a of ['data-view','data-act','data-action','data-tab','data-k','role','type','name'])
     if(el.hasAttribute(a)){ p+='['+a+'='+norm(el.getAttribute(a)).slice(0,24)+']'; break; }
   return p; };
 const sel=el=>{ const parts=[part(el)]; let n=el.parentElement, hops=0;
   while(n && n!==document.body && hops<6){ if(n.id){ parts.unshift('#'+norm(n.id)); break; }
     if(hops<2) parts.unshift(part(n)); n=n.parentElement; hops++; }
   return parts.join(' > '); };
 const add=(cat,el,extra)=>out.push({cat,sel:sel(el)+(extra?' '+extra:'')});
 const byId=id=>document.getElementById(id);
 const textOf=el=>{ let t=(el.innerText||'').trim();
   el.querySelectorAll('img[alt]').forEach(i=>{ t+=' '+i.alt; });
   el.querySelectorAll('[aria-label]').forEach(i=>{ if(!i.closest('[aria-hidden=true]')) t+=' '+i.getAttribute('aria-label'); });
   return t.trim(); };
 const labelled=el=>{ const ids=(el.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
   return ids.map(i=>byId(i)).filter(Boolean).map(n=>(n.innerText||n.textContent||'').trim()).join(' ').trim(); };
 // -> [name, source]
 const accName=el=>{
   const lb=labelled(el); if(lb) return [lb,'aria'];
   const al=(el.getAttribute('aria-label')||'').trim(); if(al) return [al,'aria'];
   const tag=el.tagName;
   if(tag==='IMG' && (el.alt||'').trim()) return [el.alt.trim(),'alt'];
   if(tag==='INPUT'||tag==='SELECT'||tag==='TEXTAREA'){
     const t=(el.type||'').toLowerCase();
     if(['submit','button','reset'].includes(t) && el.value) return [el.value,'text'];
     if(t==='image') return [el.alt||'','alt'];
     if(el.id){ const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l&&(l.innerText||'').trim()) return [l.innerText.trim(),'label']; }
     const w=el.closest('label'); if(w&&(w.innerText||'').trim()) return [w.innerText.trim(),'label'];
     if(el.title) return [el.title,'title'];
     if(el.placeholder) return [el.placeholder,'placeholder'];
     return ['',''];
   }
   const t=textOf(el); if(t) return [t,'text'];
   if(el.title) return [el.title,'title'];
   return ['',''];
 };
 const LETTER=/[\p{L}\p{N}]/u;
 const seen=new Set();
 for(const el of document.querySelectorAll(INTERACTIVE)){
   if(seen.has(el)||hiddenTree(el)||!visible(el)) continue; seen.add(el);
   if(el.disabled && el.tagName!=='A') { /* still needs a name, but a disabled one is not reachable */ }
   const [name,src]=accName(el);
   if(!name){ add('name',el); continue; }
   if(src==='text'||src==='title'){
     const own=(el.innerText||'').trim();
     if(!LETTER.test(own) && !el.querySelector('img[alt]:not([alt=""])') && (src==='title' || !LETTER.test(name)))
       add('icon',el,src==='title'?'(title only)':'');
   }
   if(el.getAttribute('role') && !NATIVE.test(el.tagName) && !el.hasAttribute('tabindex') && !el.isContentEditable)
     add('keyboard',el,'(role without tabindex)');
   if(mobile){
     const r=(el.closest('label')||el).getBoundingClientRect();
     const onScreen=r.right>0&&r.left<innerWidth&&r.bottom>0&&r.top<innerHeight;
     const primary=el.closest('nav,header,[role=toolbar],[role=tablist],[role=navigation],.sidebar,.bottom-nav,[class*=toolbar],[class*=-bar],[class*=-head],[class*=tabs]');
     if(onScreen && primary && (r.width<32||r.height<32) && !(el.tagName==='A' && el.closest('p,li') && !el.closest('nav')))
       add('tap',el,'('+Math.round(r.width)+'x'+Math.round(r.height)+')');
   }
 }
 for(const img of document.querySelectorAll('img')){
   if(hiddenTree(img)||!visible(img)) continue;
   const role=img.getAttribute('role');
   if(!img.hasAttribute('alt') && role!=='presentation' && role!=='none') add('img',img);
 }
 for(const el of document.querySelectorAll('[role=img]')){
   if(hiddenTree(el)||!visible(el)) continue;
   if(!accName(el)[0]) add('img',el,'(role=img)');
 }
 // Looks clickable: sets cursor:pointer itself (not inherited), but Tab cannot reach it.
 for(const el of document.body.querySelectorAll('*')){
   if(NATIVE.test(el.tagName)||el.closest(INTERACTIVE+',label')) continue;
   if(el.namespaceURI!=='http://www.w3.org/1999/xhtml') continue;
   const s=getComputedStyle(el); if(s.cursor!=='pointer') continue;
   const p=el.parentElement; if(p && getComputedStyle(p).cursor==='pointer') continue;
   if(hiddenTree(el)||!visible(el)||el.isContentEditable) continue;
   add('keyboard',el);
 }
 const ids={};
 for(const el of document.querySelectorAll('[id]')) ids[el.id]=(ids[el.id]||0)+1;
 for(const [id,n] of Object.entries(ids)) if(n>1) out.push({cat:'dup-id',sel:'#'+id+' x'+n});
 return out;})(%s)"""


async def settle(b):
    for _ in range(30):
        if await b.js("!document.querySelector('#feed .spinner, #feed .loading')"):
            break
        await asyncio.sleep(.1)
    await asyncio.sleep(.6)


# A timeline with something on it: a profile with a picture, a post with an image, a reply and one of
# our own -- an empty feed would audit the empty state and none of the note cards people actually use.
SEED = r"""(async()=>{const NT=NostrTools, k=new Uint8Array(32).fill(7), other=NT.getPublicKey(k);
 const now=Math.floor(Date.now()/1000), img=location.origin+'/static/icon-192.png';
 const evs=[NT.finalizeEvent({kind:0,created_at:now,tags:[],content:JSON.stringify({name:'Ada',picture:img,about:'Fixture profile'})},k)];
 const root=NT.finalizeEvent({kind:1,created_at:now-60,tags:[['t','fixture'],['imeta','url '+img,'m image/png','alt A neon test card']],content:'A post with a picture '+img+' #fixture'},k);
 evs.push(root, NT.finalizeEvent({kind:1,created_at:now-30,tags:[['e',root.id,'','root'],['p',other]],content:'A reply to the picture'},k));
 for(let i=0;i<4;i++) evs.push(NT.finalizeEvent({kind:1,created_at:now-100-i,tags:[],content:'Plain post '+i},k));
 evs.push(await __PC.signTemplate({kind:1,pubkey:__PC.me().pubkey,created_at:now-5,tags:[],content:'My own post'}));
 window.__events=(window.__events||[]).concat(evs); for(const e of evs){ try{ Store.saveEvent(e); }catch(_){} }
 window.__seed={other, root:root.id}; return true;})()"""

GO = "try{{ document.querySelectorAll('.modal.open,.overlay.open').forEach(m=>m.classList.remove('open')); {} }}catch(e){{ (window.__errors=window.__errors||[]).push(String(e)) }}"


async def walk(b, mobile):
    found = {}
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(SEED)
    await b.js(GO.format("__PC.switchView('global')"))
    await settle(b)
    found["shell"] = await b.js(AUDIT % json.dumps(mobile))
    views = await b.js("[...new Set([...document.querySelectorAll('.sidebar .nav-item[data-view]')].map(b=>b.dataset.view))]")
    for v in views:
        await b.js(GO.format(f"__PC.switchView({json.dumps(v)})"))
        await settle(b)
        if v == "global":
            found["notes on the timeline"] = await b.js("document.querySelectorAll('#feed .note').length")
            # The picture's alt is the author's own NIP-92 description, not a generic word.
            found["picture alt"] = await b.js("[...document.querySelectorAll('#feed .media-row img')].map(i=>i.alt)")
        found[v] = await b.js(AUDIT % json.dumps(mobile))
    # Two screens no sidebar item opens, and the ones people spend the most time in.
    await b.js(GO.format("__PC.openProfile(__seed.other)"))
    await settle(b)
    found["profile"] = await b.js(AUDIT % json.dumps(mobile))
    await b.js(GO.format("__PC.openThread(__seed.root)"))
    await settle(b)
    found["thread"] = await b.js(AUDIT % json.dumps(mobile))
    # Keyboard, not just markup: Enter on a focused post opens it, Enter on the account card (a
    # role=button <div>) opens its menu. Real key events, through CDP.
    async def enter():
        for t in ("keyDown", "keyUp"):
            await b.call("Input.dispatchKeyEvent", {"type": t, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                                                    **({"text": "\r"} if t == "keyDown" else {})})
    await b.js(GO.format("__PC.switchView('global')"))
    await settle(b)
    await b.js("document.querySelector('#feed article.note[data-id]').focus()")
    await enter()
    await settle(b)
    found["enter opens a post"] = await b.js("__PC.VIEW")
    # a11y.js names an icon button from its title, and follows a title changed in place (a toggle).
    found["title follows"] = await b.js("""(async()=>{const x=document.createElement('button');x.title='react';
      x.innerHTML='<svg class="ic"></svg>';document.body.appendChild(x);await new Promise(r=>setTimeout(r,50));
      const first=x.getAttribute('aria-label');x.title='remove your reaction';await new Promise(r=>setTimeout(r,50));
      const second=x.getAttribute('aria-label');x.remove();return [first,second]})()""")
    if not mobile:
        await b.js("document.querySelectorAll('.menu-pop').forEach(x=>x.remove());document.querySelector('#me-card').focus()")
        await enter()
        await settle(b)
        found["enter opens the account menu"] = await b.js("!!document.querySelector('.menu-pop')")
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        # The windowed desktop: icons, taskbar, and the start menu.
        await b.js(GO.format("__PC.switchView('global'); PCOS.enter()"))
        await settle(b)
        found["os-desktop"] = await b.js(AUDIT % json.dumps(mobile))
        found["desktop icons"] = await b.js("document.querySelectorAll('#os-root .os-icon').length")
        await b.js("document.querySelector('#os-start') && document.querySelector('#os-start').click()")
        await settle(b)
        found["os-start-menu"] = await b.js(AUDIT % json.dumps(mobile))
        found["start menu rows"] = await b.js("document.querySelectorAll('#os-startmenu button').length")
    return views, found


def report(found):
    seen, grouped, counts = set(), {}, {}
    for view, rows in found.items():
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            continue   # a measurement ("notes on the timeline"), not an audit
        for r in rows:
            key = r["cat"] + " " + r["sel"]
            if key in seen or any(a in key for a in ALLOW):
                continue
            seen.add(key)
            grouped.setdefault(view, []).append(key)
            counts[r["cat"]] = counts.get(r["cat"], 0) + 1
    return grouped, counts


SIZES = {"desktop": (1280, 800, False), "mobile": (390, 844, True)}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("size", list(SIZES))
def test_every_view_is_accessible(size):
    width, height, mobile = SIZES[size]
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride",
                     {"width": width, "height": height, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile})
        await b.call("Page.reload")
        await b.until("!!window.__PC && document.body && document.body.classList.contains('guest')")
        got["views"], got["found"] = await walk(b, mobile)

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert len(got["views"]) >= 40, f"only {len(got['views'])} views walked"
    assert got["found"]["notes on the timeline"] >= 3, "the seeded posts never reached the timeline"
    assert got["found"]["enter opens a post"] == "thread", "Enter on a focused post did not open it"
    assert "A neon test card" in got["found"]["picture alt"], got["found"]["picture alt"]
    assert got["found"]["title follows"] == ["react", "remove your reaction"], got["found"]["title follows"]
    if not mobile:
        assert got["found"]["enter opens the account menu"], "Enter on the account card did nothing"
        assert got["found"]["desktop icons"] >= 10 and got["found"]["start menu rows"] >= 10, \
            ("the windowed desktop was never audited", got["found"]["desktop icons"], got["found"]["start menu rows"])
    grouped, counts = report(got["found"])
    total = sum(counts.values())
    lines = [f"{total} accessibility violations at {size} {width}x{height}: {counts}"]
    for view, keys in grouped.items():
        lines.append(f"  [{view}] {len(keys)}")
        lines += [f"    {k}" for k in sorted(keys)]
    assert total == 0, "\n".join(lines)
