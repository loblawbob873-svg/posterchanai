"""Files' Edit menu has Cut, Copy, Paste, Delete, Rename and Move, and each one DOES it -- in the SHIPPED client.

Reported 2026-10-27, the day after the Explorer rework: "Files -> You forgot to add delete actions. you maybe missing
other actions like copy and paste or move also!" The Edit menu listed whatever hidden buttons the pane happened to draw,
so the drive had no Cut / Copy / Paste at all and This Computer hid all three, and its Delete, one level down behind
"Selected items…". Every press here is a real mouse event (music_art_fixture.press) or a real key event, over the
60-file drive fixture with a folder "Work" to move and copy into, at a phone and a desktop size:

  * Edit lists Cut, Copy, Paste, Delete, Rename, Move to…, Select all, Select none -- in that order -- and with nothing
    selected the ones that need a selection are disabled;
  * Cut, then (in Work) Paste MOVES the file there; Ctrl+X / Ctrl+V does the same;
  * a file's ⋯ -> Copy, then Paste, makes a SECOND file in that folder (named "(copy)" beside the original), read and
    stored again -- the original stays where it was;
  * Delete (from Edit, and from the Delete key) asks first, and Cancel keeps the file.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client import test_files_explorer_menus_full_app as T
from tests.client.test_files_phone_layout_full_app import DRIVE

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")

# The drive's blobs answer with bytes (a copy reads them), and a folder "Work" exists to paste into.
BYTES = r"""
const _bytesFetch = window.fetch;
window.fetch = async function(url, opts){
  const u = String(url);
  if(/\/[0-9a-f]{64}\.(jpg|pdf)$/.test(u) && !(opts && opts.method && opts.method !== 'GET'))
    return new Response(new Blob([new Uint8Array([0xff,0xd8,0xff,1,2,3])], {type:'image/jpeg'}), {status:200});
  return _bytesFetch.apply(this, arguments);
};
"""
EDIT = ["Cut", "Copy", "Paste", "Delete", "Rename", "Move to…", "Select all", "Select none"]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _key(b, key, code, vk, ctrl=False):
    mods = 2 if ctrl else 0
    for t in ("keyDown", "keyUp"):
        await b.call("Input.dispatchKeyEvent", {"type": t, "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                                "modifiers": mods})


async def _home(b):
    # Up, or (All is the top: Up is disabled there) Back -- what a person presses either way.
    sel = await b.js("(()=>{for(const s of ['#fx-up','#fx-back']){const u=document.querySelector(s);"
                     " if(u && !u.disabled) return s;} return ''})()")
    assert sel, "no way back to the folder list"
    await A.press(b, sel)
    await b.until("document.querySelectorAll('.fx-home-tile').length>=2")


async def _into(b, folder):
    await _home(b)
    await A.press(b, f".fx-home-tile[data-folder='{folder}']")
    await b.until("!!document.querySelector('#bl-grid')")
    await asyncio.sleep(.4)


async def _select(b, sha):
    await A.press(b, f"#bl-grid .file-card[data-sha='{sha}'] .selbox")
    await b.until(f"/1 selected/.test(document.getElementById('fx-status').textContent)")


def _base(m):
    return [x["t"].split(" (")[0].split(" 1 ")[0] for x in m]


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_cut_copy_paste_delete_and_move_from_the_menus_and_the_keys(w, h):
    got = {}

    async def check(b):
        await T._open_files(b, w, h)
        await b.js("__PC.filesIdx().addFolder('Work'); __PC.switchView('blossom'); 1")
        await b.until("!!document.querySelector(\".fx-home-tile[data-folder='Work']\")")
        await T._all_files(b)
        shas = await b.js("[...document.querySelectorAll('#bl-grid .file-card[data-sha]')].map(c=>c.dataset.sha)")
        a, c, d = shas[0], shas[2], shas[3]
        got["edit0"] = await T._menu(b, "edit")
        await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); 1")
        # Cut -> Work -> Paste moves it
        await _select(b, a)
        await T._menu(b, "edit")
        await T._pick(b, "Cut (1)")
        await _into(b, "Work")
        got["edit_paste"] = [x["t"] for x in await T._menu(b, "edit")]
        await T._pick(b, "Paste 1 file (move)")
        await b.until(f"__PC.filesIdx().folderOf('{a}')==='Work'")
        await b.until(f"!!document.querySelector(\"#bl-grid .file-card[data-sha='{a}']\")")
        got["moved"] = await b.js(f"__PC.filesIdx().folderOf('{a}')")
        # its ⋯ -> Copy, then Paste: a second file in Work, the original stays
        before = await b.js("Object.keys(__PC.filesIdx().data.files).length")
        await A.press(b, f"#bl-grid .file-card[data-sha='{a}'] .fx-rowmore")
        await b.until("!!document.querySelector('.menu-pop')")
        got["row"] = [x["t"] for x in await b.js(T.MENU)]
        await T._pick(b, "Copy")
        await T._menu(b, "edit")
        await T._pick(b, "Paste 1 file (copy)")
        await b.until(f"Object.keys(__PC.filesIdx().data.files).length>{before}")
        got["copies"] = await b.js("Object.entries(__PC.filesIdx().data.files).filter(([s,m])=>m.folder==='Work').map(([s,m])=>[s,m.name])")
        got["orig_still"] = await b.js(f"__PC.filesIdx().folderOf('{a}')")
        # Ctrl+X in All, Ctrl+V in Work
        await _home(b)
        await T._all_files(b)
        await _select(b, c)
        await _key(b, "x", "KeyX", 88, ctrl=True)
        await _into(b, "Work")
        await _key(b, "v", "KeyV", 86, ctrl=True)
        await b.until(f"__PC.filesIdx().folderOf('{c}')==='Work'")
        # Delete asks first; Cancel keeps it -- from the menu and from the Delete key
        await _home(b)
        await T._all_files(b)
        await _select(b, d)
        await T._menu(b, "edit")
        await T._pick(b, "Delete (1)")
        await b.until("!!document.querySelector('.uiconfirm')")
        got["confirm"] = await b.js("document.querySelector('.uiconfirm-msg').textContent")
        await A.press(b, ".uiconfirm [data-uc='0']")
        await b.until("!document.querySelector('.uiconfirm')")
        await _key(b, "Delete", "Delete", 46)
        await b.until("!!document.querySelector('.uiconfirm')")
        got["confirm_key"] = await b.js("document.querySelector('.uiconfirm-msg').textContent")
        await A.press(b, ".uiconfirm [data-uc='0']")
        got["d_kept"] = await b.js(f"!!document.querySelector(\"#bl-grid .file-card[data-sha='{d}']\")")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")
        got["a"] = a

    asyncio.run(desktop.with_browser("online", "", check, DRIVE + BYTES))
    shape = (w, h)
    e0 = got["edit0"]
    assert _base(e0) == EDIT, (shape, "the Edit menu", [x["t"] for x in e0])
    dis = {x["t"].split(" (")[0]: x["d"] for x in e0}
    assert dis["Cut"] and dis["Copy"] and dis["Delete"] and dis["Paste"] and not dis["Select all"], \
        (shape, "with nothing selected, the selection actions must be disabled", dis)
    assert "Paste 1 file (move)" in got["edit_paste"], (shape, got["edit_paste"])
    assert got["moved"] == "Work", (shape, "Cut + Paste did not move the file")
    assert "Cut" in got["row"] and "Copy" in got["row"] and "Delete" in got["row"], (shape, "a file's ⋯", got["row"])
    names = [n for s, n in got["copies"] if s != got["a"]]
    assert any("(copy)" in (n or "") for n in names), (shape, "Copy + Paste made no second file in Work", got["copies"])
    assert got["orig_still"] == "Work", (shape, "the original moved or vanished when it was copied")
    assert got["confirm"].startswith("Delete 1 file?") and got["confirm_key"].startswith("Delete 1 file?"), \
        (shape, got["confirm"], got["confirm_key"])
    assert got["d_kept"], (shape, "Cancel deleted the file")
    assert not got["errors"], (shape, got["errors"])


# This Computer, through a stub of the desktop's host bridge that RECORDS what it is asked to do.
HOST = r'''
window.__hostCalls=[];
window.pcHost={roots:async()=>[{path:'/home/test',name:'Home',kind:'home'}],
 list:async(path)=>({path,parent:'/home',entries:[{name:'vmtest',path:'/home/test/vmtest',dir:true,size:0,mtime:100},
   {name:'notes.txt',path:'/home/test/notes.txt',dir:false,size:48000,mtime:100}]}),
 read:async()=>new TextEncoder().encode('x'), open:async()=>({ok:true}),
 transfer:async(paths,dest,move)=>{__hostCalls.push(['transfer',paths,dest,!!move]);return {ok:true};},
 trash:async(p)=>{__hostCalls.push(['trash',p]);return {ok:true};},
 rename:async(p,to)=>{__hostCalls.push(['rename',p,to]);return {ok:true};}};
window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;
'''


@CHROME
def test_this_computer_has_the_same_edit_menu_and_it_reaches_the_machine():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('[data-host=\"1\"]')")
        await b.js("document.querySelector('[data-host=\"1\"]').click()")
        await b.until("!!document.querySelector('[data-p=\"/home/test/notes.txt\"]')")
        got["none"] = await T._menu(b, "edit")
        await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); 1")
        await A.press(b, "[data-p=\"/home/test/notes.txt\"] .hf-select")
        await asyncio.sleep(.3)
        got["one"] = [x["t"] for x in await T._menu(b, "edit")]
        await T._pick(b, "Copy")
        await asyncio.sleep(.3)
        await T._menu(b, "edit")
        paste = await b.js("[...document.querySelectorAll('.menu-pop button')].map(e=>e.textContent.trim()).find(t=>/^Paste/.test(t))||''")
        got["paste_label"] = paste
        await T._pick(b, paste)
        await b.until("__hostCalls.some(c=>c[0]==='transfer')")
        # Delete from Edit: asks, then the machine's own bin
        await b.until("!!document.querySelector('[data-p=\"/home/test/notes.txt\"]')")
        await A.press(b, "[data-p=\"/home/test/notes.txt\"] .hf-select")
        await asyncio.sleep(.3)
        await T._menu(b, "edit")
        await T._pick(b, "Delete (1)")
        await b.until("!!document.querySelector('.uiconfirm')")
        await A.press(b, ".uiconfirm [data-uc='1']")
        await b.until("__hostCalls.some(c=>c[0]==='trash')")
        got["calls"] = await b.js("__hostCalls")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "?pcwin=blossom", check, HOST))
    assert _base(got["none"])[:4] == ["Cut", "Copy", "Paste", "Delete"], [x["t"] for x in got["none"]]
    assert "Selected items…" not in [x["t"] for x in got["none"]], "the actions are hidden behind a sub-menu again"
    one = got["one"]
    assert "Cut" in one and "Copy" in one and "Delete (1)" in one and "Rename" in one and "Move to…" in one, one
    assert got["paste_label"].startswith("Paste"), got["paste_label"]
    calls = got["calls"]
    assert ["transfer", ["/home/test/notes.txt"], "/home/test", False] in calls, calls
    assert ["trash", "/home/test/notes.txt"] in calls, calls
    assert not got["errors"], got["errors"]
