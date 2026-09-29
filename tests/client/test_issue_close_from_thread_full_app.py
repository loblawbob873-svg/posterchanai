"""A git issue can be closed or resolved from its own thread, with or without a closing comment.

Asked for: "Add an option to close/resolve issue in the last comment. And an option to close/resolve
issue in the thread of issue." Status could only be set from the repo's issue LIST. The real bundled
client: the issue thread, the status bar under the issue, publish(), relay.js; the relay is the test
fixture's socket, which stores what it is sent.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ISSUE_ID = "cd" * 32


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


def setup(author_is_me=True):
    return (r"""
(() => {
  window.__toasts = [];
  const root = document.getElementById('toast-root');
  new MutationObserver(() => root.querySelectorAll('.toast').forEach(t => { if(!t.__seen){ t.__seen = 1; __toasts.push(t.textContent); } }))
    .observe(root, { childList: true });
  window.__gitSent = [];
  const P = Object.getPrototypeOf(__sockets.find(s => s.readyState === 1));
  const send = P.send;
  P.send = function(raw){
    const m = JSON.parse(raw);
    if(Array.isArray(m) && m[0] === 'EVENT' && [1111, 1630, 1631, 1632].includes(m[1].kind)){
      __gitSent.push(m[1]);
      (window.__events = window.__events || []).push(m[1]);
      setTimeout(() => this.fire('message', ['OK', m[1].id, true, '']), 30);
      return;
    }
    return send.call(this, raw);
  };
  const me = __PC.me().pubkey, now = Math.floor(Date.now() / 1000), other = '7'.repeat(64);
  const owner = AUTHOR_IS_ME ? me : other;
  window.__REPO = { id: 'c'.repeat(64), kind: 30617, pubkey: owner, created_at: now - 3600, sig: 'e'.repeat(128), content: '',
                    tags: [['d', 'demo'], ['name', 'demo']] };
  window.__events = [__REPO, { id: 'ISSUE', kind: 1621, pubkey: owner, created_at: now - 60, sig: 'f'.repeat(128),
                       content: 'The live cover picker does nothing.',
                       tags: [['a', '30617:' + owner + ':demo'], ['subject', 'Bug: cover picker']] }];
  __PC.openThread('ISSUE');
})()
""".replace("'ISSUE'", json.dumps(ISSUE_ID)).replace("AUTHOR_IS_ME", "true" if author_is_me else "false"))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_comment_and_close_then_reopen_from_the_issue_thread(width):
    async def check(b):
        if width < 600:
            await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js(setup(True))
        await b.until("!!document.querySelector('.issue-status [data-is=\"1632\"]')")
        assert "Open" in await b.js("document.querySelector('.issue-status .collab-state').textContent")
        # Typing turns the buttons into "Comment & …".
        await b.js("(()=>{const t=document.querySelector('.issue-comment');t.value='Fixed in 1.0.1745 — thanks!';t.dispatchEvent(new Event('input'))})()")
        assert await b.js("document.querySelector('[data-is=\"1632\"]').textContent.trim()") == "Comment & close"
        await b.js("document.querySelector('[data-is=\"1632\"]').click()")
        await b.until("__gitSent.some(e=>e.kind===1632)")
        sent = await b.js("__gitSent.map(e=>({k:e.kind,c:e.content,t:e.tags}))")
        kinds = [e["k"] for e in sent]
        assert kinds.index(1111) < kinds.index(1632), f"the status went out before the comment: {kinds}"
        comment = next(e for e in sent if e["k"] == 1111)
        assert comment["c"] == "Fixed in 1.0.1745 — thanks!"
        assert ["E", ISSUE_ID] == comment["t"][0][:2] or any(t[:2] == ["E", ISSUE_ID] for t in comment["t"]), comment["t"]
        close = next(e for e in sent if e["k"] == 1632)
        assert any(t[:2] == ["e", ISSUE_ID] for t in close["t"]), close["t"]
        await b.until("__toasts.some(t=>/Commented and issue closed/.test(t))")
        await b.until("!!document.querySelector('.issue-status[data-state=\"closed\"] [data-is=\"1630\"]')")
        # Reopen, without a comment.
        await b.js("document.querySelector('[data-is=\"1630\"]').click()")
        await b.until("__gitSent.some(e=>e.kind===1630)")
        await b.until("!!document.querySelector('.issue-status[data-state=\"open\"]')")
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_somebody_else_sees_the_state_but_no_buttons():
    async def check(b):
        await desktop.login(b)
        await b.js(setup(False))
        await b.until("!!document.querySelector('.issue-status .collab-state')")
        assert not await b.js("!!document.querySelector('.issue-status [data-is]')"), \
            "a reader who is neither the author nor a maintainer was offered Close"

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))
