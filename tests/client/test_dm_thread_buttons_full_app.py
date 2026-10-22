"""A conversation's own buttons, pressed in the SHIPPED bundle on a seeded 40-message conversation:

  * "Load older" shows more of the history (25 on open, then the rest);
  * 🔒 switches attachment encryption on and off, and the button says which;
  * ← Back closes the conversation and returns to the list.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_chat_assist_full_app import PLAIN, ASSIST
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED as DM_SEED


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_load_older_lock_and_back():
    async def check(b):
        await desktop.login(b)
        pk = await b.js(DM_SEED + '(40)')
        await b.js("__PC.switchView('messages')")
        sel = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
        await b.until(f"!!document.querySelector('{sel}')")
        await b.js(f"document.querySelector('{sel}').click()")
        await b.until("!!document.querySelector('#dm-in') && !!document.querySelector('#dm-older')")
        shown = await b.js("document.querySelectorAll('#dm-msgs .dm-msg, #dm-msgs .bubble, #dm-msgs [data-id]').length")
        assert shown >= 20, ("the opening page of the conversation drew too few messages", shown)
        await b.js("document.querySelector('#dm-older').click(); 1")
        await b.until(f"!document.querySelector('#dm-older') && document.querySelectorAll('#dm-msgs .dm-msg, #dm-msgs .bubble, #dm-msgs [data-id]').length>{shown}")

        was = await b.js("document.querySelector('#dm-lock').classList.contains('on')")
        await b.js("document.querySelector('#dm-lock').click(); 1")
        await b.until(f"document.querySelector('#dm-lock').classList.contains('on')==={json.dumps(not was)}")
        await b.js("document.querySelector('#dm-lock').click(); 1")
        await b.until(f"document.querySelector('#dm-lock').classList.contains('on')==={json.dumps(was)}")

        await b.js("document.querySelector('#dm-back').click(); 1")
        await b.until("!document.querySelector('#dm-list').classList.contains('has-active') && !document.querySelector('#dm-in')")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN + RELAY + ASSIST))
