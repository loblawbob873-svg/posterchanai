"""Making a MySpace page: the Theme button on your own profile, and the viewer's two switches.

The shipped client in Chrome against a stub relay that records what it is sent and accepts it: press Theme, pick a
preset, give the name its own colour and the borders none (Vyram's marked-up profile, 2026-10-29), Save -- the event
that goes out is Ditto's kind 16767 with the three shared colours first, and the page repaints at once. Then, with
Settings -> Appearance's switches off, a themed profile with a song shows neither.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

INIT = r'''
window.__published = [];
const __SK2 = new Uint8Array(32).fill(2);
function __other(){
  const T = window.NostrTools, pk = T.getPublicKey(__SK2), now = Math.floor(Date.now()/1000);
  window.__OTHER = pk;
  return { pk,
    k0: T.finalizeEvent({ kind: 0, created_at: now - 60, tags: [], content: JSON.stringify({ name: 'Themed', fields: [['🎶 Song', location.origin + '/x.wav']] }) }, __SK2),
    theme: T.finalizeEvent({ kind: 16767, created_at: now - 30, content: '', tags: [['c','#1c253f','background'],['c','#f6f0ea','text'],['c','#c03838','primary']] }, __SK2) };
}
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  if(m[0]==='EVENT'){ window.__published.push(m[1]); out(['OK', m[1].id, true, '']); return; }
  if(m[0]!=='REQ') return; const id=m[1];
  let O=null; try{ O=__other(); }catch(_){}
  for(const f of m.slice(2)){
    if(O && (f.authors||[]).includes(O.pk)){ if((f.kinds||[]).includes(0)) out(['EVENT',id,O.k0]); if((f.kinds||[]).includes(16767)) out(['EVENT',id,O.theme]); }
    for(const e of window.__published) if((f.kinds||[]).includes(e.kind) && (f.authors||[]).includes(e.pubkey)) out(['EVENT',id,e]);
  }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FS.OPEN=1;FS.CONNECTING=0;FS.CLOSED=3;window.WebSocket=FS;
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@CHROME
def test_the_theme_button_publishes_a_ditto_theme_with_vyrams_parts_and_the_page_repaints():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#theme-prof')")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('.pt-ed [data-preset]')")
        await b.js("""(()=>{const ed=document.querySelector('.pt-ed');
          ed.querySelector('[data-preset="Paper"]').click();
          const row=ed.querySelector('.pt-row[data-k="p:name"]'), inp=row.querySelector('input');
          inp.value='#b4472f'; inp.dispatchEvent(new Event('input',{bubbles:true}));
          const bd=ed.querySelector('#pt-border'); bd.value='none'; bd.dispatchEvent(new Event('change',{bubbles:true}));
          window.__prevBg=getComputedStyle(ed.querySelector('#pt-prev')).backgroundColor;
          ed.querySelector('#pt-save').click();})()""")
        await b.until("window.__published.some(e=>e.kind===16767)")
        await b.until("!document.querySelector('.pt-ed')")
        await b.until("document.querySelector('#feed').dataset.ptheme==='1'")
        got['ev'] = await b.js("window.__published.filter(e=>e.kind===16767).pop()")
        got['page'] = await b.js("""(()=>{const f=document.querySelector('#feed');
          return {bg:f.style.getPropertyValue('--bg'), name:getComputedStyle(document.querySelector('.prof .pbody h2')).color,
                  bannerWidth:getComputedStyle(document.querySelector('.prof .banner')).borderTopWidth, preview:window.__prevBg};})()""")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    tags = got['ev']['tags']
    assert tags[:3] == [['c', '#f6f1e7', 'background'], ['c', '#2b2118', 'text'], ['c', '#b4472f', 'primary']], tags
    assert ['c', '#b4472f', 'name'] in tags and ['border', 'none'] in tags and ['client', 'PosterChan'] in tags, tags
    page = got['page']
    assert page['bg'] == '#f6f1e7' and page['name'] == 'rgb(180, 71, 47)' and page['bannerWidth'] == '0px', page
    assert page['preview'] == 'rgb(246, 241, 231)', ("the editor's preview did not show the preset", page)



@CHROME
def test_a_theme_to_start_from_is_the_whole_look_and_is_what_gets_saved():
    """'maybe add more profile themes that people can choose' (2026-10-29): twenty-odd themes, each drawn in its own
    colours, and picking one brings EVERY control into step -- colours, the text font, the title font, the part colours
    and the borders -- so Save publishes exactly what the preview shows."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#theme-prof')")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('.pt-ed [data-preset]')")
        got['swatches'] = await b.js("""[...document.querySelectorAll('.pt-ed [data-preset]')].map(x=>({n:x.dataset.preset,
          bg:getComputedStyle(x).backgroundColor}))""")
        await b.js("document.querySelector('.pt-ed [data-preset=\"Haunted\"]').click()")
        got['ui'] = await b.js("""(()=>{const ed=document.querySelector('.pt-ed'), r=k=>ed.querySelector('.pt-row[data-k="'+k+'"]');
          return {font:ed.querySelector('#pt-font').value, border:ed.querySelector('#pt-border').value,
                  bg:r('background').querySelector('input').value, name:r('p:name').querySelector('input').value,
                  nameOn:!r('p:name').classList.contains('pt-off'), tabsOn:!r('p:tabs').classList.contains('pt-off'),
                  on:[...ed.querySelectorAll('[data-preset].on')].map(x=>x.dataset.preset)};})()""")
        await b.js("document.querySelector('.pt-ed #pt-save').click()")
        await b.until("window.__published.some(e=>e.kind===16767)")
        got['ev'] = await b.js("window.__published.filter(e=>e.kind===16767).pop()")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    sw = got['swatches']
    assert len(sw) >= 20, ("more themes to choose from", len(sw))
    assert len({x['bg'] for x in sw}) >= 15, ("each theme is drawn in its own colours", sw)
    ui = got['ui']
    assert ui == {'font': 'Special Elite', 'border': 'solid', 'bg': '#0d0b0b', 'name': '#e0312f', 'nameOn': True,
                  'tabsOn': False, 'on': ['Haunted']}, ui
    tags = got['ev']['tags']
    assert tags[:3] == [['c', '#0d0b0b', 'background'], ['c', '#ddd5c7', 'text'], ['c', '#b3121f', 'primary']], tags
    fonts = {t[3]: t[1] for t in tags if t[0] == 'f'}
    assert fonts == {'body': 'Special Elite', 'title': 'Creepster'}, tags
    assert ['c', '#e0312f', 'name'] in tags and any(t[:3] == ['border', 'show', 'solid'] for t in tags) and ['title', 'Haunted'] in tags, tags

@CHROME
def test_with_the_switches_off_a_profile_keeps_the_apps_look_and_stays_quiet():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ localStorage.setItem('pc_prof_themes','0'); localStorage.setItem('pc_prof_autoplay','0'); if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        for t in ('mousePressed', 'mouseReleased'):
            await b.call('Input.dispatchMouseEvent', {'type': t, 'x': 700, 'y': 500, 'button': 'left', 'clickCount': 1})
        await b.js("try{ __other(); }catch(_){}")
        await b.until("!!window.__OTHER")
        await b.js("__PC.openProfile(window.__OTHER)")
        await b.until("!!document.querySelector('#feed .prof-track audio')")
        await asyncio.sleep(2)
        got['s'] = await b.js("""(()=>{const f=document.querySelector('#feed'), a=document.querySelector('#feed .prof-track audio');
          return {themed:f.dataset.ptheme||'', bg:f.style.getPropertyValue('--bg'), playing:!!a && !a.paused, chip:!!document.querySelector('.pt-song')};})()""")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['s'] == {'themed': '', 'bg': '', 'playing': False, 'chip': False}, got['s']


@CHROME
def test_close_publishes_nothing_and_upload_opens_the_picker_and_says_what_became_of_the_file():
    """The editor's two other buttons, pressed: ✕ closes it with nothing sent; Upload opens the file picker, and a chosen
    picture's uploaded address fills the background field, or the person is told why not -- and the button never stays
    stuck on 'Uploading…'."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("window.__toasts=[];{const t=__PC.toast;__PC.toast=(m)=>{__toasts.push(String(m));};}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#theme-prof')")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('#pt-close')")
        await b.js("document.querySelector('#pt-close').click()")
        await b.until("!document.querySelector('.pt-ed')")
        got['published'] = await b.js("window.__published.filter(e=>e.kind===16767).length")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('#pt-up')")
        await b.js("""(()=>{window.__picker=0;const f=document.querySelector('#pt-file');const c=f.click.bind(f);f.click=()=>{__picker++;};
          document.querySelector('#pt-up').click();
          const d=new DataTransfer();d.items.add(new File([new Uint8Array([137,80,78,71])],'bg.png',{type:'image/png'}));
          f.files=d.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()""")
        await b.until("document.querySelector('#pt-up').textContent==='Upload' && !document.querySelector('#pt-up').disabled")
        got['picker'] = await b.js("__picker")
        got['bg'] = await b.js("document.querySelector('#pt-bgurl').value")
        got['toasts'] = await b.js("__toasts")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['published'] == 0, "closing the editor published a theme"
    assert got['picker'] == 1, "Upload did not open the file picker"
    # The uploaded picture's address fills the field (the harness's upload answers with its own fixture address).
    assert got['bg'].endswith('/fixture.png') or any('did not upload' in t for t in got['toasts']), got


@CHROME
def test_vyrams_theme_follow_ups_border_styles_per_element_hex_codes_and_the_edit_button():
    """Vyram, 'Themes' (2026-10-30): borders are a STYLE (dashes, triangles, circles, squares) shown or hidden per
    element; "The banner does not need a border around it"; "Colors need #codeinput next to them"; "Whats up with the
    edit button?" (it kept its gradient while Pay and Theme took the theme's colour)."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#theme-prof')")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('.pt-ed [data-preset]')")
        got['ui'] = await b.js("""(async()=>{const ed=document.querySelector('.pt-ed'), q=s=>ed.querySelector(s);
          q('[data-preset="Paper"]').click();
          const row=q('.pt-row[data-k="background"]'), hex=row.querySelector('.pt-hex'), pick=row.querySelector('input[type=color]');
          hex.value='#12345a'; hex.dispatchEvent(new Event('input',{bubbles:true}));
          const picked=pick.value;
          ed.querySelector('details').open=true;
          const bd=q('#pt-border'); bd.value='triangles'; bd.dispatchEvent(new Event('change',{bubbles:true}));
          const elsShown=!q('#pt-bels').hidden;
          const box=k=>q('[data-bel="'+k+'"]');
          box('banner').checked=false; box('banner').dispatchEvent(new Event('change',{bubbles:true}));
          box('buttons').checked=true; box('buttons').dispatchEvent(new Event('change',{bubbles:true}));
          const hexCount=ed.querySelectorAll('.pt-row .pt-hex').length, rows=ed.querySelectorAll('.pt-row').length;
          q('#pt-save').click();
          return {picked, elsShown, hexCount, rows};})()""")
        await b.until("window.__published.some(e=>e.kind===16767)")
        await b.until("!document.querySelector('.pt-ed')")
        await b.until("document.querySelector('#feed').hasAttribute('data-pb-avatar')")
        got['ev'] = await b.js("window.__published.filter(e=>e.kind===16767).pop()")
        got['page'] = await b.js("""(()=>{const cs=s=>getComputedStyle(document.querySelector(s));
          return {banner:cs('.prof .banner').borderTopWidth, avatarImg:cs('.prof .pav').borderImageSource,
                  edit:cs('#edit-prof').backgroundImage+'|'+cs('#edit-prof').backgroundColor,
                  pay:cs('#prof-pay').backgroundImage+'|'+cs('#prof-pay').backgroundColor};})()""")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    ui, tags, page = got['ui'], got['ev']['tags'], got['page']
    assert ui['picked'] == '#12345a', ("a typed #hex did not move the colour picker", ui)
    assert ui['hexCount'] == ui['rows'], ("every colour needs its #hex field", ui)
    assert ui['elsShown'], ("choosing a border style must offer the per-element switches", ui)
    assert ['c', '#12345a', 'background'] in tags, tags
    border = [t for t in tags if t[0] == 'border']
    assert border and border[0][:3] == ['border', 'show', 'triangles'], border
    els = border[0][3].split(',')
    assert 'banner' not in els and 'buttons' in els and 'avatar' in els, border
    assert page['banner'] in ('0px', ''), ("the banner still has a border", page)
    assert 'svg' in page['avatarImg'], ("triangles are a pattern border on the avatar", page)
    assert page['edit'] == page['pay'], ("the Edit button does not follow the theme like Pay", page)


@CHROME
def test_the_song_bar_stays_in_the_header():
    """'Why is this at the top & scrolling down with you?' -- the profile's song bar was position:sticky."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__other(); __PC.openProfile(window.__OTHER)")
        await b.until("!!document.querySelector('.pt-song')")
        got['pos'] = await b.js("getComputedStyle(document.querySelector('.pt-song')).position")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['pos'] == 'static', got


@CHROME
@pytest.mark.parametrize("lang,word", [("en-US", "colors"), ("en-GB", "colours")])
def test_colour_or_color_follows_the_browsers_english(lang, word):
    """'Maybe if your language is set to English(GB), we English(US) colors' (2026-10-30)."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#theme-prof')")
        await b.js("document.querySelector('#theme-prof').click()")
        await b.until("!!document.querySelector('.pt-ed summary')")
        got['summary'] = await b.js("document.querySelector('.pt-ed summary').textContent")

    lang_init = "Object.defineProperty(navigator,'languages',{get:()=>[%r]});Object.defineProperty(navigator,'language',{get:()=>%r});" % (lang, lang)
    asyncio.run(desktop.with_browser('online', '', check, lang_init + INIT))
    assert word in got['summary'].lower(), got
