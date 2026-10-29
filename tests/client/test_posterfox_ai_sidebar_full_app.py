"""PosterFox's AI sidebar opens this instance's AI chat with the prompt in the box, UNSENT.

PosterFox (the PosterChanOS browser) points Firefox's own AI sidebar at the PosterChan instance the desktop is signed in
to (posterfox.cfg: browser.ml.chat.provider = <instance>/client?pcai=1). Firefox opens that URL with `q=<prompt>` --
"Summarize", "Explain this", a selection. The shipped client must land in AI chat with exactly that text in the box
and send nothing (the prompt carries page text the person should see first), and clear the parameters so a reload
does not ask again.
"""
import asyncio
import urllib.parse
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
PROMPT = "Summarize this page:\n\nThe PosterChan desktop got workspaces & snap layouts."


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@CHROME
def test_the_sidebar_prompt_lands_in_ai_chat_unsent():
    got = {}
    route = '?pcai=1&q=' + urllib.parse.quote(PROMPT)

    async def check(b):
        await desktop.login(b)
        # Signed in already (the sidebar shares the browser's session), THEN Firefox opens the provider URL.
        await b.js("location.href = location.pathname + %r; true" % route)
        await b.until("!!window.__PC && document.readyState==='complete'")
        await b.until("!!document.getElementById('ai-input') && document.getElementById('ai-input').value.length>0")
        got['value'] = await b.js("document.getElementById('ai-input').value")
        got['search'] = await b.js("location.search")
        await asyncio.sleep(.5)
        got['sent'] = await b.js("(window.__chatRequests||[]).length")

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['value'] == PROMPT, got
    assert 'pcai' not in got['search'], ("the parameters stayed in the address, a reload would ask again", got)
    assert got['sent'] == 0, "the prompt was sent without the person pressing send"
