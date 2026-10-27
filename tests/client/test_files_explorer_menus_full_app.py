"""Files laid out like File Explorer, in the SHIPPED client, at phone and desktop sizes.

Asked 2026-10-26: "The file manager is a clunky mess ... mimic Windows File Explorer. It needs a traditional toolbar for
the file operations (file, edit, Admin) and to reduce the cluttered button mess throughout the UI. Navigating through
folders should look and mimic Windows File Explorer, show only the folder and the name, and list of files, now we waste
too much UI to folder, they are like big boxes", then "make sure file manager is good on mobile too. fully tested!" and
"a cyberpunk UI, adaptable to each theme". Measured on the real screens, every press a real mouse event at the control's
centre after proving nothing covers it (music_art_fixture.press):

  * a MENU BAR -- File, Edit, View, and Admin for an admin only -- and nothing else in the pane is a loose button: the
    address row (locations, back, up, the path), the column headings, the rows and one ⋯ per row;
  * the drive's home lists each folder as ONE ROW (not a box), and the list of files is Details by default;
  * File holds the uploads and New folder / New document and presses the real ones (the file picker opens); Edit's
    Select all selects what is on screen and the status bar says so, which enables File -> Download selected; View
    switches Details / Tiles and sorts;
  * a file's ⋯ (and a right-click on it) lists that file's actions, and Move to... opens its folder list BESIDE the ⋯,
    not in a corner;
  * dropping files anywhere on the pane is a drop on the uploader;
  * a status bar says how many items and how many are selected;
  * the neon is the THEME's: the open menu glows in that theme's --neon, in two different themes;
  * nothing sideways, and on a phone every menu, row and ⋯ is a finger-sized target.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client.test_files_phone_layout_full_app import DRIVE

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
SIZES = [(390, 844), (360, 780), (844, 390), (1280, 860)]
# Phone-sized layouts: narrow, or short (a phone held sideways is wider than 820px) -- files-explorer.css's rule.
def _phone(w, h):
    return w <= 820 or h <= 600


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

# The signed-in fixture account (key 0x01.., desktop.login's) made an admin of this instance, for the Admin menu --
# from the FIRST page load, since the client reads its config at boot, before the test could flip anything.
ME = nostr_service.npub_of(bip340.pubkey_from_seckey(b"\x01" * 32).hex())
ADMIN = r"""
const _cfgFetch = window.fetch;
window.fetch = async function(url, opts){
  const r = await _cfgFetch.apply(this, arguments);
  if(String(url).includes('/client/config')){
    try{ const j = await r.clone().json();
      return new Response(JSON.stringify({...j, admin_npubs: [%r]}), {status: 200, headers: {'Content-Type': 'application/json'}});
    }catch(_){ }
  }
  return r;
};
""" % ME

# Every <button> drawn in the pane that is not part of the explorer's furniture: the clutter this rework removes.
LOOSE = r"""(()=>{const main=document.querySelector('.fx-main'); if(!main) return ['no .fx-main'];
  const ok='.fx-menu,.fx-nb,.fx-crumb,.fx-col,.fx-home-tile,.fx-rowmore,.fx-locations-open,.selbox,summary,.bl-more';
  return [...main.querySelectorAll('button,select,label.btn')].filter(b=>b.checkVisibility() && !b.matches(ok))
    .map(b=>(b.id?'#'+b.id:'.'+String(b.className).split(' ')[0])+' '+(b.textContent||'').trim().slice(0,20));})()"""

GEOM = r"""(()=>{const R=e=>{const r=e.getBoundingClientRect();return {t:r.top,b:r.bottom,l:r.left,r:r.right,h:r.height,w:r.width}};
  const vis=s=>[...document.querySelectorAll(s)].filter(e=>e.checkVisibility()).map(R);
  return {vw:innerWidth, sideways:document.documentElement.scrollWidth>innerWidth+1,
    menus:[...document.querySelectorAll('.fx-menubar .fx-menu')].filter(e=>e.checkVisibility()).map(e=>({name:e.textContent.trim(),...R(e)})),
    homeRows:vis('.fx-home-tile'), rows:vis('.files-grid .file-card'), more:vis('.files-grid .fx-rowmore'),
    names:[...document.querySelectorAll('.files-grid .file-card .fx-name, .files-grid .file-card .meta')]
      .filter(e=>e.closest('.file-card').checkVisibility()).map(e=>({w:e.getBoundingClientRect().width,txt:e.textContent.trim()})),
    list:(()=>{const g=document.querySelector('#bl-grid');return g?R(g):null})(),
    details:!!document.querySelector('#bl-grid.details'),
    status:(document.getElementById('fx-status')||{}).textContent||''}})()"""

MENU = "[...document.querySelectorAll('.menu-pop button, .menu-pop label')].map(b=>({t:b.textContent.trim(),d:b.disabled}))"


async def _open_files(b, w, h, admin=False):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if w <= 900 else 1,
                                                        "mobile": w <= 900})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; localStorage.removeItem('pc_nostr_settings_filesView'); __PC.switchView('blossom'); 1")
    await b.until("!!document.querySelector('.fx-menubar') && !!document.getElementById('fx-explorer-css')")


async def _shot(b, name):
    import base64
    import os
    if not os.environ.get("PC_FILES_SHOTS"):
        return
    Path(os.environ["PC_FILES_SHOTS"]).mkdir(parents=True, exist_ok=True)
    d = (await b.call("Page.captureScreenshot", {"format": "png"}))["data"]
    Path(os.environ["PC_FILES_SHOTS"], name + ".png").write_bytes(base64.b64decode(d))


async def _menu(b, name):
    await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); 1")
    await A.press(b, f".fx-menubar .fx-menu[data-fxmenu='{name}']")
    await b.until("!!document.querySelector('.menu-pop')")
    return await b.js(MENU)


async def _pick(b, label):
    sel = await b.js("(()=>{const x=[...document.querySelectorAll('.menu-pop button')].find(e=>e.textContent.trim()===%s);"
                     "if(!x)return ''; x.id='fx-pick-me'; return '#fx-pick-me'})()" % json.dumps(label))
    assert sel, ("no menu item", label, await b.js(MENU))
    await A.press(b, sel)


async def _all_files(b):
    await A.press(b, ".fx-home-tile[data-folder='']")
    await b.until("document.querySelectorAll('#bl-grid .file-card[data-sha]').length>=30")
    await asyncio.sleep(.3)


@CHROME
@pytest.mark.parametrize("w,h", SIZES)
def test_the_explorer_is_a_menu_bar_folder_rows_and_a_list(w, h):
    got = {}

    async def check(b):
        await _open_files(b, w, h)
        await b.until("document.querySelectorAll('.fx-home-tile').length>=2")
        await _shot(b, f"home-{w}x{h}")
        got["home"] = await b.js(GEOM)
        got["home_loose"] = await b.js(LOOSE)
        await _all_files(b)
        await _shot(b, f"list-{w}x{h}")
        got["list"] = await b.js(GEOM)
        got["list_loose"] = await b.js(LOOSE)
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, DRIVE))
    shape, phone = (w, h), _phone(w, h)
    home, lst = got["home"], got["list"]
    assert [m["name"] for m in home["menus"]] == ["File", "Edit", "View"], (shape, "the menu bar", home["menus"])
    for m in home["menus"]:
        # A phone: a finger. A desktop: the app's own control size there (the page is drawn at 0.72 zoom).
        assert m["h"] >= (43.5 if phone else 20), (shape, "a menu is not a usable target", m)
    assert home["homeRows"] and len({round(r["t"]) for r in home["homeRows"]}) == len(home["homeRows"]), \
        (shape, "the folders are boxes side by side, not one row each", home["homeRows"])
    for r in home["homeRows"]:
        assert r["h"] <= 56 and r["w"] >= home["list"]["w"] * 0.9, (shape, "a folder is a box, not a row", r, home["list"])
    assert got["home_loose"] == [], (shape, "loose buttons on the home screen", got["home_loose"])
    assert "folder" in home["status"] and "stored" in home["status"], (shape, "the status bar", home["status"])
    assert lst["details"], (shape, "the list is not Details by default")
    assert got["list_loose"] == [], (shape, "loose buttons over the list", got["list_loose"])
    assert lst["rows"] and all(r["h"] <= (56 if phone else 44) for r in lst["rows"]), (shape, "a file row is not a row", lst["rows"][:3])
    assert len(lst["more"]) == len(lst["rows"]), (shape, "not one ⋯ per row", len(lst["more"]), len(lst["rows"]))
    assert lst["names"] and all(n["w"] >= 80 and n["txt"] for n in lst["names"]), \
        (shape, "a file's NAME is squeezed out of its row", [n for n in lst["names"] if n["w"] < 80][:3])
    if phone:
        assert all(m["w"] >= 43.5 and m["h"] >= 43.5 for m in lst["more"]), (shape, "a ⋯ is smaller than a finger", lst["more"][:2])
    assert lst["status"].startswith("60 items"), (shape, "the status bar", lst["status"])
    assert not home["sideways"] and not lst["sideways"], (shape, "the page scrolls sideways")
    assert not got["errors"], (shape, got["errors"])


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_the_menus_do_what_the_buttons_did(w, h):
    got = {}

    async def check(b):
        await _open_files(b, w, h, admin=True)
        await _all_files(b)
        got["menus"] = await b.js("[...document.querySelectorAll('.fx-menubar .fx-menu')].map(e=>e.textContent.trim())")
        got["file"] = await _menu(b, "file")
        # Upload files... presses the real picker.
        await b.js("window.__picked=[]; const _c=HTMLInputElement.prototype.click;"
                   "HTMLInputElement.prototype.click=function(){ if(this.type==='file'){ __picked.push(this.id); return; } return _c.call(this); }; 1")
        # File -> Upload files… presses #bl-pick, Upload folder… presses #bl-pickfolder: each opens its own picker.
        await _pick(b, "Upload files…")
        await _menu(b, "file")
        await _pick(b, "Upload folder…")
        got["picked"] = await b.js("__picked.slice()")
        got["edit"] = await _menu(b, "edit")
        await _pick(b, "Select all (60)")
        await asyncio.sleep(.3)
        got["status_sel"] = await b.js("document.getElementById('fx-status').textContent")
        got["file_sel"] = await _menu(b, "file")
        await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); 1")
        await _menu(b, "edit")
        await _pick(b, "Select none")
        await asyncio.sleep(.3)
        got["status_none"] = await b.js("document.getElementById('fx-status').textContent")
        got["view"] = await _menu(b, "view")
        await _pick(b, "Sort: Name A–Z")
        await b.until("document.querySelector('#fx-sort') && document.querySelector('#fx-sort').value==='name:1'")
        await b.until("document.querySelectorAll('#bl-grid .file-card[data-sha]').length>=30")
        names = await b.js("[...document.querySelectorAll('#bl-grid .file-card .fname')].slice(0,5).map(e=>e.textContent.trim())")
        got["sorted"] = names
        await _menu(b, "view")
        await _pick(b, "Tiles")
        await b.until("!document.querySelector('#bl-grid.details') && document.querySelectorAll('#bl-grid .file-card[data-sha]').length>=30")
        got["tiles_more"] = await b.js("[...document.querySelectorAll('#bl-grid .file-card')].filter(c=>c.querySelector('.fx-rowmore')?.checkVisibility()).length")
        got["tiles_loose"] = await b.js(LOOSE)
        await _menu(b, "view")
        await _pick(b, "Details")
        await b.until("!!document.querySelector('#bl-grid.details')")
        got["admin"] = await _menu(b, "admin")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, ADMIN + DRIVE))
    shape = (w, h)
    assert got["menus"] == ["File", "Edit", "View", "Admin"], (shape, "an admin's menu bar", got["menus"])
    file_items = [i["t"] for i in got["file"]]
    for want in ("Upload files…", "Upload folder…", "New folder", "New document", "Download selected"):
        assert want in file_items, (shape, "File is missing", want, file_items)
    assert next(i for i in got["file"] if i["t"] == "Download selected")["d"], (shape, "Download selected with nothing selected")
    assert got["picked"] == ["bl-file", "bl-folder"], (shape, "Upload files… / Upload folder… (#bl-pick / #bl-pickfolder) did not open their pickers", got["picked"])
    assert "Select all (60)" in [i["t"] for i in got["edit"]], (shape, got["edit"])
    assert got["status_sel"] == "60 items · 60 selected", (shape, "Select all, by the status bar", got["status_sel"])
    dl = next(i for i in got["file_sel"] if i["t"].startswith("Download selected"))
    assert dl["t"] == "Download selected (60)" and not dl["d"], (shape, "File -> Download selected after Select all", dl)
    assert got["status_none"] == "60 items", (shape, "Select none", got["status_none"])
    view = [i["t"] for i in got["view"]]
    assert "✓ Details" in view and "Tiles" in view and "✓ Sort: Newest first" in view, (shape, "View", view)
    assert got["sorted"] == sorted(got["sorted"], key=str.lower), (shape, "Sort: Name A–Z", got["sorted"])
    assert got["tiles_more"] >= 30, (shape, "tiles without their ⋯", got["tiles_more"])
    assert got["tiles_loose"] == [], (shape, "loose buttons over the tiles", got["tiles_loose"])
    assert "Storage by user" in [i["t"] for i in got["admin"]], (shape, "Admin", got["admin"])
    assert not got["errors"], (shape, got["errors"])


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_a_files_more_and_right_click_list_its_actions_and_move_opens_beside_it(w, h):
    got = {}

    async def check(b):
        await _open_files(b, w, h)
        await _all_files(b)
        sha = await b.js("document.querySelector('#bl-grid .file-card[data-sha]').dataset.sha")
        more = f"#bl-grid .file-card[data-sha='{sha}'] .fx-rowmore"
        xy = await A.press(b, more)
        await b.until("!!document.querySelector('.menu-pop')")
        got["menu"] = [i["t"] for i in await b.js(MENU)]
        await _pick(b, "Move to…")
        await b.until("!!document.querySelector('.menu-pop') && /All/.test(document.querySelector('.menu-pop').innerText)")
        await asyncio.sleep(.6)            # a phone's sheet slides up into place
        got["move_at"] = await b.js("(()=>{const r=document.querySelector('.menu-pop').getBoundingClientRect();return {l:r.left,t:r.top,r:r.right,b:r.bottom}})()")
        got["more_at"] = xy
        await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); 1")
        if w > 900:
            r = await b.js(f"(()=>{{const e=document.querySelector(\"#bl-grid .file-card[data-sha='{sha}'] .fx-name\").getBoundingClientRect();return {{x:e.left+20,y:e.top+e.height/2}}}})()")
            await b.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": r["x"], "y": r["y"], "button": "right", "clickCount": 1})
            await b.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": r["x"], "y": r["y"], "button": "right", "clickCount": 1})
            await b.until("!!document.querySelector('.menu-pop')")
            got["right"] = [i["t"] for i in await b.js(MENU)]
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, DRIVE))
    shape = (w, h)
    for want in ("Open", "Copy link", "Download", "Rename", "Move to…", "Delete"):
        assert want in got["menu"], (shape, "the ⋯ menu is missing", want, got["menu"])
    m, x = got["move_at"], got["more_at"]
    if w <= 820:      # a phone: the app's menus are a sheet along the bottom -- on screen, full width
        assert m["b"] <= h + 1 and m["t"] >= h / 3 and m["r"] - m["l"] >= w * 0.8, (shape, "Move to… sheet", m)
    else:             # a desktop: beside the ⋯ it was picked from, never in a corner
        assert abs((m["t"] + m["b"]) / 2 - x["y"]) < 260 and m["r"] > 0 and m["l"] < w, \
            (shape, "Move to… opened away from the ⋯ it was picked from", m, x)
        assert m["t"] > 40 or x["y"] < 120, (shape, "Move to… opened in the corner", m)
    if w > 900:
        assert got["right"] == got["menu"], (shape, "a right-click is not the same menu", got["right"], got["menu"])
    assert not got["errors"], (shape, got["errors"])


@CHROME
def test_dropping_on_the_list_is_a_drop_on_the_uploader():
    got = {}

    async def check(b):
        await _open_files(b, 1280, 860)
        await _all_files(b)
        got["over"] = await b.js(r"""(()=>{const main=document.querySelector('.fx-main'), g=document.querySelector('#bl-grid');
          const dt=new DataTransfer(); dt.items.add(new File(['hello'],'a.txt',{type:'text/plain'}));
          window.__up=[]; g.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt}));
          return main.classList.contains('fx-dropping')})()""")
        got["dz_visible"] = await b.js("!!document.querySelector('#bl-drop .dz-inner')?.checkVisibility()")

    asyncio.run(desktop.with_browser("online", "", check, DRIVE))
    assert got["over"], "dragging files over the list does not offer the drop"
    assert not got["dz_visible"], "the old drop box is still drawn"


@CHROME
@pytest.mark.parametrize("theme", ["cyberpunk", "win98"])
def test_the_neon_is_the_themes_own(theme):
    got = {}

    async def check(b):
        await _open_files(b, 1280, 860)
        await b.js(f"document.documentElement.setAttribute('data-theme', {json.dumps(theme)}); 1")
        await asyncio.sleep(.2)
        await _menu(b, "file")
        await asyncio.sleep(.4)            # the open menu eases into its colour (transition .12s)
        got["menu"] = await b.js("getComputedStyle(document.querySelector('.fx-menu.open')).color")
        got["neon"] = await b.js("(()=>{const p=document.createElement('span');p.style.color='var(--neon)';document.body.appendChild(p);"
                                 "const c=getComputedStyle(p).color;p.remove();return c})()")

    asyncio.run(desktop.with_browser("online", "", check, DRIVE))
    assert got["menu"] == got["neon"], (theme, "the open menu is not in the theme's --neon", got)
