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


def test_background_signing_reuses_the_existing_service():
    """No second foreground service, and no switching one on behind the user's back.

    `StayAwakeService` already keeps this app's process — and therefore its WebView, and therefore
    the signer's socket — alive with the screen off. A service of its own would be the same code, a
    second permanent notification and a second battery story. And "Stay connected" is a documented
    opt-in that costs battery: turning it on because an app paired would spend someone's battery for
    them. It offers; it does not decide.
    """
    src = _src()
    seg = src[src.index("  async function _signerBackgroundHint(box){"):]
    seg = seg[:seg.index("\n  }\n") + 4]
    assert "setStayConnected" in seg, "the hint cannot actually turn background signing on"
    assert re.search(r"setStayConnected\(\{\s*on:true\s*\}\)", seg.replace(" ", "") or seg) or \
        "setStayConnected({ on:true })" in seg, "the offer does not enable it"
    assert "b.onclick" in seg, "it enables it without being asked"


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


def test_the_signer_only_uses_this_instances_relay():
    """A QR is a picture anyone can print, and it names the relay this device will dial.

    Unconstrained, a code from anywhere could point the signer — the half that holds the key — at a
    stranger's relay, which learns the device's IP from the connection alone, before any pairing is
    approved. It costs nothing in the flow this exists for: `_ncRelays()` names `CFG.relay_url` and
    nothing else, so a PosterChan QR already carries exactly this relay.
    """
    src = _src()
    seg = src[src.index("    async start(uri, onStatus){"): src.index("    async resume(){")]
    assert "CFG && CFG.relay_url" in seg, "the signer takes whatever relay the QR names"
    assert "const relay = ourRelay || qrRelay" in seg, \
        "the instance relay does not take precedence over the QR's"


def test_a_foreign_relay_is_refused_out_loud_not_silently_swapped():
    """Quietly re-pointing it at ours would pair against a relay the other device is not listening
    on: both halves behave perfectly and it waits on "waiting for the signer" for ever."""
    src = _src()
    seg = src[src.index("    async start(uri, onStatus){"): src.index("    async resume(){")]
    assert "throw new Error(" in seg[seg.index("ourRelay &&"):], \
        "a mismatched relay is swapped rather than reported"


def test_a_standalone_build_still_works():
    """With no instance there is no relay of ours to insist on, and the QR's is all there is."""
    src = _src()
    seg = src[src.index("    async start(uri, onStatus){"): src.index("    async resume(){")]
    assert "ourRelay &&" in seg, "the constraint is applied even when this build has no instance relay"
