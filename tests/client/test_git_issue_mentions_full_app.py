"""A person mentioned in a git issue is a NAME, and the comment box finds people with @, like the social composer.

Reported 2026-10-29: "our git is missing nostr:npub tagging, all your comments to vyram look like: nostr:npub15nzy…
Faster again, live on poster.place now." The issue page renders comments with mdToHtml, which knows no Nostr
entities, so every reply that tagged its reader opened with a 63-character key. Then: "make sure we can @ find the
user to mention like on social for git" -- the issue's comment box had no people picker at all. Real bundled client,
real Chrome: the issue page draws the mention as a clickable @name, typing @ali offers alice, picking her inserts
nostr:npub, and the posted comment p-tags her so she is told.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_issue_close_from_thread_full_app import ISSUE_ID, setup

ALICE = "8" * 64


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


EXTRA = r"""
  const _ow = __REPO.pubkey, _t0 = Math.floor(Date.now() / 1000), _np = NostrTools.nip19.npubEncode(ALICE);
  window.__alice = _np;
  window.__events.push({ id: '5'.repeat(64), kind: 0, pubkey: ALICE, created_at: _t0 - 500, sig: 'a'.repeat(128),
                         content: JSON.stringify({ name: 'alice' }), tags: [] });
  // the shape of every "Fixed, live" reply: the reader's key first, then the text; plus a key inside code, kept raw
  window.__events.push({ id: '1'.repeat(64), kind: 1111, pubkey: _ow, created_at: _t0 - 30, sig: 'a'.repeat(128),
    content: 'nostr:' + _np + ' Faster again, live now. Literal: `' + _np + '`',
    tags: [['E', ISSUE, '', _ow], ['K', '1621'], ['P', _ow], ['e', ISSUE, '', _ow], ['k', '1621'], ['p', ALICE]] });
""".replace("ISSUE", json.dumps(ISSUE_ID)).replace("ALICE", json.dumps(ALICE))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_issue_mentions_are_names_and_the_comment_box_finds_people(width):
    async def check(b):
        if width < 600:
            await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js("window.__events = window.__events || []")
        await b.js(setup(True).replace("  __PC.openThread(", EXTRA + "\n  __PC.openThread("))
        await b.until("document.querySelectorAll('.gi-page .gi-c').length >= 2")
        await b.until("!!document.querySelector('.gi-page .gi-c-body a.mention')")
        await b.until("document.querySelector('.gi-page .gi-c-body a.mention').textContent === '@alice'")
        got = await b.js("""(() => {
          const body = [...document.querySelectorAll('.gi-page .gi-c-body')].find(n => n.querySelector('a.mention'));
          const m = body.querySelector('a.mention');
          return { text: body.textContent, np: m.dataset.np, code: (body.querySelector('code') || {}).textContent || '' };
        })()""")
        assert not got["text"].startswith("nostr:"), ("the comment still opens with the raw key", got)
        assert got["text"].startswith("@alice Faster again"), got
        assert got["np"] == await b.js("__alice"), got
        assert got["code"] == await b.js("__alice"), ("a key inside `code` must stay text", got)
        # clicking the mention opens that person's profile
        await b.js("document.querySelector('.gi-page .gi-c-body a.mention').click()")
        await b.until("!!document.querySelector('#prof-list') && document.querySelector('#feed').innerText.includes('alice')")

        # the comment box finds people with @
        await b.js(setup(True).replace("  __PC.openThread(", EXTRA + "\n  __PC.openThread("))
        await b.until("!!document.querySelector('.issue-comment')")
        await b.js("""(() => { const t = document.querySelector('.issue-comment'); t.focus();
          t.value = 'Thanks @ali'; t.selectionStart = t.selectionEnd = t.value.length;
          t.dispatchEvent(new Event('input', { bubbles: true })); })()""")
        await b.until("!!document.querySelector('.issue-status .mention-box:not(.hidden) .mention-opt')")
        assert "alice" in await b.js("document.querySelector('.issue-status .mention-box .mention-opt').textContent")
        await b.js("""(() => { const o = document.querySelector('.issue-status .mention-box .mention-opt');
          ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t => o.dispatchEvent(new (t.startsWith('pointer') ? PointerEvent : MouseEvent)(t, { bubbles: true, cancelable: true, button: 0 }))); })()""")
        await b.until("document.querySelector('.issue-comment').value.includes('nostr:npub1')")
        assert await b.js("document.querySelector('.issue-comment').value") == "Thanks nostr:" + await b.js("__alice") + " "
        await b.js("(()=>{const t=document.querySelector('.issue-comment');t.dispatchEvent(new Event('input'));document.querySelector('[data-is=\"comment\"]').click();})()")
        await b.until("__gitSent.some(e => e.kind === 1111)")
        tags = await b.js("__gitSent.find(e => e.kind === 1111).tags")
        assert ["p", ALICE] in [t[:2] for t in tags], ("the mentioned person is not tagged, so never told", tags)
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))
