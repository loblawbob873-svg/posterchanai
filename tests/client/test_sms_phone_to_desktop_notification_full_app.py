"""A TEXT ARRIVING ON THE PHONE MUST RING ON THE DESKTOP — END TO END, WITH THE PHONE'S OWN EVENT.

Reported 2026-10-22: "I am not seeing any SMS notifications on OS and windows app ... that should have
had a regression test a long time ago for sms". Every desktop SMS-notification test before this one
fed the client a PLAINTEXT, JavaScript-shaped document (tests/client/sms_live_notifications.cjs) or
read the source (test_a_text_you_can_notice.py). Neither had ever seen what the handset actually
sends when a text arrives with PosterChan closed: the event `SmsOutbox.archiveIncoming` seals in
Java (NIP-44 v2 to the account's own key, BIP-340 signed) and `SignerRelayService` puts on its relay
sockets.

So the chain is driven from that real event:

1. tests/sms_java_event.py RUNS the shipped `SmsOutbox.archiveIncoming` on a JVM for the account the
   desktop logs in with (32 bytes of 0x01, desktop.login).
2. The SHIPPED bundled desktop client (desktop/build-www.sh, headless Chrome) logs in; its live
   Texts subscription is the one REQ carrying `#l: ["pcai-sms"]`, and the relay fixture delivers the
   Java event ON THAT SUBSCRIPTION, the way a relay delivers a live event — not by re-querying.
3. Asserted on every surface the desktop app has:
   * the message is in the Texts archive (decrypted, incoming, right body) and its conversation is
     in the Texts list;
   * `PC.osNotify` is called with the sender, the preview and `notificationType: 'sms'`;
   * the in-app toast shows;
   * and the notification reaches the surface that SHOWS it on each shell:
       - browser: `new Notification(sender, {body})`;
       - Windows/macOS/Linux desktop app (`window.pcShell` + `window.pcHost`, as desktop/preload.js
         exposes them): `pcHost.notify({title, body, route})` — the IPC main.js turns into a native
         notification;
       - the SAME desktop app with the windowed desktop layout on (`PCOS` on, not PosterChanOS) —
         THE BROKEN LINK: notifications went to the PosterChanOS card window, which main.js refuses
         outside --shell, so nothing at all was shown (no native notification, no in-window card);
       - PosterChanOS (`PCOS` on, --shell): the desktop's own card, through `pcToast.show` (the
         always-on-top toast window, desktop/toast-host.js) — the OS runs no notification daemon.
   The bridges answer exactly as desktop/main.js does; tests/test_desktop_windows_notification_identity.py
   runs those handlers so the stand-ins here cannot drift.
4. A text DELIVERED after the desktop started but STAMPED (by the carrier) before it still rings:
   freshness is the phone's own receipt time (`received`), not the service-centre stamp.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sms_java_event  # noqa: E402

SEC = "01" * 32
SENDER = "+15550142"
BODY = "Running 10 min late / see you at \"the place\""

pytestmark = [
    pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required'),
    pytest.mark.skipif(not sms_java_event.available(), reason='JDK + android.jar required'),
]


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.fixture(scope='module')
def java():
    b = sms_java_event.Builder()
    yield b
    b.close()


# Record every REQ the client opens so a LIVE event can be delivered on the open subscription, as a
# relay does; and stand in for each shell's notification surface with a recorder.
SPIES = r'''
window.__subs=[];
(()=>{const send=WebSocket.prototype.send;WebSocket.prototype.send=function(raw){
  try{const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='REQ')__subs.push({ws:this,id:m[1],filters:m.slice(2)});
      if(Array.isArray(m)&&m[0]==='CLOSE')__subs=__subs.filter(s=>!(s.ws===this&&s.id===m[1]));}catch(_){}
  return send.call(this,raw);};})();
window.__match=(f,ev)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
  &&(!f.ids||f.ids.includes(ev.id))&&(f.since==null||ev.created_at>=f.since)&&(f.until==null||ev.created_at<=f.until)
  &&Object.keys(f).filter(k=>k[0]==='#').every(k=>(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
window.__smsSubs=()=>__subs.filter(s=>s.ws.readyState===1&&s.filters.some(f=>(f['#l']||[]).includes('pcai-sms')));
window.__deliverLive=(ev)=>{let n=0;for(const s of __subs){if(s.ws.readyState!==1)continue;
  if(s.filters.some(f=>__match(f,ev))){s.ws.fire('message',['EVENT',s.id,ev]);n++;}}return n;};
window.__shown=[];
window.__cards=()=>Array.from(document.querySelectorAll('.notif-toast,.os-toast')).map(e=>e.textContent);
window.Notification=class{constructor(title,o){__shown.push({via:'Notification',title,body:(o||{}).body,tag:(o||{}).tag});}
  close(){} static get permission(){return 'granted'} static requestPermission(){return Promise.resolve('granted')}};
'''

NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
# What desktop/preload.js exposes in EVERY Electron window, answered the way desktop/main.js answers:
#   pc:host:notify -> a native Notification, EXCEPT under --shell (PosterChanOS), where it shows nothing;
#   pc:toast:show  -> the always-on-top card window (toast-host.js) ONLY under --shell, `false` otherwise.
# A recorder entry means the person would SEE something.
ELECTRON = r"""
window.pcShell.backgroundOwner=true;
window.pcHost={notify:async(o)=>{if(__shellMode)return false;__shown.push({via:'native',title:o.title,body:o.body,route:o.route,tag:o.tag});return true},
  onNotificationClick:(fn)=>()=>{}};
window.pcToast={show:async(o)=>{if(!__shellMode)return false;__shown.push({via:'toast-window',html:o.html});return true},onClick:(fn)=>{}};
"""
SHELLS = {
    # A browser tab on an instance: the Notifications API.
    'web': NO_OS + "window.__shellMode=false;delete window.pcPopup;",
    # The Windows/macOS/Linux desktop app in its ordinary single-column layout.
    'desktop-app': NO_OS + "window.__shellMode=false;" + ELECTRON,
    # The same desktop app with the windowed DESKTOP layout switched on (PCOS on, but NOT PosterChanOS):
    # main.js refuses pc:toast:show here, so a card handed to the toast window is a card nobody sees.
    'desktop-app-desktop-layout': "window.__shellMode=false;" + ELECTRON,
    # PosterChanOS: main.js --shell, a compositor answering pcWM, the desktop entered.
    'posterchanos': "window.__shellMode=true;" + ELECTRON,
}
OS_ON = ('desktop-app-desktop-layout', 'posterchanos')


async def _deliver_phone_text(b, java, when_offset_ms, shell):
    await desktop.login(b)
    if shell in OS_ON:
        await b.js("PCOS.isOn()||PCOS.enter()")
        await b.until("PCOS.isOn()")
    await b.until("!!window.PCSms && __smsSubs().length>0")
    await b.js("(()=>{window.__osCalls=[];window.__toasts=[];const P=__PC;"
               "const o=P.osNotify;P.osNotify=function(t,body,opts){__osCalls.push({title:t,body,opts:{...opts,onClick:typeof (opts||{}).onClick}});return o.apply(this,arguments)};"
               "const n=P.notifToast;P.notifToast=function(html){__toasts.push(String(html));return n.apply(this,arguments)};})()")
    # The text reaches the phone NOW -- after the desktop is up -- carrying the service centre's stamp
    # `when_offset_ms` from now; the phone's receiver records the moment it got it.
    now = int(time.time() * 1000)
    ev = java.event(SEC, SENDER, BODY, now + when_offset_ms, now)
    assert await b.js("__PC.me().pubkey") == ev['pubkey'], "fixture key mismatch"
    delivered = await b.js("__deliverLive(%s)" % json.dumps(ev))
    assert delivered >= 1, await b.js("__subs.map(s=>s.filters)")
    return ev


def _check(shell, when_offset_ms=0):
    async def check(b):
        java = check.java
        ev = await _deliver_phone_text(b, java, when_offset_ms, shell)
        d = ev['tags'][0][1]
        # (a) in the archive, decrypted from the Java ciphertext, and in the Texts list.
        await b.until("!!PCSms._state().msgs.get(%s)" % json.dumps(d))
        m = await b.js("(()=>{const m=PCSms._state().msgs.get(%s);return {address:m.address,body:m.body,incoming:m.incoming,date:m.date}})()" % json.dumps(d))
        assert abs(m.pop('date') - (time.time() * 1000 + when_offset_ms)) < 60_000, "the thread keeps the carrier's time"
        assert m == {'address': SENDER, 'body': BODY, 'incoming': True}, m
        # (b) osNotify with sender, preview and type.
        await b.until("__osCalls.length>0")
        call = (await b.js("__osCalls"))[0]
        assert call['title'] == SENDER and call['body'] == BODY, call
        assert call['opts']['notificationType'] == 'sms', call
        assert call['opts']['route'] == 'texts:' + SENDER.replace('+', '%2B'), call
        # (c) the in-app toast was raised...
        await b.until("__toasts.length>0")
        assert SENDER in (await b.js("__toasts"))[0]
        # (d) ...and something is actually SHOWN on this shell: the OS-level notification, and the
        # in-app card where this shell draws one.
        await b.until("__shown.length>0 || __cards().length>0")
        await asyncio.sleep(.5)
        shown, cards = await b.js("__shown"), await b.js("__cards()")
        if shell == 'web':
            assert any(s['via'] == 'Notification' and s['title'] == SENDER and s['body'] == BODY for s in shown), shown
            assert any(SENDER in c for c in cards), cards
        elif shell in ('desktop-app', 'desktop-app-desktop-layout'):
            # The platform notification -- the only thing seen with the window minimised or covered.
            assert any(s['via'] == 'native' and s['title'] == SENDER and s['body'] == BODY
                       and s['route'] == call['opts']['route'] for s in shown), (shown, cards)
            assert any(SENDER in c for c in cards), (shown, cards)
        else:
            assert any(s['via'] == 'toast-window' and SENDER in s['html'] for s in shown), shown
        # And the conversation is listed in Texts.
        await b.js("__PC.switchView('texts')")
        await b.until("!!document.querySelector('.sms-wrap') && document.querySelector('.sms-wrap').textContent.includes(%s)"
                      % json.dumps(SENDER))
    return check


@pytest.mark.parametrize('shell', list(SHELLS))
def test_a_text_the_phone_archived_rings_on_every_desktop_shell(shell, java):
    check = _check(shell)
    check.java = java
    asyncio.run(desktop.with_browser('online', '', check, SPIES + SHELLS[shell]))


def test_a_text_delivered_late_still_rings(java):
    """THE CARRIER'S STAMP IS NOT WHEN THE TEXT ARRIVED. A phone out of signal, a slow carrier or a
    service centre stamping in the wrong zone delivers a text NOW carrying a time from before this
    desktop started listening. It used to be filed in Texts without a sound, because "is this new?"
    was judged by that stamp; the phone now records when IT received the text, and that decides."""
    check = _check('desktop-app', when_offset_ms=-20 * 60 * 1000)
    check.java = java
    asyncio.run(desktop.with_browser('online', '', check, SPIES + SHELLS['desktop-app']))


if __name__ == '__main__':
    sys.exit(pytest.main([os.path.abspath(__file__), '-q']))
