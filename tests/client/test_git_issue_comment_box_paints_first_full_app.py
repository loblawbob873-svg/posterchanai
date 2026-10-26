"""An issue's comment box is on screen before the relays answer -- and what was typed survives their answer.

The gate's "there is no comment box on the issue" failure, flaky under load in eight deploys: mountIssueStatus
awaited the repo announcement and the issue's status events before drawing ANY of the status bar, so on a slow relay
the issue page had no way to comment for as long as the relays took. It now paints from what the device holds and
corrects itself when they answer. Here the relay holds both reads; the box must still be there at once, text typed
into it must survive the late answer, and the late answer (the issue was closed) must still update the state.
Real bundled client, real Chrome.
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


HOLD = r"""
(() => {
  // Hold every REQ for the issue's status kinds or a repo announcement: the relay is slow, not empty.
  window.__held = []; window.__holding = true;
  const P = Object.getPrototypeOf(__sockets.find(s => s.readyState === 1));
  const send = P.send;
  P.send = function(raw){
    const m = JSON.parse(raw);
    if(__holding && Array.isArray(m) && m[0] === 'REQ' && m.slice(2).some(f => (f.kinds || []).some(k => k === 30617 || (k >= 1630 && k <= 1633)))){
      __held.push([this, raw]); return;
    }
    return send.call(this, raw);
  };
  window.__release = () => { __holding = false; for(const [s, raw] of __held.splice(0)) send.call(s, raw); };
})()
"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_the_comment_box_is_there_before_the_relays_answer(width):
    async def check(b):
        if width < 600:
            await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js(HOLD)
        # The issue's repo is not in the Store: setup() puts it only on the relay, which is holding.
        await b.js(setup(False))
        await b.until("!!document.querySelector('.gi-page')")
        await asyncio.sleep(2)
        got = await b.js("""({ box: !!document.querySelector('.gi-page .issue-status .issue-comment'),
                               held: __held.length })""")
        assert got["held"] >= 1, ("the test did not hold the status read, so it proves nothing", got)
        assert got["box"], ("the issue page has no comment box while the relay is slow", got)
        # Type, then the relay answers late: the issue was closed by its author.
        await b.js("(()=>{const t=document.querySelector('.issue-comment');t.focus();t.value='Still happens on 1.0.2611';t.dispatchEvent(new Event('input'))})()")
        await b.js("""(() => { const iss = __events.find(e => e.kind === 1621);
            __events.push({ id: '9'.repeat(64), kind: 1632, pubkey: iss.pubkey, created_at: iss.created_at + 5, sig: 'b'.repeat(128),
                            content: '', tags: [['e', iss.id, '', 'root'], ['p', iss.pubkey]] });
            __release(); })()""")
        await b.until("document.querySelector('.issue-status') && document.querySelector('.issue-status').dataset.state === 'closed'")
        after = await b.js("""({ text: document.querySelector('.issue-comment').value,
                                 focused: document.activeElement === document.querySelector('.issue-comment'),
                                 send: !document.querySelector('.issue-status [data-is="comment"]').disabled })""")
        assert after["text"] == "Still happens on 1.0.2611", ("the late answer wiped what was typed", after)
        assert after["focused"], ("the late answer took the cursor out of the comment box", after)
        assert after["send"], ("the Comment button is disabled over typed text", after)
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))
