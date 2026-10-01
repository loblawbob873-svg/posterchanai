"""Opening an image on Android: Share and Save do two different things.

Reported: "opening an image on android: first two buttons share, do the same action". In the APK the
first button (Copy) has to be the share sheet -- a WebView cannot put an image on the clipboard -- and
Save went through saveBlobAs, which in the APK is ALSO the share sheet. Now the first button says Share,
and Save writes the picture into the phone's gallery through the MediaSave plugin; a phone that cannot
(Android 9 and older) shows no second share button. A browser keeps Copy image + Save image.

Driven in the real bundle with the native bridge stubbed in after boot.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NATIVE = r"""(gallery=>{window.__saved=[];window.__shared=[];
  window.Capacitor={isNativePlatform:()=>true,Plugins:{
    MediaSave:{available:async()=>({ok:gallery}),save:async o=>{__saved.push({mime:o.mime,name:o.name,len:(o.data||'').length});return {ok:true,where:'Pictures/PosterChan/'+o.name};}},
    Share:{share:async o=>{__shared.push(o);return {};}},
    Filesystem:{writeFile:async o=>({uri:'file:///cache/'+o.path})}}};})"""

BAR = "[...document.querySelectorAll('.lightbox .lb-btn')].filter(b=>b.style.display!=='none').map(b=>b.title)"


async def _open(b, native=None):
    await desktop.login(b)
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
    if native is not None:
        await b.js(NATIVE + "(%s)" % ("true" if native else "false"))
    await b.js("__PC.openLightbox('/static/icon-512.png','image')")
    await b.until("!!document.querySelector('.lightbox img')")
    await asyncio.sleep(.4)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_android_share_shares_and_save_saves_to_the_gallery():
    async def check(b):
        await _open(b, native=True)
        titles = await b.js(BAR)
        assert titles[:2] == ["Share  (C)", "Save to gallery  (S)"], titles
        await b.js("[...document.querySelectorAll('.lightbox .lb-btn')].find(x=>x.title.startsWith('Save')).click()")
        await b.until("__saved.length===1")
        saved = await b.js("__saved[0]")
        assert saved["mime"] == "image/png" and saved["name"].endswith(".png") and saved["len"] > 100, saved
        assert await b.js("__shared.length") == 0, "Save opened the share sheet again"
        await b.js("[...document.querySelectorAll('.lightbox .lb-btn')].find(x=>x.title.startsWith('Share')).click()")
        await b.until("__shared.length===1")
        assert await b.js("__saved.length") == 1
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_phone_that_cannot_save_to_the_gallery_shows_no_second_share():
    async def check(b):
        await _open(b, native=False)
        titles = await b.js(BAR)
        assert "Share  (C)" in titles and not any(t.startswith(("Save to gallery", "Save image")) for t in titles), titles
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_browser_keeps_copy_and_save():
    async def check(b):
        await _open(b)
        titles = await b.js(BAR)
        assert titles[:2] == ["Copy image  (C)", "Save image  (S)"], titles
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_a_phone_the_buttons_say_what_they_do_and_saving_is_seen():
    """"android users see nothing when saving" / "we need a save to device feature for android when
    opening images": the Save button was there, but as a bare ⤓ whose name lived in a hover tooltip a
    phone never shows -- and the "Saved to …" message it printed was drawn BEHIND the image (.lightbox is
    z-index 100000, toasts were 500). Each button must carry a visible name, and after Save the
    confirmation must be the thing on top where it is drawn."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await _open(b, native=True)
        labels = await b.js("""[...document.querySelectorAll('.lightbox .lb-btn')].filter(x=>x.style.display!=='none')
          .map(x=>{const l=x.querySelector('.lb-lbl');const r=x.getBoundingClientRect();
            return {name:l&&getComputedStyle(l).display!=='none'?l.textContent:'',fits:r.right<=innerWidth+1&&r.left>=-1}})""")
        assert [l["name"] for l in labels] == ["Share", "Save", "Files", "Close"], labels
        assert all(l["fits"] for l in labels), labels
        await b.js("[...document.querySelectorAll('.lightbox .lb-btn')].find(x=>x.title.startsWith('Save')).click()")
        await b.until("__saved.length===1")
        await b.until("[...document.querySelectorAll('#toast-root .toast')].some(t=>/Saved to/.test(t.textContent))")
        # #toast-root is pointer-events:none (a message must not eat taps), and elementFromPoint looks
        # straight through that -- so hit-test with it switched on for the measurement only.
        on_top = await b.js("""(()=>{const root=document.querySelector('#toast-root');const t=[...root.querySelectorAll('.toast')].find(t=>/Saved to/.test(t.textContent));
          root.style.pointerEvents='auto';t.style.pointerEvents='auto';
          const r=t.getBoundingClientRect();const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
          root.style.pointerEvents='';t.style.pointerEvents='';
          const bar=document.querySelector('.lightbox .lb-bar').getBoundingClientRect();
          return {hit:hit&&(hit.className||hit.tagName),mine:!!hit&&(hit===t||t.contains(hit)),clearOfBar:r.bottom<=bar.top+1}})()""")
        assert on_top["mine"], ("the save confirmation is hidden behind the image", on_top)
        assert on_top["clearOfBar"], ("the save confirmation covers the viewer's buttons", on_top, await b.js("(()=>{const r=document.querySelector('.lightbox .lb-bar').getBoundingClientRect();const t=document.querySelector('#toast-root').getBoundingClientRect();return {bar:[r.top,r.bottom],toast:[t.top,t.bottom],ih:innerHeight}})()"))
    asyncio.run(desktop.with_browser("online", "", check, ""))
