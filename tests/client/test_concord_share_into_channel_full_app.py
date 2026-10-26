"""Android: share from another app into a Communities channel ("needs helper to share content from other apps into a
concord community/channel", 2026-10-26).

The APK's "PosterChan Communities" share entry (ShareToCommunities alias; ShareTarget reports "concord") hands the
share to the page. The real handler (__PC.consumeShare = app.js _consumeSendIntent) with a stand-in for Android's
send-intent plugin: the picker lists the community's channels, choosing one opens it, and its composer holds the
shared text -- or the shared picture is uploaded through the attach path. Nothing is sent until Send.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

ROOT = Path(__file__).resolve().parents[2]
CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
           "||'{}'),osMode:false}));")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def native_share(payload_js, nonce):
    return ("(()=>{window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{"
            "SendIntent:{checkSendIntentReceived:async()=>(%s)},"
            "ShareTarget:{getTarget:async()=>({target:'concord',nonce:%d})}}};"
            "window.__shareDone=__PC.consumeShare();return true;})()" % (payload_js, nonce))


async def _ready(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 850, "deviceScaleFactor": 1, "mobile": True})
    await desktop.login(b)
    await b.js("(()=>{window.__toasts=[];const t=__PC.toast;__PC.toast=(m)=>{__toasts.push(String(m));try{t(m)}catch(_){}};window.__publishOK=true;})()")
    await b.js(ROOM)
    await b.until("!!document.querySelector('.cc-app')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_shared_text_lands_in_the_chosen_channels_composer():
    got = {}

    async def check(b):
        await _ready(b)
        await b.js("__PC.switchView('home')")
        await b.js(native_share("{type:'text/plain',url:'Look at this https://example.com/a'}", 11))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        got["choices"] = await b.js("[...document.querySelectorAll('[data-cc-share-channel]')].map(e=>e.textContent)")
        got["fits"] = await b.js("[...document.querySelectorAll('.cc-share-channel')].every(e=>{const r=e.getBoundingClientRect();return r.height>=44&&r.right<=innerWidth+1})")
        await b.js("document.querySelector('[data-cc-share-channel=\"general\"]').click()")
        await b.until("!!document.querySelector('.cc-app.show-chat #cc-input') && /example.com/.test(document.querySelector('#cc-input').value)")
        got["composer"] = await b.js("document.querySelector('#cc-input').value")
        got["sent"] = await b.js("(window.__wraps||[]).length")
        got["view"] = await b.js("__PC.isView('concord')")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["choices"] == ["#general"], got
    assert got["fits"], ("the channel choices are not phone-sized targets", got)
    assert got["composer"] == "Look at this https://example.com/a", got
    assert got["view"] and got["sent"] == 0, ("the share was sent without the person pressing Send", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_shared_picture_is_uploaded_through_the_channels_attach_path():
    got = {}

    async def check(b):
        await _ready(b)
        # Android's plugin hands a file URI; the page reads it (_readSharedFile). Serve a real PNG at that URI.
        await b.js("(()=>{const png=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=='),c=>c.charCodeAt(0));"
                   "const f=window.fetch;window.fetch=(u,o)=>String(u).includes('shared-photo.png')?Promise.resolve(new Response(new Blob([png],{type:'image/png'}))):f(u,o);})()")
        await b.js("__PC.switchView('home')")
        await b.js(native_share("{type:'image/png',url:'http://localhost/_capacitor_file_/data/shared-photo.png'}", 12))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        await b.js("document.querySelector('[data-cc-share-channel=\"general\"]').click()")
        await b.until("(window.__toasts||[]).some(t=>/uploading shared-photo\\.png/.test(t))")
        got["toasts"] = await b.js("__toasts.slice(-4)")
        got["sent"] = await b.js("(window.__wraps||[]).length")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["sent"] == 0, got


def test_the_apk_has_a_communities_share_entry_that_reports_concord():
    manifest = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
    entry = manifest[manifest.index('android:name=".ShareToCommunities"'):]
    entry = entry[:entry.index("</activity-alias>")]
    assert 'android:label="PosterChan Communities"' in entry and "android.intent.action.SEND" in entry
    plugin = (ROOT / "mobile/android/app/src/main/java/place/poster/app/share/ShareTargetPlugin.java").read_text()
    assert '"place.poster.app.ShareToCommunities"' in plugin and '"concord"' in plugin
