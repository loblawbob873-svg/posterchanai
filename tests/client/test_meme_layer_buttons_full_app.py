"""Meme Builder: a photo layer's own buttons, pressed in the real client and checked against the SAVED project
(what a reload or an export would use, not what the screen happens to show):

  * ↔ / ↕ flip it; ⟲ puts rotation and flips back; Duplicate makes a second layer; Delete removes one, and
    ↶ / ↷ undo and redo that;
  * "Match canvas" sizes the canvas to the photo (720x640 here); zoom + zooms the stage in;
  * Arrange runs the timeline's re-sequence without changing a single still layer.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_meme_drawing_full_app import CHROME, _MEDIA, _open, media  # noqa: F401 (fixture)

PROJ = "JSON.parse(localStorage.getItem('pc_meme_project')||'{}')"
LAYERS = PROJ + ".layers"


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_photo_layer_buttons(media):
    async def check(b):
        await b.js("PCMeme.reset()")
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
        await b.until("!!document.getElementById('mb-fliph')")
        click = lambda i: b.js(f"document.getElementById({i!r}).click(); 1")

        await click('mb-fliph'); await b.until(f"{LAYERS}[0].flipH===true")
        await click('mb-flipv'); await b.until(f"{LAYERS}[0].flipV===true")
        await click('mb-rot0'); await b.until(f"!{LAYERS}[0].flipH && !{LAYERS}[0].flipV && !({LAYERS}[0].rotate)")

        # Match canvas waits for the photo to have loaded.
        await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!(i&&i.naturalWidth)})()")
        await click('mb-canvas-match')
        await b.until(f"{PROJ}.w===720 && {PROJ}.h===640")

        z0 = await b.js("(document.getElementById('mb-zoomlbl')||document.querySelector('.mb-zoom-val')||{}).textContent||getComputedStyle(document.querySelector('.mb-stage')).transform")
        await click('mb-zoomin')
        await b.until(f"((document.getElementById('mb-zoomlbl')||document.querySelector('.mb-zoom-val')||{{}}).textContent||getComputedStyle(document.querySelector('.mb-stage')).transform)!=={json.dumps(z0)}")

        await b.until("!!document.getElementById('mb-dup')")
        await click('mb-dup'); await b.until(f"{LAYERS}.length===2")
        await b.until("!!document.getElementById('mb-del')")
        await click('mb-del'); await b.until(f"{LAYERS}.length===1")
        await click('mb-undo'); await b.until(f"{LAYERS}.length===2")
        await click('mb-redo'); await b.until(f"{LAYERS}.length===1")

        before = await b.js(f"JSON.stringify({LAYERS}.map(l=>[l.id,l.x,l.y,l.w,l.h]))")
        await b.until("!!document.getElementById('mb-arrange')")
        await click('mb-arrange')
        await asyncio.sleep(.3)
        assert await b.js(f"JSON.stringify({LAYERS}.map(l=>[l.id,l.x,l.y,l.w,l.h]))") == before, "Arrange moved a still layer"
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', media, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_project_menu_rename_clear_new_and_save(media):
    """The project menu: Rename names it; "Remove every layer" (confirmed) empties it and ↶ brings the layers back;
    "Start a new project" (confirmed) resets layers AND name; "Save to Files" uploads the project file."""
    async def check(b):
        await b.js("PCMeme.reset()")
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
        await b.until("!!document.getElementById('mb-project')")

        async def menu(item):
            await b.js("document.getElementById('mb-project').click(); 1")
            await b.until(f"!!document.getElementById({item!r})")
            await b.js(f"document.getElementById({item!r}).click(); 1")

        async def answer(text=None):
            await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
            if text is not None:
                await b.js(f"document.querySelector('.uiprompt-in').value={text!r}; 1")
            await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")

        await menu('mbp-name'); await answer('dog on a skateboard')
        await b.until(f"{PROJ}.name==='dog on a skateboard'")
        await menu('mbp-clear'); await answer()
        await b.until(f"{LAYERS}.length===0")
        await b.js("document.getElementById('mb-undo').click(); 1")
        await b.until(f"{LAYERS}.length===1")
        await menu('mbp-save')
        await b.until("/saved|Saved/.test(document.body.innerText)")
        await menu('mbp-new'); await answer()
        await b.until(f"{LAYERS}.length===0 && !{PROJ}.name")
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', media, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_resize_canvas_size_and_export_dialogs(media):
    """Resize layer: "Original" fills in the picture's own pixels (720x640), "Canvas" the canvas size, and Cancel
    changes nothing. Canvas size and Export each close on Cancel without touching the project."""
    async def check(b):
        await b.js("PCMeme.reset()")
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
        await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!(i&&i.naturalWidth)})()")
        before = await b.js(f"JSON.stringify({PROJ})")
        lid = await b.js(f"{LAYERS}[0].id")
        await b.js(f"PCMeme.openLayerResize({lid!r}); 1")
        await b.until("!!document.getElementById('rz-orig')")
        await b.js("document.getElementById('rz-orig').click(); 1")
        assert await b.js("[document.getElementById('rz-w').value,document.getElementById('rz-h').value]") == ["720", "640"]
        await b.js("document.getElementById('rz-canvas').click(); 1")
        canvas = await b.js(f"[String({PROJ}.w),String({PROJ}.h)]")
        assert await b.js("[document.getElementById('rz-w').value,document.getElementById('rz-h').value]") == canvas
        await b.js("document.getElementById('rz-cancel').click(); 1")
        await b.until("!document.getElementById('rz-cancel')")
        await b.js("PCMeme.openCanvasSize(); 1")
        await b.until("!!document.getElementById('cs-cancel')")
        await b.js("document.getElementById('cs-cancel').click(); 1")
        await b.until("!document.getElementById('cs-cancel')")
        await b.js("PCMeme.openExport(); 1")
        await b.until("!!document.getElementById('ex-cancel')")
        await b.js("document.getElementById('ex-cancel').click(); 1")
        await b.until("!document.getElementById('ex-cancel')")
        assert await b.js(f"JSON.stringify({PROJ})") == before, "a cancelled dialog changed the project"
    asyncio.run(_open(1280, 800, False, '', media, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_the_finished_render_s_buttons(media):
    """After Render: Download saves the file; "Upload & copy link" uploads it and shows the link; "Use as a layer"
    puts the render back on the timeline; "Post to Nostr" opens the composer with the link; and, when the media
    came off a post, "Reply" publishes a reply to THAT post carrying the link."""
    async def check(b):
        await b.js("PCMeme.reset(); window.__publishOK=true; window.__sentR=[]; const __s=WebSocket.prototype.send;"
                   "WebSocket.prototype.send=function(r){try{const m=JSON.parse(r);if(m[0]==='EVENT')__sentR.push(m[1]);}catch(_){}return __s.call(this,r)}; 1")
        post = {"id": "e" * 64, "pk": "f" * 64}
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png',{json.dumps(post)})")
        await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!(i&&i.naturalWidth)})()")
        await b.js("(()=>{const s=document.getElementById('mb-fmt');s.value='png';s.dispatchEvent(new Event('change',{bubbles:true}))})()")
        await b.until("!!document.getElementById('mb-render')")
        await b.js("document.getElementById('mb-render').click(); 1")
        for _ in range(4):                                   # a render takes a moment
            try:
                await b.until("!!document.getElementById('mb-dl')")
                break
            except AssertionError:
                pass

        await b.js("document.getElementById('mb-dl').click(); 1")
        await b.until("/saved|shared/.test(document.getElementById('mb-dl').textContent)")
        await b.js("document.getElementById('mb-copy').click(); 1")
        await b.until("!!document.querySelector('#mb-reslink a')")
        link = await b.js("document.querySelector('#mb-reslink a').getAttribute('href')")
        await b.until("!!document.getElementById('mb-reply')")
        await b.js("document.getElementById('mb-reply').click(); 1")
        await b.until("__sentR.some(e=>e.kind===1&&e.tags.some(t=>t[0]==='e'&&t[1]==='" + post["id"] + "'))")
        reply = await b.js("__sentR.find(e=>e.kind===1)")
        assert reply["content"].startswith("http"), reply
        await b.js("document.getElementById('mb-post').click(); 1")
        await b.until("!!document.querySelector('#cmp-send')")
        text = await b.js("document.querySelector('#cmp-send').closest('.modal, .modal-card, [role=dialog], body').querySelector('textarea').value")
        assert text.startswith("http"), text
        assert link.startswith("http")
        # Last, because it redraws the builder (the result panel goes with it).
        await b.js("__PC.closeModal(); 1")
        n = await b.js(f"{LAYERS}.length")
        await b.js("document.getElementById('mb-again').click(); 1")
        await b.until(f"{LAYERS}.length==={n + 1}")
    asyncio.run(_open(1280, 800, False, '', media, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_the_add_sheet_sticker_layout_and_a_file_from_this_device(media):
    """＋ Add: Sticker → an emoji becomes its own layer; Ready-made layout → the layout's layers are added (and ↶
    takes them away again); From this device → the picked picture is uploaded and lands as a layer."""
    async def check(b):
        await b.js("PCMeme.reset(); window.__fileInputs=[]; const __c=HTMLInputElement.prototype.click;"
                   "HTMLInputElement.prototype.click=function(){if(this.type==='file'){__fileInputs.push(this);return;}return __c.call(this);}; 1")
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
        await b.until(f"{LAYERS}.length===1 && !!document.getElementById('mb-add-media')")

        async def add(item):
            await b.js("document.getElementById('mb-add-media').click(); 1")
            await b.until(f"!!document.getElementById({item!r})")
            await b.js(f"document.getElementById({item!r}).click(); 1")

        await add('mba-sticker')
        await b.until("!!document.querySelector('.emoji-pop .ep-grid button[data-e]')")
        await b.js("document.querySelector('.emoji-pop .ep-grid button[data-e]').dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true})); 1")
        await b.until(f"{LAYERS}.length===2")

        await add('mba-tpl')
        await b.until("!!document.querySelector('.mb-tplrow:not([disabled])')")
        await b.js("document.querySelector('.mb-tplrow:not([disabled])').click(); 1")
        await b.until(f"{LAYERS}.length>2")
        await b.js("document.getElementById('mb-undo').click(); 1")
        await b.until(f"{LAYERS}.length===2")

        await add('mbm-local')
        await b.until("__fileInputs.length===1")
        png = await b.js("(async()=>{const r=await fetch(" + json.dumps(_MEDIA[0].base + '/photo.png') + ");return Array.from(new Uint8Array(await r.arrayBuffer()))})()")
        await b.js("(()=>{const i=__fileInputs[0];const dt=new DataTransfer();dt.items.add(new File([new Uint8Array(" + json.dumps(png) + ")],'mine.png',{type:'image/png'}));"
                   "i.files=dt.files;i.dispatchEvent(new Event('change'));})(); 1")
        await b.until(f"{LAYERS}.length===3")
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', media, check))
