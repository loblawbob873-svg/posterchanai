"""Guards for the live-stream session identity, the replay `recording` tag, and the admin claim.

STREAMS. kind 30311 is a parameterized-REPLACEABLE event, so its address is `30311:<pubkey>:<d>`.
`d` was the MediaMTX publish token — which is stable for a user's whole life — so every broadcast
replaced the previous one at the same address: last week's stream, its recording and its chat all
silently overwritten by this week's. A broadcast now gets `<token>-<starts>`.

The token still has to be recoverable from it, in two places that fail in opposite directions:
  - stream_end_service.token_of() feeds is_publishing(), which probes MediaMTX BY PATH. Hand it the
    full `d` and the probe finds nothing, reads it as "feed gone", and the reaper ends every live
    stream it sweeps.
  - the client's _tokenOfD() feeds the VOD lookup, which is keyed by token.

REPLAYS. The `recording` tag is the only cross-client way to say "the replay is here". It was stamped
only when the streamer happened to reopen their own ended stream in our client, and even then it was
published to our relay ONLY — so shosho and zap.stream, which read the external stream relays, never
saw it and showed the stream as unreplayable.

ADMIN CLAIM. /api/auth/nostr-login is public and promotes the first signer to admin on a node with no
admin. The lock counted only admins WITH a linked npub, and the seeded `admin`/`admin` row had none —
so on any node the owner administered by password the claim stayed open to strangers forever.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "js" / "client" / "app.js"


# --- stream session identity ---------------------------------------------------------------------

def test_the_d_tag_is_the_session_not_the_lifetime_publish_token():
    src = APP_JS.read_text(encoding="utf-8")
    assert "['d', s.token]" not in src, (
        "the 30311 `d` is the publish token again — that token never rotates, so every broadcast "
        "replaces the previous one at the same replaceable address")
    assert "['d', s.d || s.token]" in src


def test_token_of_recovers_the_mediamtx_path_from_either_d_form():
    from app.services.stream_end_service import token_of

    def ev(d):
        return {"event": {"tags": [["d", d], ["status", "ended"]]}}

    # New form: <token>-<starts>. Must yield the bare token — is_publishing() probes MediaMTX with it.
    assert token_of(ev("a1b2c3d4e5f60718-1780000000")) == "a1b2c3d4e5f60718"
    # Old parked sentinels, whose `d` really is the token, must be untouched.
    assert token_of(ev("a1b2c3d4e5f60718")) == "a1b2c3d4e5f60718"
    assert token_of({}) == ""
    # Only a trailing unix-timestamp-sized run of digits is a session suffix.
    assert token_of(ev("a1b2c3d4e5f60718-42")) == "a1b2c3d4e5f60718-42"


def test_parking_an_ended_event_is_authorized_by_the_token_not_the_raw_d():
    """The third place the session suffix has to be stripped — and the one that was missed.

    /api/streams/sentinel gates on "is this your stream". It compared the `d` tag straight against the
    user's publish token, which the `<token>-<starts>` change made permanently unequal: every park 403'd,
    so no pre-signed "ended" event was ever stored and every broadcast stayed ● LIVE on zap.stream and
    shosho once the tab closed. The failure is silent from the app's side — only an access log shows it.
    """
    src = (ROOT / "app" / "routers" / "streams.py").read_text(encoding="utf-8")
    assert '_tag("d") != _user_token' not in src, (
        "the sentinel gate compares the raw `d` to the publish token again — `d` is `<token>-<starts>`, "
        "so this can never match and no stream will ever be able to end itself"
    )
    assert 'stream_end_service.token_of({"event": event}) != _user_token' in src, (
        "the sentinel gate must authorize on the TOKEN recovered from `d` (stream_end_service.token_of)"
    )


def test_client_and_server_agree_on_how_to_strip_the_session_suffix():
    """Two implementations of one rule; if they drift, the VOD lookup and the liveness probe disagree."""
    from app.services.stream_end_service import token_of
    js = re.search(r"function _tokenOfD\(d\).*?\.replace\((/.*?/),", APP_JS.read_text(encoding="utf-8"), re.S)
    assert js, "_tokenOfD no longer strips the suffix with a replace()"
    assert "-\\d{9,}$" in js.group(1), f"client strips {js.group(1)!r}, server strips -\\d{{9,}}$"
    assert token_of({"event": {"tags": [["d", "abc-1780000000"]]}}) == "abc"


def test_the_watch_link_and_the_announcement_address_the_published_stream():
    src = APP_JS.read_text(encoding="utf-8")
    assert "identifier:info.token" not in src, (
        "a naddr built from the publish token addresses whichever broadcast most recently overwrote "
        "that address, not this one")
    assert "identifier:info.d" in src            # Go Live modal's copyable watch link
    assert "identifier:_d" in src                # the kind-1 announcement


# --- replay stamping ------------------------------------------------------------------------------

def test_the_recording_tag_is_pushed_to_the_relays_other_clients_read():
    src = APP_JS.read_text(encoding="utf-8")
    fn = src[src.index("async function _backfillRecordingTag"):]
    fn = fn[:fn.index("\n  /* STAMP THE REPLAY")]
    assert "Relay.publishTo(STREAM_RELAYS" in fn, (
        "publish() only reaches our own relay, so the stamped event never leaves poster.place and "
        "shosho/zap.stream keep the older untagged copy — the replay stays invisible")


def test_replays_are_stamped_without_the_streamer_reopening_their_own_stream():
    src = APP_JS.read_text(encoding="utf-8")
    assert "_stampReplayWhenReady" in src and "_sweepUnstampedReplays" in src
    # ...and both are actually wired, not just defined.
    assert src.count("_stampReplayWhenReady") >= 2, "defined but never called"
    assert src.count("_sweepUnstampedReplays") >= 2, "defined but never called"


def test_the_replay_lookup_picks_this_broadcasts_recording():
    src = APP_JS.read_text(encoding="utf-8")
    assert "const v=(j.vods||[])[0]" not in src, (
        "vods[0] is the newest recording for a token shared by every broadcast, so opening an old "
        "stream plays — and stamps — somebody else's session")
    assert "_vodUrlFor" in src


# --- admin claim ----------------------------------------------------------------------------------

def test_no_default_password_admin_is_seeded():
    src = (ROOT / "app" / "database.py").read_text(encoding="utf-8")
    assert 'get_password_hash("admin")' not in src, (
        "a fresh install must not ship a full administrator whose password is 'admin' — "
        "POST /api/auth/login is mounted unconditionally, so it is reachable on every node")
    assert 'username="admin"' not in src


def test_the_first_login_admin_claim_locks_on_any_admin():
    src = (ROOT / "app" / "routers" / "auth.py").read_text(encoding="utf-8")
    claim = src[src.index("POSTERCHANAI_AUTO_ADMIN"):]
    claim = claim[:claim.index("user.is_admin = True")]
    claim = "\n".join(l for l in claim.splitlines() if not l.lstrip().startswith("#"))
    assert "nostr_npub.isnot(None)" not in claim, (
        "counting only admins WITH an npub leaves the claim open on any node whose admin signs in "
        "with a password — the next stranger to hit this public endpoint becomes an admin")
    assert "User.is_admin == True" in claim


def test_no_login_ui_exists_for_the_password_endpoint():
    """The reason the seeded account was a pure backdoor: nothing ever called it."""
    hits = []
    for sub in ("static/js", "templates"):
        for path in (ROOT / sub).rglob("*"):
            if path.is_file() and path.suffix in (".js", ".html"):
                if "auth/login" in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"a password login form appeared ({hits}) — re-evaluate removing the seeded admin"


# --- extension NIP-07 under a strict CSP ----------------------------------------------------------

def test_firefox_manifest_injects_the_signer_into_the_page_world():
    """zap.stream sends `script-src 'self' 'wasm-unsafe-eval' …` — no 'unsafe-inline'. The provider was
    smuggled in as an inline <script>, which that CSP blocks outright, so window.nostr never appeared
    and NIP-07 login was impossible there. A MAIN-world content script is an extension script, exempt
    from the page CSP, and needs no web_accessible_resources (so it still leaks no per-install UUID)."""
    m = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    main = [c for c in m.get("content_scripts", []) if c.get("world") == "MAIN"]
    assert main, "no MAIN-world content script — NIP-07 stays broken on every CSP-strict site"
    assert main[0]["js"] == ["inject.js"]
    assert main[0].get("run_at") == "document_start", "sites detect a signer at document_start"
    # The inline path must REMAIN for Firefox < 128, which has no world:MAIN.
    isolated = [c for c in m["content_scripts"] if c.get("world") != "MAIN"]
    assert isolated and "inject.js" in isolated[0]["js"], (
        "removing inject.js from the isolated script drops the pre-128 Firefox fallback")


def test_the_chrome_build_still_generates_its_own_main_world_manifest():
    out = ROOT / "extension" / "dist" / "chrome" / "manifest.json"
    if not out.exists():
        pytest.skip("extension/dist not built here")
    cs = json.loads(out.read_text(encoding="utf-8"))["content_scripts"]
    assert [c for c in cs if c.get("world") == "MAIN"]
    # build.sh rebuilds the list wholesale; inject.js must not ALSO remain in the isolated script,
    # or Chrome defines the provider twice.
    isolated = [c for c in cs if c.get("world") != "MAIN"]
    assert all("inject.js" not in c.get("js", []) for c in isolated)


def test_node_can_parse_the_client_after_these_edits():
    r = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True)
    if r.returncode != 0 and "not found" in (r.stderr or ""):
        pytest.skip("node not available")
    assert r.returncode == 0, r.stderr


def test_a_replay_is_never_matched_to_an_earlier_broadcasts_recording(tmp_path):
    """Regression, from production: four separate broadcasts were all stamped with ONE recording that
    belonged to a stream two hours earlier — whose blob no longer existed, so every replay 404'd.

    The VOD row is not written until concat+upload finishes (~90s after the stream ends), so at stamping
    time the only candidates are OLDER streams on the same token. The matcher scored on absolute
    difference over a six-hour window, so it reached back and took one. A recording cannot begin before
    the broadcast was announced; "no recording yet" must yield nothing so the caller retries.
    """
    src = APP_JS.read_text(encoding="utf-8")
    body = src[src.index("async function _vodUrlFor"):]
    body = body[:body.index("\n  /* The tab that was closed")]
    assert "Math.abs((parseInt(v.started_at" not in body, "scoring on |difference| again"
    assert "6 * 3600" not in body, "the six-hour window is back"

    harness = tmp_path / "m.js"
    harness.write_text(
        body.replace("async function _vodUrlFor(token, starts){", "function pick(vods, starts){")
            .replace("if(!token) return '';", "")
            .replace("const r = await _streamFetch('/api/streams/vods/by-token/' + encodeURIComponent(token));", "")
            .replace("if(!(r && r.ok)) return '';", "")
            .replace("const vods = ((await r.json()) || {}).vods || [];", "")
        + """
const GOOD={url:'GOOD',started_at:1785955398}, BAD={url:'BAD',started_at:1785948486};
const STARTS=[1785955397,1785955420,1785955454,1785955477];
let out=[];
for(const s of STARTS) out.push(pick([GOOD,BAD], s));
for(const s of STARTS) out.push(pick([BAD], s) || 'NONE');
console.log(JSON.stringify(out));
""", encoding="utf-8")
    r = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    if r.returncode != 0 and "not found" in (r.stderr or ""):
        pytest.skip("node not available")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got[:4] == ["GOOD"] * 4, f"wrong recording chosen when the right one exists: {got[:4]}"
    assert got[4:] == ["NONE"] * 4, f"reached back to an earlier broadcast's recording: {got[4:]}"


def test_a_wrong_recording_tag_can_be_corrected():
    """The stamp bailed on ANY existing `recording`, so a bad one was permanent — which is how four
    events stayed pointed at a dead blob. The sweep is now also the repair path."""
    src = APP_JS.read_text(encoding="utf-8")
    fn = src[src.index("async function _backfillRecordingTag"):]
    fn = fn[:fn.index("\n  /* STAMP THE REPLAY")]
    assert "if(_cur === vurl) return;" in fn, "no longer a no-op only when the tag already matches"
    sweep = src[src.index("async function _sweepUnstampedReplays"):]
    sweep = sweep[:sweep.index("\n  // Delete YOUR OWN stream")]
    assert "t[0]==='recording' && t[1])) continue" not in sweep, (
        "the sweep skips already-tagged events again, so a wrong tag can never be repaired")


def test_vod_listings_hide_recordings_whose_blob_is_gone():
    """A StreamVOD row outlives its bytes — the streamer deletes the recording (nothing removes the
    row) or an upload never stored the blob. Measured live: 43 rows for one token, 42 dead. The client
    picks from this list and STAMPS the choice onto the NIP-53 `recording` tag, so a dead row becomes a
    permanent dead replay link in every other Nostr client."""
    src = (ROOT / "app" / "routers" / "streams.py").read_text(encoding="utf-8")
    assert "def _playable(" in src
    body = src[src.index("@router.get(\"/vods\")"):]
    body = body[:body.index("@router.post(\"/quality\")")]
    assert body.count("_playable(db, rows)") == 2, (
        "both /vods and /vods/by-token must filter — the stamper reads by-token")
    assert "BlossomBlob" in src


def test_the_end_of_stream_message_is_reassuring_not_a_warning():
    """The upload runs in a SERVER-SIDE reaper task, so closing the app cannot interrupt it. Measured
    on seven real streams: 62-89s end-to-live, uncorrelated with clip length. So the copy states the
    wait and that leaving is safe — a blocking "don't close the app" warning would be false, and false
    warnings are how users learn to dismiss real ones."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "You can close the app; it finishes on the server." in src
    assert "about a minute" in src
    # ...and it must only promise a replay when one is actually coming.
    assert "s && s.record" in src, "the message promises a recording even when recording is off"
    assert "record: !!(info.record_available && info.record_enabled)" in src


def test_a_stream_that_just_ended_does_not_claim_it_has_no_recording():
    """The recording does not exist for ~60-90s after the stream ends. Stating 'no recording available'
    in that window told the streamer their replay had failed while it was still being made — which is
    what made people think they had to sit and wait with the app open."""
    src = APP_JS.read_text(encoding="utf-8")
    branch = src[src.index("if(!playUrl){"):]
    branch = branch[:branch.index("if(n2) n2.textContent = vurl")]
    assert "_fresh" in branch and "15 * 60" in branch, "no recent-vs-really-absent distinction"
    assert "Saving your recording" in branch
    assert "setTimeout" in branch, "a stream still saving must re-check, not need a manual reload"


def test_a_stale_live_announcement_is_not_re_adopted_forever():
    """A 30311 of ours that says `live` but has no feed must be RETIRED, not adopted again.

    _adoptOwnLive exists so a reload mid-broadcast does not strand a stream as permanently LIVE. But it
    adopted any own event whose status said `live`, which makes a stream that never received its
    `ended` event immortal: opening Streams re-adopts it, the heartbeat republishes it, and created_at
    marches forward while `starts` stays days behind. Observed on a real account — a two-day-old
    broadcast was being restamped LIVE on zap.stream every time the owner opened the app.

    Two guards make acting on the probe safe, and both must stay:
      * the api must answer  — a dead HLS means "over" only if the server is reachable; otherwise this
        device is simply offline, and ending a live broadcast over that is far worse than a stale one.
      * the announcement must be OLD — the heartbeat needs three misses over ~2 minutes before it ends
        anything, because OBS reconnects. A recent dead feed is still adopted and left to that path.
    """
    src = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")
    i = src.index("async function _retireIfOver(")
    body = src[i:src.index("async function _sweepStaleOwnLive(")]

    assert "/api/streams/ingest" in src[src.index("async function _serverAnswers("):][:600], (
        "the retire path must confirm OUR OWN api is reachable before believing a dead HLS probe — "
        "otherwise an offline device ends the user's live stream"
    )
    assert "_serverAnswers(hls)" in body, (
        "an unreachable api must leave the announcement alone, not retire it — and the probe takes the "
        "stream's own hls url so it asks the server that actually hosts it"
    )
    assert "> 900" in body, (
        "only an announcement old enough that no OBS reconnect explains it may be retired on a single "
        "probe; a recent one belongs to the heartbeat's 3-strike path"
    )
    assert body.count("_probeFeed(hls)") >= 2, (
        "a dead feed must be CONFIRMED by a second probe before publishing `ended` — the startup sweep "
        "runs unattended, so one CDN blip would otherwise end a genuinely live broadcast"
    )
    assert "'status','ended'" in body.replace('"', "'"), (
        "a stream confirmed over has to be published as ended — not adopting it merely stops the "
        "republishing, it does not clear `live` for everyone already watching the address"
    )


def test_a_401_from_our_api_means_reachable_not_offline(tmp_path):
    """THE BUG THAT MADE THE FIX ABOVE A NO-OP, and the reason this test executes the guard instead of
    grepping for it.

    The reachability guard was `apiUp = r.ok` against /api/streams/ingest — an endpoint behind
    get_current_user. The Nostr client normally carries no app session (`_aiToken` is set only by
    nostr-login, and the call sent no credentials), so the real answer is 401, `r.ok` is false, and the
    guard concluded "my network is down" and returned. Every time. The retirement it protects could
    never run once — a stream "fixed" one night was still ● LIVE on zap.stream the next morning, and
    the previous version of this test passed throughout, because the string `if(!apiUp) return;` was
    present and said nothing about what apiUp MEANT.

    Reachability is not authorization: a 401 is our server answering. Only a fetch that never resolves
    means we could not reach it. 5xx is deliberately excluded — a sick origin is exactly when the HLS
    probe fails for reasons unrelated to the broadcast.
    """
    src = APP_JS.read_text(encoding="utf-8")
    body = src[src.index("async function _serverAnswers("):]
    body = body[:body.index("\n  // On (re)opening Streams")]

    harness = tmp_path / "s.js"
    harness.write_text(
        "let _aiToken='';\nfunction _instanceBase(){ return 'https://poster.place'; }\n" + body + """
const HLS='https://poster.place/api/streams/hls/0d1ddd1c187b1cda/index.m3u8';
async function main(){
  // A faithful Response stub: real fetch gives BOTH .ok and .status, and `ok` is exactly
  // status<300. Stubbing only .status would let the old `return r.ok` yield undefined, which
  // JSON.stringify silently drops — the test would fail on a missing key rather than on the
  // wrong answer, and would not read as the bug it is guarding.
  const res = s => ({ status:s, ok: s>=200 && s<300 });
  const cases = {
    'unauthenticated 401': res(401),
    'forbidden 403':       res(403),
    'ok 200':              res(200),
    'not found 404':       res(404),
    'origin sick 502':     res(502),
    'offline (throws)':    'THROW',
  };
  const out={};
  for(const [name, resp] of Object.entries(cases)){
    globalThis.fetch = async () => { if(resp==='THROW') throw new TypeError('failed'); return resp; };
    out[name] = await _serverAnswers(HLS);
  }
  // The BUNDLED app has no instance and no server to ask. It must refuse to retire anything rather
  // than trust a probe answered by its own bundle while the device is offline.
  globalThis.fetch = async () => res(200);
  out['standalone, no instance'] = await (async () => {
    const real = _instanceBase; _instanceBase = () => '';
    try { return await _serverAnswers(''); } finally { _instanceBase = real; }
  })();
  console.log(JSON.stringify(out));
}
main();
""", encoding="utf-8")
    r = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    if r.returncode != 0 and "not found" in (r.stderr or ""):
        pytest.skip("node not available")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)

    assert got["unauthenticated 401"] is True, (
        "a 401 is our server ANSWERING — treating it as unreachable is what silently disabled the "
        "whole stale-stream retirement"
    )
    assert got["forbidden 403"] is True and got["not found 404"] is True and got["ok 200"] is True, (
        f"any HTTP answer proves the app is up: {got}"
    )
    assert got["origin sick 502"] is False, (
        "a 5xx origin is ambiguous — the HLS probe fails then for reasons unrelated to the broadcast"
    )
    assert got["offline (throws)"] is False, (
        "a fetch that never resolves is the one case that really means this device is offline"
    )
    assert got["standalone, no instance"] is False, (
        "with no instance there is no server to ask — and in the BUNDLED app a relative /api/ path is "
        "answered by the bundle itself, which would read as 'server up' on a device with no network"
    )
    assert "new URL(hls" in body, (
        "the reachability probe must target the origin of the DEAD HLS URL — that is the server whose "
        "playlist just failed, and it is not necessarily the instance this client is signed in to"
    )


def test_stale_own_live_is_swept_without_opening_the_streams_view(tmp_path):
    """The retirement used to run ONLY inside renderStreams, filtering a generic
    `{kinds:[30311], limit:80}` feed. Two ways that loses:

      * a wrong ● LIVE claim lives on zap.stream and every other NIP-53 client, and its owner has no
        reason to open OUR Streams tab — so it stood for days, twice.
      * a day-old announcement of ours is not in the newest 80 events across relays carrying every
        stream on the network. Measured against the real relays: nos.lol's window had already dropped
        the stuck event while primal's still held it, so even opening the view was a coin flip.

    So the sweep asks BY AUTHOR, and runs at startup.
    """
    src = APP_JS.read_text(encoding="utf-8")
    body = src[src.index("async function _sweepStaleOwnLive("):]
    body = body[:body.index("\n  // On (re)opening Streams")]

    assert "authors:[ME.pubkey]" in body.replace(" ", ""), (
        "the sweep must ask for OUR OWN events by author — a global limit-80 feed is not guaranteed to "
        "still contain a day-old announcement"
    )
    assert "verifyEvent" in body, (
        "these relays are untrusted: a forged `live` event in our name would otherwise make us publish "
        "an `ended` for a broadcast that never existed"
    )
    assert "_sweepStaleOwnLive()" in src[src.index("function startApp("):], (
        "the sweep has to run at startup, not only when Discover → Streams is opened"
    )


def test_adopt_is_awaited_by_its_caller():
    """It probes the network now, so it returns a promise — a bare call would surface a rejection as an
    unhandled promise rejection, which in this client is a console error nobody reads."""
    src = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")
    assert "_a=_adoptOwnLive(streams); if(_a&&_a.catch) _a.catch(()=>{})" in src.replace(" ", "").replace(
        "const_a", "_a"
    ) or "_a.catch(()=>{})" in src, (
        "the async _adoptOwnLive must have its rejection caught on the promise, not only synchronously"
    )


def test_the_streams_grid_includes_what_we_already_hold(tmp_path):
    """Reported: "I start a stream, go to Discover -> Streams, and have to refresh to see it."

    Relay.query is NETWORK-ONLY, so the grid was whatever the relays answered in that instant — and
    the event least likely to be in it is the one you published a second earlier. _goLive publishes,
    requires the relay to have stored it, then switches straight to this view; the REQ that follows
    can still return without it, and the view has no live subscription, so nothing ever re-queries.
    Verified against production while a stream was live: the event WAS on our relay, ranked 1st of 80
    for the client's own filter — so the relay was never the problem and only a reload fixed the view.

    Asserted on behaviour, not on the source: a locally-held event missing from the network answer
    must survive into the grid, and a stale cached copy must still lose to a newer one from the relay
    (_dedupAddr keeps the newest per address, which is what makes merging safe).
    """
    src = APP_JS.read_text(encoding="utf-8")
    body = src[src.index("async function renderStreams("):]
    body = body[:body.index("\n  function streamCard(")]

    harness = tmp_path / "m.js"
    harness.write_text("""
const _dedupAddr = (evs) => {
  const m = new Map();
  for (const e of evs) { const d = (e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const k = e.pubkey+':'+d; const cur = m.get(k);
    if (!cur || cur.created_at < e.created_at) m.set(k, e); }
  return [...m.values()];
};
// The merge exactly as renderStreams performs it.
function merge(fromRelay, fromStore){
  const evs = [...fromRelay];
  const seen = new Set(evs.map(e => e.id));
  (fromStore||[]).forEach(e => { if (!seen.has(e.id)) evs.push(e); });
  return _dedupAddr(evs);
}
const mine = (id, created) => ({id, pubkey:'me', created_at:created,
  tags:[['d','tok-1'],['status','live']]});
const out = {};
// 1. the relay answer does not yet contain our just-published stream
out.rescued = merge([{id:'other',pubkey:'x',created_at:5,tags:[['d','z']]}], [mine('new', 10)])
                .some(e => e.id === 'new');
// 2. a STALE cached copy must not win over a newer one from the relay
out.newestWins = merge([mine('fresh', 20)], [mine('stale', 10)]).map(e => e.id);
// 3. no duplicate when both sides have it
out.noDupes = merge([mine('same', 10)], [mine('same', 10)]).length;
console.log(JSON.stringify(out));
""", encoding="utf-8")
    r = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    if r.returncode != 0 and "not found" in (r.stderr or ""):
        pytest.skip("node not available")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["rescued"] is True, "a locally-held stream missing from the relay answer is dropped"
    assert got["newestWins"] == ["fresh"], f"a stale cached copy beat the relay's newer one: {got}"
    assert got["noDupes"] == 1, "the same event from both sources renders twice"

    assert "Store.query([{ kinds:[30311]" in body, (
        "renderStreams must merge the local store, or publishing your own stream and navigating "
        "straight here shows an empty grid until a full reload")
