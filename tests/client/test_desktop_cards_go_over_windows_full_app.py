"""On PosterChanOS a notification card goes to its own always-on-top window -- social AND Telegram.

"desktop missed another social notification": the desktop drew its card inside the desktop surface,
which sits under every application window. "i am not seeing any notifications for telegram messages":
osNotify used Electron's native notifications, which need a notification server this OS does not run.
Drives the SHIPPED bundle with the preload's pcToast bridge stubbed the way preload injects it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

BRIDGE = r'''
window.__publishOK = true;
window.__toasts = []; window.__toastClick = null;
window.pcToast = { show: c => { __toasts.push(c); return Promise.resolve(true); },
                   onClick: cb => { __toastClick = cb; } };
'''


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_social_and_telegram_cards_go_to_the_card_window_and_clicks_come_back():
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!window.PCOS && PCOS.isOn()")
        # A social notification card, the way notifs.js raises it on the desktop.
        await b.js("window.__went=[];PCOS.osToast('<b>alice</b> mentioned you', '', ()=>__went.push('social'), 'mention');true")
        await asyncio.sleep(.3)
        res["social"] = await b.js("({sent:__toasts.slice(), inPage:document.querySelectorAll('.os-toast').length})")
        # A Telegram message, the way telegram.js announces it.
        await b.js("__PC.osNotify('Telegram · Bob', 'are you there?', {tag:'tg-1-2', notificationType:'dm', onClick:()=>__went.push('telegram')});true")
        await asyncio.sleep(.3)
        res["telegram"] = await b.js("__toasts.slice(-1)[0]||null")
        # The card window reports a click on the Telegram card: the page runs that card's action.
        await b.js("__toastClick(__toasts.slice(-1)[0].id);true")
        await asyncio.sleep(.2)
        res["went"] = await b.js("__went.slice()")

    asyncio.run(desktop.with_browser("online", "", check, BRIDGE))
    s = res["social"]
    assert len(s["sent"]) == 1 and "mentioned you" in s["sent"][0]["html"], ("the social card never reached the card window", res)
    assert s["inPage"] == 0, ("the card was drawn inside the desktop surface, under every window", res)
    t = res["telegram"]
    assert t and "Telegram · Bob" in t["html"] and "are you there?" in t["html"], ("the Telegram message raised no card", res)
    assert res["went"] == ["telegram"], ("clicking the card did not do what that notification does", res)
