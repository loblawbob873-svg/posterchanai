"""A burst of notifications is announced as a couple of cards and ONE "N more", never a card each.

2026-10-13: "we need to fix the toaster flood when you open posterchan and it syncs notifications". Opening the
app, waking the machine or a relay connecting late hands the notifications subscription a run of events in the
same second, and `notifPing` popped a card, a chime and an OS notification for every one of them. Runs the SHIPPED
burst gate + `notifPing` under node with a fake clock: twelve arrivals in a second show two cards and then exactly
one summary card; an arrival after the burst has gone quiet is announced on its own again.
"""
import subprocess

from tests.client_source import client_source


def _shipped():
    src = client_source()
    b = src.index("  function notifPing(ev){")
    a = src.find("  const _BURST_MS=")
    a = b if a < 0 else a        # no gate at all: run notifPing as it stands, which is the failure
    end = src.index("\n  }\n", b) + 4
    return src[a:end]


HARNESS = r'''
const assert=require('node:assert/strict');
let now=1_000_000_000_000; Date.now=()=>now;
let timers=[]; global.setTimeout=(f,ms)=>{ timers.push({at:now+ms,f}); return timers.length; };
function advance(ms){ now+=ms; for(;;){ const due=timers.filter(t=>t.at<=now).sort((x,y)=>x.at-y.at)[0];
  if(!due) break; timers.splice(timers.indexOf(due),1); due.f(); } }
const toasts=[], os=[];
const S={LOGO:'logo.png', ME:{pubkey:'me'}};
const notifToast=(html)=>toasts.push(html), osNotify=(t,b)=>os.push(b);
const notificationAllowed=()=>true, isMutedAuthor=()=>false, _mutedThread=()=>false, isReply=()=>false;
const zapSender=()=>null, profOf=()=>({name:'alice'}), _tipNote=()=>null, _quotesMe=()=>false;
const fmtSats=x=>x, zapAmount=()=>0, reactDisp=()=>'+', emojiName=(pk,w)=>w, enc=x=>String(x);
const _notifCtxId=()=>null, openThread=()=>{}, switchView=()=>{};
SHIPPED
const ev=i=>({id:'e'+i, kind:1, pubkey:'p'+i, tags:[['p','me']], created_at:1});
for(let i=0;i<12;i++){ notifPing(ev(i)); advance(80); }
assert.equal(toasts.length, 2, 'a burst of 12 popped '+toasts.length+' cards before the summary');
advance(5000);
assert.equal(toasts.length, 3, 'the held arrivals were never summarised: '+JSON.stringify(toasts));
assert.match(toasts[2], /10 more notifications/, 'the summary miscounts: '+toasts[2]);
assert.equal(os.length, 3, 'OS notifications were not coalesced with the cards: '+os.length);
advance(60000);
notifPing(ev(99));
assert.equal(toasts.length, 4, 'a lone notification after the burst was swallowed');
assert.match(toasts[3], /alice/);
advance(10000);
assert.equal(toasts.length, 4, 'a summary was shown with nothing held');
'''


def test_a_burst_of_notifications_is_two_cards_and_one_summary():
    r = subprocess.run(["node", "-e", HARNESS.replace("SHIPPED", _shipped())],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr[-2000:]
