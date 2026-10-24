"""A message you send never lands ABOVE the last message in the room.

Reported 2026-10-23: "sometimes my message goes above the last reply in the room". A room is ordered by the time each
SENDER's device stamped its message, and phones' clocks disagree by seconds. Answer somebody whose clock runs ahead of
yours, quickly, and your reply was stamped earlier than the message it answers -- so every client, Armada included,
put it above that message. Nothing on the wire can correct another device's clock; what the sender CAN do is never
stamp a message earlier than the newest one it has already seen in that channel.

The shipped client: the room's last message comes from a device 30 s ahead; you send through the real composer; your
message must be LAST -- while it is sending and after it is sent -- and the stamp handed to the reader must not be
earlier than that message. A message from a device far in the FUTURE (a broken clock, or one forged) must not drag
your stamps forward with it.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

# The reader stand-in honours the floor exactly as the real one does (createChatWrap: max(now, notBefore)); the
# real reader's half is tests/client/concord_send_stamp_runtime.mjs.
STUB_FLOOR = ("createChatWrap:async(_bundle,_wraps,_channel,text,owner,sign,tags,kind)=>{",
              "createChatWrap:async(_bundle,_wraps,_channel,text,owner,sign,tags,kind,notBefore)=>{"
              "const ms=Math.max(Date.now(),Number(notBefore)||0);")


def _room(ahead_ms):
    r = ROOM.replace("text:'hello from bob',at:Date.now()-60000", "text:'hello from bob',at:(window.__bobAt=window.__bobAt||Date.now()+%d)" % ahead_ms)   # stamped ONCE, like a real message
    r = r.replace(*STUB_FLOOR)
    r = r.replace("__wraps.push({text,tags,kind});\n      return{rumorId:sealed.id,wrap:{...sealed,kind:1059},ms:Date.now()};",
                  "__wraps.push({id:sealed.id,text,tags,kind,notBefore,ms});\n      return{rumorId:sealed.id,wrap:{...sealed,kind:1059},ms};")
    assert "notBefore,ms" in r and "__bobAt" in r, "the fixture moved; this test is not testing"
    return r


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ORDER = """[...document.querySelectorAll('.cc-message')].map(m=>m.textContent.includes('hello from bob')?'bob':
  (m.textContent.includes('my answer')?'me':'?')).filter(x=>x!=='?')"""


async def _send(b, text):
    await b.js("(()=>{const i=document.querySelector('#cc-input');i.focus();i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));})()"
               % json.dumps(text))
    await b.js("document.querySelector('#cc-send').click()")


def _run(ahead_ms):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 850, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("window.__publishOK=true")
        await b.js(_room(ahead_ms))
        await b.until("!!document.querySelector('#cc-input')")
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('hello from bob'))")
        await _send(b, "my answer")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('my answer'))")
        got["while_sending"] = await b.js(ORDER)
        await b.until("__wraps.some(w=>w.text==='my answer')")
        await asyncio.sleep(.8)
        got["after_sent"] = await b.js(ORDER)
        # …and after the room's next refresh, which re-sorts the channel by the stamps -- the moment it used to jump.
        # The fixture's reader now also hands back what was sent, as another device would see it.
        await b.js("""(()=>{const r=window.PosterCordReader,old=r.inspectChat;r.inspectChat=async(...a)=>{const got=await old(...a);
          const mine=__wraps.filter(w=>w.text==='my answer').map(w=>({id:w.id,pubkey:__PC.me().pubkey,text:w.text,at:w.ms,kind:9,tags:[]}));
          return {...got,messages:[...got.messages,...mine]};};})()""")
        await b.js("void PCConcord.refreshActiveChannel(__PC)")
        await asyncio.sleep(1.5)
        got["after_refresh"] = await b.js(ORDER)
        got["sent"] = await b.js("__wraps.find(w=>w.text==='my answer')")

    classic = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
               "||'{}'),osMode:false}));")
    asyncio.run(desktop.with_browser("online", "", check, classic))
    return got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_reply_to_a_device_whose_clock_is_ahead_still_lands_below_it():
    got = _run(30_000)
    assert got["while_sending"] == ["bob", "me"], ("while sending, your message sat above the one it answers", got)
    assert got["after_sent"] == ["bob", "me"], ("once sent, your message sorted above the one it answers", got)
    assert got["after_refresh"][-1] == "me" and got["after_refresh"].index("bob") < got["after_refresh"].index("me"), \
        ("after the room refreshed, your message jumped above the one it answers", got)
    sent = got["sent"]
    assert sent and sent.get("notBefore") and sent["ms"] >= sent["notBefore"], ("the send was not given a floor", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_message_from_a_broken_far_future_clock_does_not_drag_your_stamps_forward():
    got = _run(3 * 24 * 3600 * 1000)   # three days ahead: a broken clock, or a forged stamp
    sent = got["sent"]
    assert sent, got
    import time
    assert abs(sent["ms"] - time.time() * 1000) < 120_000, ("one bad clock in the room pushed your messages into the future", got)
