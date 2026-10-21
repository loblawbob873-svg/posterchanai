"""Opening a git issue shows an ISSUE PAGE, not a social thread.

Reported 2026-10-13: "When I open an issue or respond to a comment in an issue, it looks like a regular social thread.
I have no idea what the issue is. Does not feel like I am in Git." The issue opened through the timeline's thread view:
the issue as a post card with like/repost/zap under it and its comments as a reply tree. Now it is the issue the way a
forge shows one -- the repo it belongs to, the title, who opened it, the description, the comments in order with
"replying to" where a comment answers another, and the comment box at the end. Real bundled client, real Chrome.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_issue_close_from_thread_full_app import ISSUE_ID, setup


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


COMMENTS = r"""
(() => {
  const _ow = __REPO.pubkey, _t0 = Math.floor(Date.now() / 1000), _alice = '8'.repeat(64);
  const _c = (id, parent, pk, text, t) => ({ id, kind: 1111, pubkey: pk, created_at: _t0 - t, sig: 'a'.repeat(128), content: text,
    tags: [['E', ISSUE, '', _ow], ['K', '1621'], ['P', _ow], ['e', parent, '', pk], ['k', parent === ISSUE ? '1621' : '1111'], ['p', _ow]] });
  const _c1 = _c('1'.repeat(64), ISSUE, _alice, 'I can reproduce this on **Firefox** too.', 40);
  const _c2 = _c('2'.repeat(64), _c1.id, _ow, 'Thanks, looking at it now.', 20);
  // Armada's attachment shape (the ngit issue "Change multiple reactions display", 2026-10-21): the bare URL on its
  // own line plus an NIP-92 imeta. The first has NO extension -- only imeta says it is a picture.
  const _shot = 'https://blossom.example/' + 'b'.repeat(64), _vid = 'https://cdn.example/clip.mp4', _page = 'https://example.com/docs';
  const _c3 = _c('3'.repeat(64), ISSUE, _alice, 'Like this:\n' + _shot + '\n\nand ' + _vid + '\n\nsee ' + _page, 10);
  _c3.tags.push(['imeta', 'url ' + _shot, 'm image/jpeg', 'x ' + 'b'.repeat(64)]);
  window.__events.push(_c1, _c2, _c3);
})()
""".replace("ISSUE", json.dumps(ISSUE_ID))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_an_issue_opens_as_an_issue_page_with_its_comments(width):
    async def check(b):
        if width < 600:
            await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 800, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        # comments first, then the setup opens the thread
        await b.js("window.__events = window.__events || []")
        js = setup(True).replace("  __PC.openThread(", COMMENTS.replace("(() => {", "").rsplit("})()", 1)[0] + "\n  __PC.openThread(")
        await b.js(js)
        await b.until("document.querySelectorAll('.gi-page .gi-c').length >= 4")
        got = await b.js("""(() => {
          const p = document.querySelector('.gi-page');
          return { crumb: p.querySelector('.gi-crumb').textContent.trim(),
                   title: p.querySelector('.gi-title').textContent.trim(),
                   meta: p.querySelector('.gi-meta').textContent,
                   boxes: p.querySelectorAll('.gi-c').length,
                   body: p.querySelector('.gi-c .gi-c-body').textContent.trim(),
                   bold: !!p.querySelectorAll('.gi-c')[1].querySelector('.gi-c-body b, .gi-c-body strong'),
                   re: [...p.querySelectorAll('.gi-re')].map(n => n.textContent),
                   social: document.querySelectorAll('#feed .thread-node, #feed .gi-page .acts').length,
                   compose: !!p.querySelector('.issue-status .issue-comment'),
                   imgs: [...p.querySelectorAll('.gi-c-body img')].map(i => i.getAttribute('src')),
                   vids: [...p.querySelectorAll('.gi-c-body video')].map(v => v.getAttribute('src')),
                   links: [...p.querySelectorAll('.gi-c-body a:not(.gi-media)')].map(a => a.getAttribute('href')),
                   overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
        })()""")
        assert got["crumb"].startswith("demo"), ("the page does not say which repo the issue belongs to", got)
        assert got["title"].startswith("Bug: cover picker"), ("the issue's title is not the heading", got)
        assert "opened this" in got["meta"] and "3 comments" in got["meta"], got
        assert got["boxes"] == 4, ("the description and the comments are not all shown", got)
        assert got["imgs"] == ["https://blossom.example/" + "b" * 64], ("an attached screenshot is not shown as a picture", got)
        assert got["vids"] == ["https://cdn.example/clip.mp4"], ("an attached video is not playable", got)
        assert got["links"] == ["https://example.com/docs"], ("an ordinary link stopped being a link", got)
        assert got["body"] == "The live cover picker does nothing.", got
        assert got["bold"], ("comments are not rendered as Markdown", got)
        assert len(got["re"]) == 1, ("a comment answering another comment does not say so", got)
        assert got["social"] == 0, ("the issue still renders as a social thread (cards / like-repost bars)", got)
        assert got["compose"], ("there is no comment box on the issue", got)
        assert got["overflow"] <= 1, ("the issue page scrolls sideways", got)
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))
