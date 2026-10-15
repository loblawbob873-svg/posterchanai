"""A file window (Files -> open, on the desktop) shows the file -- never the social timeline first.

"Preview is showing the social timeline first before trying to open the file preview!" (2026-10-15). On
PosterChanOS opening a file from Files opens a `doc:file:<key>` window: a fresh client page that boots, then
runs the file job. Every frame between those two is the page's default screen. A MutationObserver installed
before any script records whether a timeline card or the timeline view was EVER drawn in that window.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


WATCH = r"""window.__sawTimeline = [];
new MutationObserver(() => {
  const f = document.getElementById('feed'); if (!f) return;
  const hit = f.querySelector('.note, article, .tl-cmp, [id^="tl-cmp"], .feed-tabs, .tl-tabs');
  if (hit && __sawTimeline.length < 5) __sawTimeline.push((hit.className || hit.id || hit.tagName) + '');
}).observe(document, {subtree:true, childList:true});"""


VIEW = 'doc:file:abcdefgh12'
CTX = "window.pcShell.windowContext={role:'app',view:'%s'};window.pcShell.backgroundOwner=false;" % VIEW


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_file_window_never_draws_the_timeline():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("/no file to show|Open it again/i.test(document.getElementById('feed')?.textContent||'')")
        await asyncio.sleep(1.0)
        got['saw'] = await b.js('window.__sawTimeline')
        got['view'] = await b.js('__PC.VIEW')

    asyncio.run(desktop.with_browser("online", "?pcwin=" + VIEW, check, WATCH + CTX))
    assert got['saw'] == [], ('the file window drew the timeline before the file', got)
