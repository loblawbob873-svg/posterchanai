"""Settings -> Appearance: your colours, a section's own colours and a wallpaper, for the WHOLE app.

Git issue "Concord Theme match with app theme" (2026-10-21): a look made in Communities only existed there,
reverted the moment you left, and the editor was not in Settings. Real client: the Appearance tab sets an accent
and background that every page takes; Communities gets its own background without touching Home; a wallpaper at
a chosen position becomes the page's own background; a reload paints all of it before the app has started; and
Concord's picker hands its colours to the whole app. Phone and desktop width.
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The ordinary app, not the PosterChanOS desktop (whose own wallpaper is the desktop's, by design).
NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
ROOTVAR = "getComputedStyle(document.documentElement).getPropertyValue('{}').trim()"
BODYBG = "getComputedStyle(document.body).backgroundImage"


async def _color(b, sel, value, sec=None):
    q = f"#us-look input[type=color][data-look='{sel}']" + (f"[data-sec='{sec}']" if sec else ":not([data-sec])")
    await b.js(f"(()=>{{const i=document.querySelector(\"{q}\");i.value='{value}';"
               "i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}))})()")
    await asyncio.sleep(.15)


async def _appearance(b, width, mobile):
    await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': mobile})
    await desktop.login(b)
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=\"appearance\"]') && !!document.querySelector('#us-look .look')")
    await b.js("document.querySelector('.us-tab[data-tab=\"appearance\"]').click()")
    got = {"pane_visible": await b.js("!!document.querySelector('#us-look').offsetParent"),
           "theme_select_here": await b.js("!!document.querySelector('[data-pane=\"appearance\"] #us-theme')")}
    await _color(b, 'accent', '#ff2a2a')
    await _color(b, 'bg', '#1a0f05')
    await _color(b, 'bg', '#e6f0ff', sec='concord')
    await b.js("(()=>{const u=document.querySelector('#look-url');u.value='/static/signon-nostr-city.webp';u.dispatchEvent(new Event('change',{bubbles:true}))})()")
    await asyncio.sleep(.2)
    await b.js("document.querySelector('#us-look [data-pos=\"right bottom\"]').click()")
    await asyncio.sleep(.2)
    got["no_overflow"] = await b.js("document.documentElement.scrollWidth<=innerWidth+1 && [...document.querySelectorAll('#us-look *')].every(e=>e.getBoundingClientRect().right<=innerWidth+1)")
    if os.environ.get('PC_SHOTS'):
        Path(os.environ['PC_SHOTS']).mkdir(parents=True, exist_ok=True)
        shot = await b.call('Page.captureScreenshot', {'format': 'png'})
        Path(os.environ['PC_SHOTS'], f'appearance-{width}.png').write_bytes(base64.b64decode(shot['data']))
    await b.js("__PC.switchView('home')")
    await asyncio.sleep(.3)
    got["home"] = {"neon": await b.js(ROOTVAR.format('--neon')), "bg": await b.js(ROOTVAR.format('--bg')),
                   "sec": await b.js("document.documentElement.getAttribute('data-pcsec')"), "body": await b.js(BODYBG)}
    await b.js("__PC.switchView('concord')")
    await asyncio.sleep(.5)
    got["concord"] = {"bg": await b.js(ROOTVAR.format('--bg')), "sec": await b.js("document.documentElement.getAttribute('data-pcsec')")}
    # A reload: painted from the saved copy by the <head> script, before app.js has even run.
    await b.call('Page.addScriptToEvaluateOnNewDocument', {'source':
        "document.addEventListener('readystatechange',()=>{if(document.readyState==='interactive'&&!window.__early)"
        "window.__early=getComputedStyle(document.documentElement).getPropertyValue('--neon').trim()+'|'+(window.__PC?'app':'noapp');});"})
    await b.call('Page.reload')
    await b.until("!!window.__PC && (!!__PC.me() || document.body.classList.contains('guest'))")
    got["early"] = await b.js("window.__early||''")
    return got


def _check(got):
    assert got["pane_visible"] and got["theme_select_here"], got
    assert got["no_overflow"], ("the Appearance pane runs off the screen", got)
    assert got["home"]["neon"] == "#ff2a2a" and got["home"]["bg"] == "#1a0f05", ("your colours did not reach Home", got)
    assert got["home"]["sec"] == "social"
    assert "signon-nostr-city.webp" in got["home"]["body"], ("the wallpaper is not the page background", got)
    assert got["concord"]["sec"] == "concord" and got["concord"]["bg"] == "#e6f0ff", ("Communities' own colour", got)
    assert got["early"].startswith("#ff2a2a|"), ("the look was not painted before the app started", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,mobile", [(390, True), (1280, False)])
def test_appearance_reaches_every_page(width, mobile):
    got = {}

    async def check(b):
        got.update(await _appearance(b, width, mobile))
    asyncio.run(desktop.with_browser("online", "", check, NO_OS))
    _check(got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_concords_colours_can_become_the_whole_apps():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('concord')")
        await b.until("!!window.PCConcordTheme")
        await b.js("PCConcordTheme.set({accent:'#22cc55'});PCConcordTheme.openPicker(__PC)")
        await b.until("!!document.querySelector('#cc-theme-whole')")
        await b.js("document.querySelector('#cc-theme-whole').click()")
        await asyncio.sleep(.2)
        await b.js("__PC.closeModal&&__PC.closeModal();__PC.switchView('home')")
        await asyncio.sleep(.3)
        got["neon"] = await b.js(ROOTVAR.format('--neon'))
        got["cc"] = await b.js("PCConcordTheme.get()")
    asyncio.run(desktop.with_browser("online", "", check, NO_OS))
    assert got["neon"] == "#22cc55", ("Concord's colour did not reach the rest of the app", got)
    assert got["cc"]["theme"] == "app" and not got["cc"]["accent"], ("Concord should now follow the app's look", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_remove_wallpaper_and_reset_appearance():
    """Remove takes the wallpaper off the page; Reset puts the theme's own colours back."""
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('#us-look .look')")
        await b.js("document.querySelector('.us-tab[data-tab=\"appearance\"]').click()")
        theme = await b.js(ROOTVAR.format('--neon'))
        await _color(b, 'accent', '#ff2a2a')
        await b.js("(()=>{const u=document.querySelector('#look-url');u.value='/static/signon-nostr-city.webp';u.dispatchEvent(new Event('change',{bubbles:true}))})()")
        await b.until(f"/signon-nostr-city/.test({BODYBG}) && !document.querySelector('#look-wall-off').disabled")
        await b.js("document.querySelector('#look-wall-off').click()")
        await b.until(f"!/signon-nostr-city/.test({BODYBG}) && document.querySelector('#look-wall-off').disabled")
        assert await b.js(ROOTVAR.format('--neon')) == '#ff2a2a', 'removing the wallpaper also dropped the colours'
        await b.js("document.querySelector('#look-reset').click()")
        await b.until(f"{ROOTVAR.format('--neon')}==={theme!r} && document.querySelector('#look-reset').disabled")

    asyncio.run(desktop.with_browser("online", "", check, NO_OS))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_each_section_has_a_text_colour_and_the_menus_keep_their_own():
    """Git issues "Timeline & Social Color" and "Menu Separate Color Class" (Vyram, 2026-10-27): the timeline needs a
    TEXT colour of its own beside background and hashtags & icons, and the menus (bottom bar, More sheet, sidebar)
    took each section's accent as you moved between sections -- they get their own colours that hold everywhere."""
    got = {}
    VAR = "getComputedStyle(document.querySelector('{}')).getPropertyValue('{}').trim()"

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 900, 'deviceScaleFactor': 1, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('.us-tab[data-tab=\"appearance\"]') && !!document.querySelector('#us-look .look')")
        await b.js("document.querySelector('.us-tab[data-tab=\"appearance\"]').click()")
        got["social_labels"] = await b.js("[...document.querySelectorAll('#us-look input[data-sec=\"social\"]')].map(i=>i.closest('label').textContent.trim())")
        await _color(b, 'accent', '#00b06f', sec='social')
        await _color(b, 'text', '#ffffff', sec='social')
        await _color(b, 'accent', '#ff0000', sec='concord')
        await _color(b, 'accent', '#3366ff', sec='menu')
        await _color(b, 'text', '#ffcc00', sec='menu')
        out = {}
        for view in ('home', 'concord'):
            await b.js(f"__PC.switchView('{view}')")
            await asyncio.sleep(.4)
            out[view] = {"text": await b.js(ROOTVAR.format('--text')), "neon": await b.js(ROOTVAR.format('--neon')),
                         "nav_neon": await b.js(VAR.format('.mobilenav', '--neon')),
                         "nav_text": await b.js(VAR.format('.mobilenav', '--text'))}
        await b.js("document.querySelector('.mobilenav [data-v=\"more\"], .mobilenav .more, .mobilenav button:last-child').click()")
        await b.until("!!document.querySelector('.modal .more-grid')")
        out["more"] = {"neon": await b.js(VAR.format('.modal:has(.more-grid)', '--neon')),
                       "label": await b.js("getComputedStyle(document.querySelector('.more-item')).color")}
        got["views"] = out

    asyncio.run(desktop.with_browser("online", "", check, NO_OS))
    assert got["social_labels"] == ["Hashtags & icons", "Background", "Text"], got["social_labels"]
    v = got["views"]
    assert v["home"]["text"] == "#ffffff" and v["home"]["neon"] == "#00b06f", ("the timeline's own text / hashtags", v)
    assert v["concord"]["neon"] == "#ff0000", ("Communities' own accent", v)
    for view in ("home", "concord"):
        assert v[view]["nav_neon"] == "#3366ff" and v[view]["nav_text"] == "#ffcc00", \
            (view, "the menu took the section's colours instead of its own", v)
    assert v["more"]["neon"] == "#3366ff" and v["more"]["label"] == "rgb(255, 204, 0)", ("the More sheet", v)
