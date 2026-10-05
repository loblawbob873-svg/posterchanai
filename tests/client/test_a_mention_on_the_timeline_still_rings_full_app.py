"""A mention rings the bell and pops its notification even when the TIMELINE stored it first.

"on desktop, I did not see a notification bell or notification for my last 2 notifications". Both
arrived (the desktop's cache held them and the user later opened one from the notification centre),
but neither announced itself. The notification subscription announced an event only when IT was the
first to put it in the Store -- `if(Store.saveEvent(ev)){ bumpNotif(); notifPing(ev) }` -- and a
mention is also a kind-1 post, so with Home or Global open the timeline's own subscription on the same
socket can receive and store it a moment earlier. "New to the Store" is not "new to the notifications":
the second subscription to see it found it already saved and stayed silent.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_notifs_module_boots_full_app import RELAY, NOTE, ME_PK, MENTIONS, BUILD_PROBE


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


TIMELINE = ("__reqs.filter(r=>r.fs.some(f=>(f.kinds||[]).includes(1)&&!f['#p']&&!f['#e']&&!f.authors&&!f.ids&&!f.until"
            "&&!(f.kinds||[]).includes(9735)))")
STATE = ("({toast:[...document.querySelectorAll('#toast-root > *, .toast')].map(t=>t.innerText).join(' | '),"
         "unread:__PC.notifUnread(), badge:[...document.querySelectorAll('#notif-badge,#notif-badge-m')]"
         ".some(e=>!e.classList.contains('hidden')&&+e.textContent>=1)})")


async def arrive(first_to_timeline):
    out = {}

    async def check(b):
        await desktop.login(b)
        await b.until(MENTIONS + ".length>0")
        await b.js("__PC.switchView('global')")
        await b.until(TIMELINE + ".length>0")
        await asyncio.sleep(1.5)                                   # past EOSE: what arrives now is live
        await b.js("document.querySelectorAll('#toast-root > *, .toast').forEach(t=>t.remove()); true")
        await b.js("(()=>{const me=" + ME_PK + ";const ev=" + NOTE + "(9,'SOMEBODY MENTIONS YOU',0,[['p',me]]);"
                   "const r=_rel();r.push(ev);localStorage.setItem('__relayEvents',JSON.stringify(r));"
                   + ("for(const q of " + TIMELINE + ")q.sock.fire('message',['EVENT',q.sub,ev]);" if first_to_timeline else "")
                   + "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,ev]);window.__ev=ev;})()")
        await asyncio.sleep(1.0)
        # The same event again (a second relay, a reconnect's backlog) announces nothing new.
        await b.js("for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,__ev]); true")
        await asyncio.sleep(.6)
        out.update(await b.js(STATE))

    await desktop.with_browser("online", "", check, extra_init=RELAY + BUILD_PROBE)
    return out


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_mention_only_the_notifications_saw_rings():
    s = asyncio.run(arrive(False))
    assert "mentioned you" in s["toast"] and s["unread"] >= 1 and s["badge"], s


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_mention_the_timeline_stored_first_still_rings():
    s = asyncio.run(arrive(True))
    assert "mentioned you" in s["toast"], ("the timeline stored it first and the notification never popped", s)
    assert s["unread"] >= 1 and s["badge"], ("the timeline stored it first and the bell never lit", s)
    assert s["toast"].count("mentioned you") == 1, ("the same mention was announced twice", s)
