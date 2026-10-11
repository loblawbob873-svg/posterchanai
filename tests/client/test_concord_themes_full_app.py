"""Concord themes: Cyberpunk by default, today's look kept as "Classic", a custom accent, synced to the account.

Operator: "we need customizable themes for Concord; the current look should be [kept as] the generic one,
and a cool cyberpunk theme as the default." Driven through the real bundled client, real login and the
real Concord workspace (only the relay and the CORD reader are fixtures):

* a FRESH account sees Cyberpunk -- asserted on COMPUTED colours of the conversation, the channel pane,
  the message text and the active community, each equal to the theme variable it should read, and
  pinned to Cyberpunk's own values (a class name proves nothing);
* choosing "Classic" in the picker gives back the computed colours the workspace had before themes
  existed (CLASSIC below was captured from the pre-theme code, not from the theme definitions);
* a custom accent recolours the active channel, the active community and the Send button;
* the choice survives a reload (painted before the first network read) and a SECOND DEVICE -- a fresh
  browser profile, same key -- comes up in it from the account's synced `pcai:client-prefs`;
* message text keeps WCAG AA contrast (>= 4.5:1) against what is actually behind it, in every preset;
* reduced-motion users get no animated glow.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PRESETS = ["cyberpunk", "classic", "synthwave", "monero", "light", "oled", "app"]

# Computed before the theme system existed (default app theme, 1280px, a room open).
CLASSIC = {
    "conversation": "rgb(32, 35, 45)",
    "channels": "rgb(25, 28, 37)",
    "rail": "rgb(17, 19, 26)",
    "activeChannelBg": "rgb(42, 46, 58)",
    "activeChannelFg": "rgb(255, 255, 255)",
    "compose": "rgb(43, 47, 59)",
    "input": "rgb(242, 243, 246)",
    "identity": "rgb(21, 24, 33)",
    "body": "rgb(237, 238, 250)",
    "activeServer": "rgb(60, 232, 255)",
    "section": "rgb(146, 152, 168)",
}
CYBERPUNK = {"conversation": "rgb(14, 12, 31)", "activeServer": "rgb(0, 240, 255)"}

# Record client-prefs publishes; answer a 30078 REQ from a seeded store, filtered by d-tag.
PREFS = r"""
window.__seedPrefs=window.__seedPrefs||[];
(()=>{const S=window.WebSocket,send=S.prototype.send;
 S.prototype.send=function(raw){let m;try{m=JSON.parse(raw);}catch(_){return send.call(this,raw);}
  if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>(f.kinds||[]).includes(30078)&&(f['#d']||[]).includes('pcai:client-prefs'))){
    setTimeout(()=>{for(const ev of window.__seedPrefs)this.fire('message',['EVENT',m[1],ev]);this.fire('message',['EOSE',m[1]]);},10);return;}
  return send.call(this,raw);};})();
"""

PROBE = r"""(()=>{const cs=s=>{const e=document.querySelector(s);return e?getComputedStyle(e):null;};
  const app=document.querySelector('.cc-app');
  const v=name=>{const d=document.createElement('i');d.style.color='var('+name+')';app.appendChild(d);const c=getComputedStyle(d).color;d.remove();return c;};
  return {theme:document.documentElement.dataset.ccTheme||'',
    conversation:cs('.cc-conversation').backgroundColor, channels:cs('.cc-channels').backgroundColor,
    rail:cs('.cc-communities').backgroundColor, activeChannelBg:cs('.cc-channel.active').backgroundColor,
    activeChannelFg:cs('.cc-channel.active').color, activeChannelShadow:cs('.cc-channel.active').boxShadow,
    activeChannelAnim:cs('.cc-channel.active').animationName,
    compose:cs('.cc-compose').backgroundColor, input:cs('#cc-input').color, identity:cs('.cc-identity').backgroundColor,
    body:cs('.cc-message-body p').color, activeServer:cs('.cc-server.active').backgroundColor,
    section:cs('.cc-section').color,
    send:cs('#cc-send').backgroundColor+' '+cs('#cc-send').backgroundImage+' '+cs('#cc-send').borderTopColor,
    vMain:v('--cct-main'), vSide:v('--cct-side'), vText:v('--cct-text'), vAccent:v('--cct-accent')};})()"""

# WCAG contrast of a text element against the first opaque background behind it (alpha-composited).
CONTRAST = r"""((sel)=>{const parse=c=>{const m=c.match(/[\d.]+/g)||[0,0,0,0];return [+m[0],+m[1],+m[2],m[3]===undefined?1:+m[3]];};
  const el=document.querySelector(sel);let fg=parse(getComputedStyle(el).color),layers=[];
  for(let n=el;n;n=n.parentElement){const b=parse(getComputedStyle(n).backgroundColor);if(b[3]>0)layers.push(b);if(b[3]>=1)break;}
  let bg=[0,0,0];for(const l of layers.reverse()){bg=[0,1,2].map(i=>l[i]*l[3]+bg[i]*(1-l[3]));}
  if(fg[3]<1)fg=[0,1,2].map(i=>fg[i]*fg[3]+bg[i]*(1-fg[3]));
  const lum=c=>{const [r,g,b]=c.slice(0,3).map(x=>{x/=255;return x<=.03928?x/12.92:((x+.055)/1.055)**2.4;});return .2126*r+.7152*g+.0722*b;};
  const a=lum(fg),b=lum(bg);return Math.round(((Math.max(a,b)+.05)/(Math.min(a,b)+.05))*100)/100;})"""


async def open_room(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(SETUP)
    await b.until("!!document.querySelector('[data-cc-channel]')||document.querySelectorAll('.cc-message').length>=50")
    await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
    await b.until("document.querySelectorAll('.cc-message').length>=50")


async def open_picker(b, width):
    if width < 600:   # the user bar lives in the rooms drawer on a phone
        await b.js("document.querySelector('#cc-back-channels').click()")
        await b.until("!!document.querySelector('.cc-app.drawer-open, .cc-app:not(.show-chat)')")
    await b.until("!!document.querySelector('#cc-theme')")
    await b.js("document.querySelector('#cc-theme').click()")
    await b.until("document.querySelectorAll('[data-cc-theme-pick]').length>=" + str(len(PRESETS)))


async def pick(b, theme):
    await b.js(f"document.querySelector('[data-cc-theme-pick=\"{theme}\"]').click()")
    await b.until(f"document.documentElement.dataset.ccTheme==={json.dumps(theme)}")
    await asyncio.sleep(.2)


def prefs_of(published):
    docs = [e for e in published if ["d", "pcai:client-prefs"] in [t[:2] for t in e.get("tags", [])]]
    return docs


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_concord_themes_default_classic_accent_contrast_and_sync(width):
    state = {}

    async def first_device(b):
        await open_room(b, width)
        got = await b.js(PROBE)
        # 1. A fresh account is in Cyberpunk, and the surfaces really READ the theme.
        assert got["theme"] == "cyberpunk", got
        assert got["conversation"] == got["vMain"] == CYBERPUNK["conversation"], ("conversation is not Cyberpunk", got)
        assert got["channels"] == got["vSide"], ("channel pane ignores the theme", got)
        assert got["body"] == got["vText"], ("message text ignores the theme", got)
        assert got["activeServer"] == got["vAccent"] == CYBERPUNK["activeServer"], ("active community ignores the accent", got)
        assert got["activeChannelAnim"] != "none", ("Cyberpunk's active channel has no glow", got)

        # 2. Message text is readable in every preset.
        for theme in PRESETS:
            await b.js(f"PCConcordTheme.set({{theme:{json.dumps(theme)}}},{{save:false}})")
            await asyncio.sleep(.15)
            for sel in (".cc-message-body p", "#cc-input", ".cc-conversation>header>b"):
                ratio = await b.js(CONTRAST + f"({json.dumps(sel)})")
                assert ratio >= 4.5, (f"{theme}: {sel} contrast {ratio} < 4.5")
            if theme == "monero":   # Monero's own orange over charcoal, not a recoloured Cyberpunk
                got = await b.js(PROBE)
                assert (got["vAccent"], got["conversation"]) == ("rgb(255, 102, 0)", "rgb(37, 37, 37)"), got
        await b.js("PCConcordTheme.set({theme:'cyberpunk'},{save:false})")

        # 3. Reduced motion: no animated glow.
        await b.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
        assert (await b.js(PROBE))["activeChannelAnim"] == "none", "glow still animates under prefers-reduced-motion"
        await b.call("Emulation.setEmulatedMedia", {"features": []})

        # 4. The picker, on this screen size: every choice on screen and tappable.
        await open_picker(b, width)
        rects = await b.js("[...document.querySelectorAll('[data-cc-theme-pick]')].map(e=>{const r=e.getBoundingClientRect();return [r.left,r.top,r.right,r.bottom,r.width,r.height]})")
        vw = await b.js("document.documentElement.clientWidth")
        for r in rects:
            assert r[0] >= 0 and r[2] <= vw + 1 and r[4] >= 44 and r[5] >= 44, ("a theme choice is off screen or too small to tap", r, vw)
        await pick(b, "classic")
        got = await b.js(PROBE)
        for key, want in CLASSIC.items():
            assert got[key] == want, (f"Classic is not today's look: {key}", got[key], want)

        # 5. A custom accent recolours the active channel, the active community and the buttons.
        await pick(b, "cyberpunk")
        await b.js("(()=>{const i=document.querySelector('#cc-theme-accent');i.value='#ff8800';i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));})()")
        await b.until("getComputedStyle(document.querySelector('.cc-server.active')).backgroundColor==='rgb(255, 136, 0)'")
        got = await b.js(PROBE)
        assert "rgb(255, 136, 0)" in got["activeChannelShadow"], ("active channel ignores the custom accent", got)
        assert "rgb(255, 136, 0)" in got["send"], ("Send ignores the custom accent", got)
        # Reset drops the override and goes back to the preset's accent.
        await b.js("document.querySelector('#cc-theme-reset').click()")
        await b.until("getComputedStyle(document.querySelector('.cc-server.active')).backgroundColor==='rgb(0, 240, 255)'")
        # The final choice: OLED with an orange accent.
        await pick(b, "oled")
        await b.js("(()=>{const i=document.querySelector('#cc-theme-accent');i.value='#ff8800';i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));})()")
        # Saves are serialized read-modify-writes of the prefs document, so the last one lands after
        # the ones before it: wait for it (up to 45 s), never for "any" publish.
        def final(published):
            return [e for e in prefs_of(published)
                    if (json.loads(e["content"]).get("concordTheme") or {}).get("theme") == "oled"
                    and (json.loads(e["content"]).get("concordTheme") or {}).get("accent") == "#ff8800"]
        docs = []
        for _ in range(90):
            docs = final(await b.js("window.__published"))
            if docs:
                break
            await asyncio.sleep(.5)
        assert docs, ("the choice was never saved to the account",
                      [json.loads(e["content"]).get("concordTheme") for e in prefs_of(await b.js("window.__published"))])
        state["seed"] = docs[-1:]

        # 6. A reload paints the choice before any relay has answered.
        await b.call("Page.reload")
        await b.until("!!window.__PC && document.readyState!=='loading'")
        early = await b.js("({t:document.documentElement.dataset.ccTheme,a:document.documentElement.style.getPropertyValue('--cct-accent').trim()})")
        assert early == {"t": "oled", "a": "#ff8800"}, ("the theme was not painted from the local cache", early)

    asyncio.run(desktop.with_browser("online", "", first_device, extra_init=PREFS))

    async def second_device(b):
        await open_room(b, width)
        await b.until("document.documentElement.dataset.ccTheme==='oled'")
        await b.until("getComputedStyle(document.querySelector('.cc-server.active')).backgroundColor==='rgb(255, 136, 0)'")
        got = await b.js(PROBE)
        assert got["conversation"] == got["vMain"] == "rgb(0, 0, 0)", ("second device is not in the synced OLED theme", got)

    asyncio.run(desktop.with_browser("online", "", second_device,
                                     extra_init="window.__seedPrefs=" + json.dumps(state["seed"]) + ";" + PREFS))
