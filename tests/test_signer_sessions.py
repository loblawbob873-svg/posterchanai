"""This device AS a remote signer: many apps at once, persisted, and held to what they asked for.

`Nip46Signer` is the Amber-shaped half of NIP-46 — the one that answers other devices rather than
asking one. It used to hold a SINGLE pairing in memory, which made two things impossible and both of
them silently:

  * a second app ended the first, because `start()` began with `stop()`. The earlier device's
    requests went to a relay nobody was listening on any more and it waited on a signer that had,
    from its point of view, stopped existing. Nothing errored on either side.
  * a reload ended all of them, because the sessions were fields on an object. Undetectable from the
    app until the next signature never came.

`scripts/check_qr_device_login.py` proves both against two real browsers (its `two-apps` case was
verified to fail against the old behaviour with "the phone is signing for 1 app(s), not 2"). What is
tested HERE is the part that needs no browser and would otherwise only be exercised by an app doing
something it should not: the PERMISSION check, and the shape of what gets persisted.

The permission rule is not decoration. `perms` in the nostrconnect URI is what an app declares it
needs, and without enforcement a mini app that asked to sign kind 1 could sign a kind 5 that deletes
your posts — with a key it was handed for something else entirely.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "static", "js", "client", "app.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _signer_source():
    """The `_allowed` / `_grants` pair, lifted out of app.js and made runnable on its own.

    Extracted rather than reimplemented: a copy of the rule in the test is a test of the copy. The
    slice is anchored on the two function names, so a rename breaks this loudly instead of quietly
    testing nothing.
    """
    src = open(APP_JS, encoding="utf-8").read()
    start = src.index("    _grants(qs){")
    end = src.index("    async start(uri, onStatus){")
    body = src[start:end]
    assert "_allowed(sess, method, params)" in body, "the permission check moved out of this slice"
    return "const S = {\n" + body + "\n};\n"


def _run(js):
    prog = _signer_source() + js
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr.strip()[:800])
    return r.stdout.strip()


def _allowed(perms, method, params=None):
    sess = {"perms": perms}
    out = _run(f"console.log(JSON.stringify(S._allowed({json.dumps(sess)}, "
               f"{json.dumps(method)}, {json.dumps(params or [])})));")
    return json.loads(out)


# --------------------------------------------------------------------------------------------
# What an app declared is what it gets
# --------------------------------------------------------------------------------------------
def test_an_app_that_declared_nothing_is_trusted_with_everything():
    """Scanning its QR is the consent, and a client older than the parameter must still work.

    Refusing an app that names no perms would break every signer client that predates the field, for
    a restriction the user already granted by pointing a camera at it.
    """
    assert _allowed(None, "sign_event", [{"kind": 1}]) is True
    assert _allowed(None, "nip44_decrypt", ["pk", "ct"]) is True


def test_an_app_that_named_its_needs_is_held_to_them():
    perms = ["get_public_key", "sign_event:1", "nip44_encrypt"]
    assert _allowed(perms, "sign_event", [{"kind": 1}]) is True
    assert _allowed(perms, "nip44_encrypt", ["pk", "hi"]) is True
    # The one that matters: a key handed over to post notes must not delete them.
    assert _allowed(perms, "sign_event", [{"kind": 5}]) is False, \
        "an app that asked to sign kind 1 was allowed to sign a deletion"
    assert _allowed(perms, "nip04_decrypt", ["pk", "ct"]) is False, \
        "an app that never asked to read DMs was allowed to decrypt one"


def test_the_template_may_arrive_as_a_json_string():
    """NIP-46 sends `sign_event`'s template as a STRING, and the real client does exactly that.

    Read as an object it has no `.kind`, so every kind check would fall through to "not granted" and
    a correctly-scoped app would be refused every signature it asked for.
    """
    perms = ["sign_event:1"]
    assert _allowed(perms, "sign_event", [json.dumps({"kind": 1, "content": "hi"})]) is True
    assert _allowed(perms, "sign_event", [json.dumps({"kind": 5})]) is False


def test_the_handshake_and_the_public_key_are_always_answerable():
    """A pairing that cannot complete its own handshake is not a pairing.

    `connect`/`ping` are the transport, and the pubkey is public by definition — every client asks
    for it immediately after connecting, and refusing it would strand apps whose `perms` simply did
    not think to list it.
    """
    perms = ["sign_event:1"]
    for m in ("connect", "ping", "get_public_key"):
        assert _allowed(perms, m) is True, f"{m} was refused"


def test_a_malformed_template_is_refused_rather_than_waved_through():
    """Unparseable input must fail CLOSED. Erring the other way is a signature on anything."""
    assert _allowed(["sign_event:1"], "sign_event", ["{not json"]) is False


def test_perms_are_a_plain_array_so_they_survive_being_stored():
    """A Set is the obvious type and the wrong one: JSON.stringify turns it into `{}`.

    The sessions are persisted through localStorage, so a Set would come back as an empty object —
    which `_allowed` reads as "declared nothing" and therefore as FULL ACCESS. The restriction would
    evaporate on the first reload, silently, and in the direction that grants rather than denies.
    """
    out = _run("const g = S._grants(new URLSearchParams('perms=sign_event%3A1,nip44_encrypt'));"
               "console.log(JSON.stringify({isArr: Array.isArray(g), round: JSON.parse(JSON.stringify(g))}));")
    got = json.loads(out)
    assert got["isArr"] is True, "perms is not an array, so storing it will not round-trip"
    assert got["round"] == ["sign_event:1", "nip44_encrypt"]


def test_an_empty_perms_parameter_is_not_an_empty_grant():
    """`?perms=` is a client that filled the field in with nothing, not one asking for nothing.

    Treated as an empty ALLOWLIST it would be refused every method including its own handshake.
    """
    out = _run("console.log(JSON.stringify(S._grants(new URLSearchParams('perms='))));")
    assert json.loads(out) is None


# --------------------------------------------------------------------------------------------
# The shape of the thing, asserted against the source
# --------------------------------------------------------------------------------------------
def _src():
    return open(APP_JS, encoding="utf-8").read()


def _signer_obj():
    """Just the `Nip46Signer` object.

    Anchoring on a bare method name is not enough: `Nip46` (the CLIENT half, which asks a signer)
    has its own `_recv`, `_send` and `_open`, and it is defined FIRST — so a slice that starts at
    the first match spans both objects and tests the wrong one.
    """
    src = _src()
    start = src.index("  const Nip46Signer = {")
    return src[start:src.index("\n  };", start)]


def test_pairing_a_second_app_does_not_stop_the_first():
    src = _src()
    start = src.index("    async start(uri, onStatus){")
    body = src[start:src.index("    async resume(){")]
    assert "this.stop()" not in body, \
        "start() stops the signer, so linking a second app ends every other pairing"
    assert "this.sessions.set(" in body, "start() does not add a session"


def test_sessions_are_restored_on_sign_in():
    """A pairing that only survives until the page reloads is a demo, not a pairing."""
    src = _src()
    assert "Nip46Signer.resume()" in src, "nothing ever reopens the stored sessions"
    boot = src[src.index("  function startApp(){"): src.index("  function startApp(){") + 1600]
    assert "Nip46Signer.resume()" in boot, \
        "the resume is not on the path every login funnels through"


def test_a_failed_scan_does_not_tear_down_working_pairings():
    src = _src()
    seg = src[src.index("  async function onQrScanned(uri){"):
              src.index("  async function onQrScanned(uri){") + 900]
    assert "Nip46Signer.stop()" not in seg, \
        "one bad QR ends every app this device was signing for"


def test_the_pairings_are_visible_and_revocable():
    """A signer that cannot say what it is signing for is not one to trust with a key."""
    src = _src()
    assert "signerApps" in src and "signerRevoke" in src, "no way to inspect or end a pairing"
    assert "data-revoke" in src, "the settings card offers no revoke"


def test_background_signing_no_longer_depends_on_the_webview_being_alive():
    """This test used to assert the OPPOSITE, and keeping the reversal on the record is the point.

    It said: no second foreground service, because `StayAwakeService` already keeps the process — and
    therefore the WebView, and therefore the signer's socket — alive with the screen off. That
    reasoning was wrong in the one way that mattered. StayAwake keeps the PROCESS off the freezer; it
    makes no promise about the RENDERER, and Chromium throttles a hidden page's timers to about one a
    minute regardless. So a dropped socket was not redialled until the screen came on, and turning
    "stay connected" on did not help — which is precisely what was reported, and is the observation
    that finally separated the two: "I have to wake the phone for events to actually send from
    desktop."

    The signer is now `SignerRelayService`, native, with no WebView in the path. What this asserts is
    that the hint reports the SERVICE's own measurement rather than this page's intention — a panel
    that says "signing in the background" because the page asked for it is how a dead signer sat
    behind a reassuring line for hours.
    """
    src = _src()
    seg = src[src.index("  async function _signerBackgroundHint(box){"):]
    seg = seg[:seg.index("\n  }\n") + 4]
    assert "_capPlugin('Signer', 'status')" in seg, (
        "the hint no longer asks the signer plugin anything")
    assert "s.serviceRunning" in seg, (
        "the hint must report what the service measured, not what this page intended")
    assert "setStayConnected" not in seg, (
        "the signer is back to riding StayAwakeService, which cannot keep a WebView socket redialling")


def test_the_signer_service_is_started_by_pairing_rather_than_offered():
    """Unlike "stay connected", this is not a background convenience to be opted into.

    Pairing an app IS the request to answer that app, and a signer that only answers while you are
    looking at it is not a signer. The cost still ends when the reason does — the service stops
    itself once nothing is paired (asserted in tests/test_android_signer_service.py).
    """
    src = _src()
    for hook in ("async start(uri, onStatus)", "async resume()"):
        seg = src[src.index("    " + hook):]
        seg = seg[:4000]
        assert "_pushNative" in seg, f"{hook} does not hand the pairing to the service"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_the_qr_carries_the_short_uri_and_the_link_carries_the_full_one():
    """The single biggest thing that makes a scan work, and it is one character away from regressing.

    `perms` is 66% of the URI's bytes and puts the symbol at version 18 (89x89 modules); without it
    the same pairing is version 8 (49x49). Measured with a fake camera across framings: v18 needs the
    code to fill ~75% of the frame, v8 needs ~40% — half the distance. Nothing is lost, because
    `perms` is optional in NIP-46 and advisory to the signer (ours ignores it; Amber prompts per
    action regardless) and the tap/paste route still carries the full URI to the SAME session.

    Both must therefore exist, and the QR must be the short one. Drawing `uri` here would look
    perfectly correct and quietly double how close a camera has to be.
    """
    src = _src()
    seg = src[src.index("    async beginNostrConnect(relays, name){"):]
    seg = seg[:seg.index("return { uri, qrUri, done };") + 40]
    assert "const qrUri=" in seg, "there is no short form; the QR carries the whole perms list"
    assert "perms=" not in seg[seg.index("const qrUri="):seg.index("const qrUri=") + 260], \
        "the short form carries perms, which is the thing that makes it unscannable"
    # …and the drawing uses it. Anchored inside loginAmberNostrConnect, because `qrSrc(` also
    # appears in the generic qrImg helper, which has nothing to do with this screen.
    draw = src[src.index("  async function loginAmberNostrConnect(){"):]
    draw = draw[:draw.index("Open in Amber / scan QR")]   # to the end of that function
    assert "qrSrc(qrUri" in draw, "the QR is drawn from the full URI, not the short one"


def test_a_foreign_relay_is_asked_about_rather_than_obeyed_or_refused():
    """A QR is a picture anyone can print, and it names the relay this device will dial.

    Obeyed blindly, a code from anywhere aims the half of the app that holds the key at a stranger's
    relay, which learns the device's IP from the connection alone. Refused outright — which is what
    shipped first — the signer only works with its own instance, and jumble.social and Coracle print
    perfectly good codes naming theirs. Being usable by other apps is the whole point of the feature,
    so the gate is consent: ours is the silent default, anything else names the host and asks once.
    """
    src = _src()
    seg = src[src.index("    async start(uri, onStatus){"): src.index("    async resume(){")]
    assert "CFG && CFG.relay_url" in seg, "the signer does not know which relay is its own"
    assert "uiConfirm" in seg, "a foreign relay is taken without asking, or refused without asking"
    assert "const relay = qrRelay;" in seg, \
        "the QR's relay is not used even after being allowed — the pairing would be made against a "\
        "relay the other app is not listening on, and would wait for ever with both halves correct"


def test_the_app_name_is_read_before_the_prompt_that_names_it():
    """`const` is in a temporal dead zone until its declaration.

    Reading `name` above its `const` throws ReferenceError on exactly the pairing the prompt exists
    to allow — a crash reachable only by a third-party QR, which is the case least likely to be
    tried locally.
    """
    src = _src()
    seg = src[src.index("    async start(uri, onStatus){"): src.index("    async resume(){")]
    assert seg.index("const name = qs.get('name')") < seg.index("uiConfirm"), \
        "the relay prompt reads `name` before it is declared"


def test_replacing_signs_out_ONE_device_not_every_one_that_shares_a_name():
    """Every PosterChan client announces itself as "PosterChan".

    The first version revoked every session sharing the name, so pairing a tablet signed the desktop
    out — reported exactly that way. "Replace" has to mean the entry the new pairing makes
    redundant: the one nobody has used for longest. Re-pairing a laptop leaves its old row idle,
    while the machine you are actively signing on has a recent timestamp and must survive.
    """
    src = _src()
    seg = src[src.index("  async function onQrScanned(uri){"):]
    seg = seg[:seg.index("\n  }\n")]
    assert "clash.forEach" not in seg, \
        "replacing revokes every session sharing the name, signing out devices you are still using"
    assert "stalest" in seg and "Nip46Signer.revoke(stalest.pk)" in seg, \
        "replace does not target the least-recently-used pairing"
    assert "a.last||a.created" in seg.replace(" ", ""), \
        "staleness is not decided by when the pairing was last used"


def test_the_prompt_says_which_login_it_will_replace():
    """"Replace" over a list of identical names is otherwise a guess the user authorises blind."""
    src = _src()
    seg = src[src.index("  async function onQrScanned(uri){"):]
    seg = seg[:seg.index("\n  }\n")]
    assert "uiConfirm" in seg, "a repeat pairing silently adds another identical entry"
    assert "last used" in seg or "never used" in seg, \
        "the prompt does not identify the pairing it is about to remove"
    assert "Keep them all" in seg, "replacing is forced rather than offered"


# --------------------------------------------------------------------------------------------
# bunker:// — the OTHER direction, and the reason nostrudel could not log in at all
# --------------------------------------------------------------------------------------------
#
# `nostrconnect://` is the flow this signer was built around: the APP publishes a QR and this phone
# scans it. jumble.social and primal.net work that way. nostrudel does not — its "login with a
# signer" screen is one text field whose placeholder is `bunker://<pubkey>?relay=wss://…`, so there
# is nothing to scan and a signer that only speaks nostrconnect cannot log into it at all. Handing it
# anything else gets a bech32 error from the client rather than an explanation ("unknown letter b" is
# a hex key being decoded as an npub), which is why this read as a broken signer instead of a missing
# feature.
#
# What is checked here is the ACCEPT rule, because it is the only part that decides anything: the
# subscription was already right (every kind-24133 addressed to us), so the whole feature is "when do
# we answer a pubkey we have never met". The answer is "when it presents the secret we just minted",
# which in this flow IS the credential — so every way of getting that wrong hands the user's key to
# whoever asked.

def _bunker_source():
    """`_acceptBunker` and the `_grants` it calls, lifted out of app.js and made runnable."""
    src = open(APP_JS, encoding="utf-8").read()
    g0 = src.index("    _grants(qs){")
    g1 = src.index("    _allowed(sess, method, params){")
    a0 = src.index("    _acceptBunker(ev, req){")
    a1 = src.index("    /* Reads BOTH schemes", a0)
    body = src[g0:g1] + src[a0:a1]
    assert "this._pending" in body, "the bunker accept rule moved out of this slice"
    return ("const S = {\n" + body + "\n"
            "  BUNKER_WINDOW: 10*60*1000,\n"
            "  sessions: new Map(), _pending: null, _lastEnc: 'nip44',\n"
            "  _persist(){}, _sync(){}, _pushNative(){ return Promise.resolve(false); },\n"
            "};\n")


def _accept(pending, req, *, pubkey="ff" * 32):
    """Run the real rule against one stranger's request. Returns [accepted, sessions, pendingLeft]."""
    prog = _bunker_source() + (
        f"S._pending = {json.dumps(pending)};\n"
        f"const got = S._acceptBunker({{pubkey:{json.dumps(pubkey)}}}, {json.dumps(req)});\n"
        "console.log(JSON.stringify([!!got, S.sessions.size, S._pending !== null]));\n")
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr.strip()[:800])
    return json.loads(r.stdout.strip())


def _live(secret="s3cret"):
    return {"secret": secret, "relay": "wss://relay.poster.place", "at": 10 ** 13}


def test_a_stranger_is_ignored_when_no_link_is_outstanding():
    """The default has to be silence. This subscription receives every kind-24133 addressed to this
    key, so without a live link an unknown peer is simply somebody else's traffic."""
    ok, n, _ = _accept(None, {"id": "1", "method": "connect", "params": ["x", "s3cret"]})
    assert ok is False and n == 0


def test_a_stranger_with_the_wrong_secret_is_refused():
    """The secret IS the credential in this flow — there is nothing else standing between a stranger
    and a key that signs."""
    ok, n, _ = _accept(_live(), {"id": "1", "method": "connect", "params": ["x", "guess"]})
    assert ok is False and n == 0


def test_the_right_secret_pairs_the_app():
    ok, n, left = _accept(_live(), {"id": "1", "method": "connect", "params": ["x", "s3cret"]})
    assert ok is True and n == 1


def test_the_link_works_once():
    """A bunker secret is a bearer token printed on a screen. Leaving it live would let a second app
    attach off the same screenshot, long after the person who displayed it stopped looking."""
    ok, n, left = _accept(_live(), {"id": "1", "method": "connect", "params": ["x", "s3cret"]})
    assert ok is True
    assert left is False, "the link is still live after being used"


def test_an_expired_link_is_refused():
    """Ten minutes, so a link shown once and forgotten cannot be redeemed days later."""
    stale = {"secret": "s3cret", "relay": "wss://r", "at": 0}
    ok, n, _ = _accept(stale, {"id": "1", "method": "connect", "params": ["x", "s3cret"]})
    assert ok is False and n == 0


def test_only_connect_can_open_a_session():
    """A stranger holding the secret still cannot jump straight to signing.

    Anything other than `connect` from an unknown pubkey is dropped without a reply — answering at
    all would confirm to an unpaired peer that this key is listening on this relay.
    """
    for method in ("sign_event", "get_public_key", "nip44_decrypt", "ping"):
        ok, n, _ = _accept(_live(), {"id": "1", "method": method, "params": ["x", "s3cret"]})
        assert ok is False, f"{method} from a stranger opened a session"
        assert n == 0


def test_the_secret_is_found_wherever_the_client_put_it():
    """Clients disagree about whether `connect`'s first argument is present.

    Reading only index 1 silently rejects every client that omits the signer pubkey — which looks
    exactly like a wrong secret, i.e. like the user's fault.
    """
    for params in (["x", "s3cret"], ["s3cret"], ["x", "s3cret", "sign_event:1"]):
        ok, _, _ = _accept(_live(), {"id": "1", "method": "connect", "params": params})
        assert ok is True, f"the secret was not found in {params}"


def test_a_connect_carrying_no_params_is_refused():
    """The empty case must not fall through to "no restriction" the way an empty `perms` does."""
    for params in ([], None, [""]):
        req = {"id": "1", "method": "connect"}
        if params is not None:
            req["params"] = params
        ok, n, _ = _accept(_live(), req)
        assert ok is False and n == 0


def test_the_paired_app_is_held_to_the_perms_it_asked_for():
    """`connect`'s third argument is the app's declared needs, and it has to reach the session or the
    permission gate has nothing to check against."""
    prog = _bunker_source() + (
        "S._pending = " + json.dumps(_live()) + ";\n"
        "S._acceptBunker({pubkey:'ff'.repeat(32)}, {id:'1',method:'connect',"
        "params:['x','s3cret','sign_event:1,nip44_decrypt']});\n"
        "console.log(JSON.stringify([...S.sessions.values()][0].perms));\n")
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:800]
    assert json.loads(r.stdout.strip()) == ["sign_event:1", "nip44_decrypt"]


def test_the_bunker_link_screen_only_calls_helpers_that_exist():
    """A modal added to a 29k-line IIFE fails at CLICK time, not at load time.

    `node --check` passes, the page loads, the settings screen renders — and the button throws
    ReferenceError the first time somebody presses it, which is the one moment they are trying to log
    into another app. Each of these has a specific reason to be checked rather than assumed:
    `copyValue` because ~30 call sites once used `navigator.clipboard` directly, which the APK's
    WebView and the desktop's app:// origin both refuse; `qrImg` because a desktop client can scan
    nothing and a phone cannot paste into another phone, so both halves have to be there.
    """
    src = _src()
    seg = src[src.index("  async function _showBunkerLink(){"):]
    seg = seg[:seg.index("\n  }\n") + 4]
    for helper in ("qrImg", "_sheet", "copyValue", "Nip46Signer.bunkerUri"):
        assert helper in seg, f"the bunker screen does not use {helper}"
    for helper in ("function copyValue", "function qrImg", "function _sheet"):
        assert helper in src, f"{helper} is gone — the bunker screen throws on click"
    assert "navigator.clipboard" not in seg, (
        "the copy must go through copyValue: navigator.clipboard is refused in both shells")


def test_the_bunker_link_is_offered_even_with_no_apps_paired():
    """The empty state is exactly when somebody is trying to connect their first app.

    The pairing list used to `return` early when it was empty, so a button appended after it would
    never be drawn for the only people who need it.
    """
    src = _src()
    seg = src[src.index("  function _renderSignerApps(){"):]
    seg = seg[:seg.index("\n  }\n") + 4]
    assert "signer-bunker" in seg
    # The two returns above it are the honest ones — no element to draw into, and no local key to
    # sign with, which makes a bunker link meaningless. What must NOT happen is the empty-APPS case
    # returning, since that is exactly when somebody is connecting their first app.
    between = seg[seg.index("No apps are signed in with this device."):seg.index("signer-bunker")]
    assert "return" not in between, (
        "the empty-apps branch returns before the bunker button, hiding it from the only people who "
        "have not paired anything yet")


def test_the_page_answers_nothing_the_service_is_already_answering():
    """The subtlest rule in the split, and it was wrong in the first draft.

    Minting a bunker link REOPENS this half's socket, because only this half holds the pending secret
    and can recognise a stranger. But the native service still holds its own socket for the apps
    already paired — so without a guard, both halves receive every request from those apps and both
    reply, publishing two signed events for one request. That is precisely what the confirmed
    one-way handover exists to prevent, reintroduced by the feature that reopens the socket.

    So: a known session, with the service running, is not this half's to answer.
    """
    src = _src()
    # Anchored on bunkerUri, NOT on `async _recv(raw){` — there are TWO of those. `Nip46` (the half
    # that ASKS a remote signer) and `Nip46Signer` (the half that ANSWERS) both define _recv/_send/
    # _open, the client one first. Slicing from the first match silently reads the wrong object,
    # which is how this test passed against code it had never looked at.
    seg = src[src.index("    async bunkerUri(){"):]
    seg = seg[:seg.index("\n    async _handle(")]
    assert seg.count("async _recv(raw){") == 1, "the signer's _recv left this slice"
    assert "_acceptBunker" in seg, "the stranger path is gone from _recv"
    assert "this.nativeOn" in seg, (
        "_recv does not check whether the service already owns this session — a bunker link would "
        "make every paired app answered twice")
    # The guard must sit on the KNOWN-session branch, not on the stranger branch: guarding the
    # stranger would disable the very flow that needs this socket.
    known = seg[seg.index("sess = this._acceptBunker"):]
    assert re.search(r"\}elseif\(this\.nativeOn\)\{", re.sub(r"\s+", "", known)), (
        "the nativeOn guard is not on the else branch, so it either fires for strangers too or "
        "never fires at all")


def test_closing_the_link_screen_ends_the_offer_and_the_socket():
    """A bearer token left live, plus an idle socket for the life of the app, both from one dialog."""
    src = _src()
    seg = src[src.index("  async function _showBunkerLink(){"):]
    seg = seg[:seg.index("\n  }\n") + 4]
    done = seg[seg.index("bunker-done"):]
    assert "_pending = null" in done, "closing the screen leaves the bunker secret redeemable"
    assert "_standDown" in done, "closing the screen leaves this half's socket open"
