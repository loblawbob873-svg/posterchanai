"""Settings → Signer → "Scan sign-in QR", then "cancel" (#qr-cancel), in the SHIPPED bundle (2026-10-22: every
button gets a test).

The scanner holds the CAMERA. Cancel must close the scanner and let the camera go -- a light that stays on after
somebody said cancel is the failure a person notices. The camera is a real MediaStreamTrack drawn from a canvas;
real login, DOM and scanner.js.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CAMERA = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__gum=0;window.__tracks=[];
navigator.mediaDevices.getUserMedia=async c=>{__gum++;const cv=document.createElement('canvas');cv.width=320;cv.height=240;
  const g=cv.getContext('2d');g.fillStyle='#246';g.fillRect(0,0,320,240);const s=cv.captureStream(5);
  s.getTracks().forEach(t=>__tracks.push(t));return s;};
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_cancel_closes_the_scanner_and_releases_the_camera():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('signer'); 1")
        await b.until("!!document.querySelector('#signer-scan-qr') && !document.querySelector('#signer-scan-qr').disabled")
        await b.js("document.querySelector('#signer-scan-qr').click(); 1")
        await b.until("!!document.querySelector('#qr-cancel') && __tracks.length>0 && "
                      "__tracks.every(t=>t.readyState==='live') && !!document.querySelector('#qr-video').srcObject")
        await b.js("document.querySelector('#qr-cancel').click(); 1")
        await b.until("!document.querySelector('#qr-cancel') && !document.querySelector('#qr-video')")
        await b.until("__tracks.every(t=>t.readyState==='ended')")        # the camera is let go
        assert await b.js("__gum") == 1
        assert not await b.js("!!document.querySelector('#qr-paste-uri')"), "cancel fell through to the paste dialog"
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, CAMERA))
