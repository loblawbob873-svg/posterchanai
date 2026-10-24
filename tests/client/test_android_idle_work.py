"""What the app costs a phone while nobody is touching it (2026-10-21 Android audit + measurement).

Measured on an emulated Pixel 5 against poster.place: ~2 MB/min of pictures still downloading while hidden,
a Texts poll asking the relay every 3 s on every screen, a Concord loop that never stopped once Communities had
been opened, every return to the app re-downloading its notification and DM backlog, and full-size originals
stored for good under thumbnail addresses. Each rule below is driven against the SHIPPED code where it can be
run outside a phone (relay.js, sw.js), and pinned at its source where it cannot (the WebView-bound modules);
the browser half is tests/client/test_apk_background_goes_quiet_full_app.py.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/client"


def _node(script):
    r = subprocess.run(["node", str(ROOT / script)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr


def test_a_reconnect_asks_only_for_what_is_new():
    _node("tests/client/relay_resume_since_runtime.mjs")


def test_a_redirected_thumbnail_is_never_cached_as_the_thumbnail():
    _node("tests/client/sw_thumb_redirect_runtime.mjs")


def test_the_texts_outbox_rides_the_live_subscription_not_a_three_second_query():
    sms = (JS / "sms.js").read_text()
    live = sms[sms.index("_sub = Relay().subscribe([f], { live:true, onEvent:"):][:1500]
    assert "if(d.startsWith(D_OUT)) phoneState().then(" in live and "drainOutbox()" in live, \
        "a send request arriving on the live subscription must drain the outbox"
    poll = sms[sms.index("const outboxStep = async () => {"):][:1500]
    assert "(++outboxTick % 20)" in poll, "the relay query + native reads must be a once-a-minute net, not every 3 s"
    assert poll.index("watch();") < poll.index("(++outboxTick % 20)"), "watch() (no network) still runs every tick"
    # …and no 3 s timer at all while hidden (2026-10-23: 20 wake-ups a minute on a backgrounded phone).
    assert "outboxPoll = setInterval(outboxStep, 3000)" in poll
    assert "if(document.hidden){ if(outboxPoll){ clearInterval(outboxPoll); outboxPoll = null; } return; }" in poll


def test_the_concord_loops_rest_while_the_relays_sleep_and_off_screen():
    c = (JS / "concord.js").read_text()
    start = c[c.index("function startLiveSync(p){"):][:400]
    assert start.index("if(liveResting())return;") < start.index("refreshRoomMetadata(p)")
    assert "if(liveOffScreen()&&(++liveTick%5))return;" in start
    assert "Relay.asleep()" in c[c.index("const liveResting="):][:200]
    sweep = c[c.index("const sweepTimer=setInterval("):][:300]
    assert "document.hidden" in sweep and "liveResting()" in sweep, "the badge sweep must not run hidden or asleep"


def test_the_online_heartbeat_slows_where_the_counter_is_not_drawn():
    app = (JS / "app.js").read_text()
    line = next(l for l in app.splitlines() if "updateUserCount(true); }, 15000)" in l)
    assert "!uc.offsetParent" in line and "(++_ucT % 4)" in line
    # The server counts a viewer for 150 s; the slowed heartbeat (60 s) must stay inside it.
    srv = (ROOT / "app/routers/client.py").read_text()
    assert float(re.search(r"^_VIEWER_WINDOW = ([0-9.]+)", srv, re.M).group(1)) > 60 * 2


def test_gifs_go_through_the_resizer():
    app = (JS / "app.js").read_text()
    body = app[app.index("window.PCThumb = (function(){"):][:1600]
    assert "/\\.svg(?:[?#]|$)/i.test(u)" in body and "gif|svg" not in body


def test_the_sign_in_art_is_not_a_megabytes_png():
    html = (ROOT / "templates/client.html").read_text()
    assert "/static/signon-nostr-city.webp" in html and "signon-nostr-city.png" not in html
    art = ROOT / "static/signon-nostr-city.webp"
    assert art.exists() and art.stat().st_size < 400_000, "the sign-in art must stay small (was 2.5 MB)"


def test_the_music_notification_is_told_about_changes_not_seconds():
    mp = (JS / "musicplayer.js").read_text()
    tick = mp[mp.index("const sec=Math.floor((S._audioEl&&S._audioEl.currentTime)||0);"):][:1600]
    assert "this._media()" in tick.split("if(sig!==this._pushSig")[1][:400], "a change must still reach the OS"
    assert "Math.abs(pos-expect)>2" in tick and "now-(this._pushAt||0)>30000" in tick, "seek + 30 s resync"
    assert tick.count("this._media()") == 1, "the per-second path must not push unconditionally"
    # Android interpolates the bar itself only because the service hands it a speed + timestamp.
    svc = (ROOT / "mobile/android/app/src/main/java/place/poster/app/music/MusicService.java").read_text()
    assert "playing ? 1f : 0f, SystemClock.elapsedRealtime()" in svc


def test_on_mobile_data_thumbnails_are_asked_for_at_1_5x():
    import json
    script = r"""
const fs=require('fs'),vm=require('vm');const src=fs.readFileSync(process.argv[1],'utf8');
const a=src.indexOf('window.PCThumb = (function(){'),b=src.indexOf('})();',a)+5;const out={};
for(const [k,conn] of [['wifi',{type:'wifi',effectiveType:'4g'}],['cell',{type:'cellular',effectiveType:'4g'}],['save',{type:'wifi',saveData:true}],['slow',{effectiveType:'3g'}],['none',null]]){
  const window={devicePixelRatio:2.75,__PC_API_BASE__:'https://poster.place'};const ctx={window,location:{},navigator:conn?{connection:conn}:{}};
  vm.runInNewContext(src.slice(a,b),ctx);out[k]=window.PCThumb.src('https://h.example/p.jpg',100);}
console.log(JSON.stringify(out));"""
    r = subprocess.run(["node", "-e", script, str(JS / "app.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert "w=275&" in o["wifi"] and "w=275&" in o["none"], o
    for k in ("cell", "save", "slow"):
        assert "w=150&" in o[k], (k, o)


def test_the_sms_archive_retry_backs_off():
    j = (ROOT / "mobile/android/app/src/main/java/place/poster/app/signer/SignerRelayService.java").read_text()
    retry = j[j.index("private void scheduleSmsHistoryRetry()"):][:500]
    assert "Math.min(smsHistoryRetryMs * 2, 600000L)" in retry and "}, delay);" in retry
    assert "smsHistoryRetryMs = 10000L;   // relays are answering again" in j
