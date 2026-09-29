"""Close / Resolve on a git issue with a SLOW relay ends in success, never in "timeout".

Reported with two screenshots: pressing Close showed two toasts, "timeout" and "relay: timeout",
while the issue did close (open count 5 -> 4 on the next load). The relay stored the status; its OK
reached the phone after Relay.publish had given up. The generic publish() toast said "timeout", and
the Close handler said "relay: timeout" underneath it.

Runs the real bundled client (login, the repo page, the Close/Resolve buttons, relay.js, publish())
at phone width and desktop width. The fixtures are the socket -- which acknowledges a git event only
AFTER the publish budget has run out, the measured shape -- and the issue it serves. Against the
pre-fix client every case fails: the toasts say "timeout" / "relay: timeout" and nothing ever says
the issue closed.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ISSUE_ID = "ab" * 32


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""
(() => {
  window.__toasts = [];
  const root = document.getElementById('toast-root');
  const grab = () => root.querySelectorAll('.toast').forEach(t => { if(!t.__seen){ t.__seen = 1; __toasts.push(t.textContent); } });
  new MutationObserver(grab).observe(root, { childList: true });
  // The shipped Relay.publish, with a smaller budget so the test does not wait eight seconds.
  const orig = Relay.publish.bind(Relay);
  Relay.publish = (ev) => orig(ev, 1500);
  Relay.LATE_MS = 10000;
  // THE SLOW RELAY: everything it says about a git event -- the OK, and the event itself for a
  // read-back by id -- arrives 2.5 s late, i.e. after the publish budget. (A phone on a slow link
  // gets the OK behind the socket's subscription backlog.)
  window.__gitSent = [];
  const P = Object.getPrototypeOf(__sockets.find(s => s.readyState === 1));
  const send = P.send;
  P.send = function(raw){
    const m = JSON.parse(raw);
    if(Array.isArray(m) && m[0] === 'EVENT' && [1621, 1630, 1631, 1632, 1633].includes(m[1].kind)){
      __gitSent.push(m[1].id);
      setTimeout(() => {
        if(!(window.__events || []).some(e => e.id === m[1].id)) (window.__events = window.__events || []).push(m[1]);
        this.fire('message', ['OK', m[1].id, true, '']);
      }, 2500);
      return;
    }
    return send.call(this, raw);
  };
  const me = __PC.me().pubkey, now = Math.floor(Date.now() / 1000);
  window.__REPO = { id: 'c'.repeat(64), kind: 30617, pubkey: me, created_at: now - 3600, sig: 'e'.repeat(128), content: '',
                    tags: [['d', 'demo'], ['name', 'demo']] };
  window.__events = [{ id: 'ISSUE', kind: 1621, pubkey: me, created_at: now - 60, sig: 'f'.repeat(128),
                       content: 'The live cover picker does nothing.',
                       tags: [['a', '30617:' + me + ':demo'], ['subject', 'Bug: Thumb/Cover for live don’t work']] }];
  __PC.openRepo(__REPO);
})()
""".replace("'ISSUE'", json.dumps(ISSUE_ID))


async def _run(width, kind):
    async def check(b):
        if width < 600:
            await b.call("Emulation.setDeviceMetricsOverride",
                         {"width": width, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js(SETUP)
        button = f".cf-act[data-id='{ISSUE_ID}'][data-kind='{kind}']"
        await b.until(f"!!document.querySelector(\"{button}\")")
        # Double tap: the second press lands while the first is in flight.
        await b.js(f"(()=>{{const x=document.querySelector(\"{button}\"); x.click(); x.click();}})()")
        # Past the publish budget, before the relay's late OK: the unknown outcome is said as one.
        await asyncio.sleep(2.1)
        early = await b.js("__toasts.slice()")
        assert not any("timeout" in t.lower() or t.startswith("relay:") for t in early), \
            f"a stored status was reported as a failure: {early}"
        assert any("not confirmed" in t.lower() for t in early), f"the unknown outcome was not said: {early}"
        done = "issue closed" if kind == 1632 else "marked resolved"
        await b.until(f"__toasts.includes({json.dumps(done)})")
        state = "st-closed" if kind == 1632 else "st-resolved"
        await b.until(f"!!document.querySelector('#rv-issues .collab-state.{state}') || "
                      f"/Closed 1/.test((document.querySelector('#rv-issues .collab-filter')||{{}}).textContent||'')")
        toasts = await b.js("__toasts.slice()")
        assert not any("timeout" in t.lower() or t.startswith("relay:") for t in toasts), toasts
        sent = await b.js("__gitSent.slice()")
        assert len(set(sent)) == 1, f"one press, one double tap -- and {len(set(sent))} different status events: {sent}"
        errors = await b.js("__errors.slice()")
        assert not errors, errors

    await desktop.with_browser("online", "?pcShell=1", check)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("kind", [1632, 1631], ids=["close", "resolve"])
def test_close_and_resolve_with_a_slow_relay_succeed(width, kind):
    asyncio.run(_run(width, kind))
