"""Telegram's message box has PosterChan's emoji picker, and a pick lands where the cursor is.

2026-10-09: "add our custom emoji picker to telegram?". Standard emoji only -- Telegram renders custom
emoji from its own sets alone, so one of ours would arrive as `:name:` text (the same rule Texts uses).
Driven in the shipped bundle as Telegram's own window against the fake Telegram backend.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE

WINDOW = "window.pcShell.windowContext={role:'app',view:'tg'};window.pcShell.backgroundOwner=false;"


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_emoji_button_inserts_at_the_cursor_and_offers_only_standard_emoji():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('.tg-composer [data-act=\"emoji\"]')")
        await b.js("(()=>{const t=document.querySelector('.tg-text');t.value='hello world';t.focus();t.setSelectionRange(5,5)})()")
        await b.js("document.querySelector('.tg-composer [data-act=\"emoji\"]').click()")
        await b.until("!!document.querySelector('.emoji-pop [data-e]')")
        got["custom"] = await b.js("[...document.querySelectorAll('.emoji-pop [data-e]')].filter(e=>/^:.+:$/.test(e.dataset.e)||e.querySelector('img')).length")
        got["picked"] = await b.js("(()=>{const e=document.querySelector('.emoji-pop [data-e]');const v=e.dataset.e;"
                                   "e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));return v})()")
        await asyncio.sleep(.2)
        got["text"] = await b.js("document.querySelector('.tg-text').value")
        got["pop_closed"] = await b.js("!document.querySelector('.emoji-pop')")
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "?pcwin=tg", check,
                                     extra_init=FAKE.replace("state:'none'", "state:'ready'") + WINDOW))
    assert got["picked"], got
    assert got["text"] == "hello" + got["picked"] + " world", ("the emoji did not land at the cursor", got)
    assert got["custom"] == 0, ("custom emoji were offered; Telegram would receive them as :name: text", got)
    assert got["pop_closed"], got
    assert not got["errors"], got["errors"]
