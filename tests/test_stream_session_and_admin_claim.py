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
