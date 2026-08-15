"""The native NIP-46 signer: its rules RUN, its crypto is cross-checked, its wiring is asserted.

WHAT WENT WRONG, because it explains why this file is shaped the way it is. The remote signer was a
WebSocket owned by JavaScript inside the WebView. Chromium throttles a hidden page's timers to about
one a minute — harder after five — so when that socket dropped for any of the ordinary mobile reasons
(a NAT timeout, a cell handover, a relay restart) the reconnect that would have healed it was
throttled along with everything else. Nothing errored anywhere. The desktop sat on "waiting for the
signer to approve" until the screen came on, at which point the page unthrottled, redialled, and the
whole backlog landed at once. Reported as: "I have to wake the phone for events to actually send from
desktop."

`StayAwakeService` was already on and could not fix it, which is the clue that dates the diagnosis: it
keeps the PROCESS off the freezer so "the WebView keeps its relay socket" — a promise about the
process, not about the renderer's timer policy. Amber has never had this problem because Amber is a
native service. So the answer is a native service, and `signer/SignerRelayService` is it.

WHAT IS ACTUALLY PROVEN HERE, and what is not, stated plainly because there is no Android device on
the machine this was written on:

  * The DECISIONS run. `Nip46Core` has no Android imports at all, so javac and java execute the real
    permission gate, the real scheme selection, the real merge and the real backoff — not a
    description of them. These are the rules that decide whether a request is answered and whether
    the answer is readable, and every one of them is a silent failure when wrong.
  * The CRYPTO runs, and is checked against a SECOND IMPLEMENTATION. `Nostr` and `Crypt` need only
    `android.util.Base64`, which tests/androidstubs implements for real (it is pure byte
    manipulation with a published answer, unlike the signature-only platform stubs around it). So the
    shipped Java signs and encrypts here, and the assertions compare it to `app/services/nostr/` —
    the repo's own independent pure-Python implementation. A round trip against ITSELF would pass
    just as happily with a wrong conversation key; agreeing with a different implementation is what
    actually predicts that jumble.social can read the reply.
  * The WIRING is asserted by reading the sources. The service itself cannot be compiled here — it
    needs the whole Android framework plus OkHttp — so the things that are true or false at a glance
    (is the service declared, is the dependency there, does boot restart it, does the web half
    actually hand over) are checked as text. That is weaker and is used only where it is the best
    available.

WHAT IS NOT PROVEN: that a real phone answers a real request while dozing. Nothing off-device can
show that. What this file does is remove every failure that would ALSO have to be fixed first.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android")
SIGNER = os.path.join(ANDROID, "app", "src", "main", "java", "place", "poster", "app", "signer")
MANIFEST = os.path.join(ANDROID, "app", "src", "main", "AndroidManifest.xml")
GRADLE = os.path.join(ANDROID, "app", "build.gradle")
BOOT = os.path.join(ANDROID, "app", "src", "main", "java", "place", "poster", "app",
                    "push", "BootReceiver.java")
APP_JS = os.path.join(ROOT, "static", "js", "client", "app.js")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _java(body: str) -> str:
    """Compile a throwaway driver against the REAL signer sources and run it.

    The driver joins the signer's own package so a rule does not have to be public to be tested.
    """
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("no JDK")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "Driver.java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.signer;\n"
                     "import java.util.*;\n"
                     "public class Driver {\n"
                     "  public static void main(String[] argv) throws Exception {\n%s\n  }\n}\n"
                     % body)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(
            ["javac", "-nowarn", "-d", out,
             "-sourcepath", os.path.join(ROOT, "tests", "androidstubs") + os.pathsep
                            + os.path.join(ANDROID, "app", "src", "main", "java"),
             os.path.join(SIGNER, "Nip46Core.java"),
             os.path.join(SIGNER, "Nostr.java"),
             os.path.join(SIGNER, "Bech32.java"),
             os.path.join(SIGNER, "Crypt.java"), src],
            capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-3000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.signer.Driver"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-3000:]
        return r.stdout.strip()


# --------------------------------------------------------------------------------------------
# The permission gate — RUN, not described.
# --------------------------------------------------------------------------------------------

def test_the_permission_gate_matches_the_web_signer_it_replaces():
    """A native signer that answers differently from the one it replaces is a new set of bugs.

    Every case here is the behaviour of `Nip46Signer._allowed` in app.js, which has been answering
    real apps for months. The permissive one is deliberate and is the one most likely to be
    "corrected" by somebody later: an app that declared NO perms is granted everything, because a
    client that lists nothing did not fill the field in — it is not asking for nothing — and refusing
    those would silently break every such app.
    """
    out = _java("""
      System.out.println("empty=" + Nip46Core.allowed("", "sign_event", 1));
      System.out.println("null=" + Nip46Core.allowed(null, "sign_event", 1));
      System.out.println("listed=" + Nip46Core.allowed("sign_event,nip44_decrypt", "nip44_decrypt", -1));
      System.out.println("unlisted=" + Nip46Core.allowed("nip44_decrypt", "sign_event", 1));
      System.out.println("kindscoped=" + Nip46Core.allowed("sign_event:1", "sign_event", 1));
      System.out.println("wrongkind=" + Nip46Core.allowed("sign_event:1", "sign_event", 5));
      System.out.println("connect=" + Nip46Core.allowed("sign_event:1", "connect", -1));
      System.out.println("ping=" + Nip46Core.allowed("sign_event:1", "ping", -1));
      System.out.println("getpub=" + Nip46Core.allowed("sign_event:1", "get_public_key", -1));
      System.out.println("spaces=" + Nip46Core.allowed(" sign_event , ping ", "sign_event", 1));
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert got["empty"] == "true", "an app that declared no perms must keep working"
    assert got["null"] == "true"
    assert got["listed"] == "true"
    assert got["unlisted"] == "false", "an undeclared method must be refused"
    assert got["kindscoped"] == "true"
    assert got["wrongkind"] == "false", "sign_event:1 must not authorise a kind 5 deletion"
    # The handshake trio is answerable whatever was declared: a client cannot ask for a pubkey
    # permission before it has connected, so gating these deadlocks the pairing it is meant to guard.
    assert got["connect"] == got["ping"] == got["getpub"] == "true"
    assert got["spaces"] == "true", "perms arrive from a URL and are whitespace-sloppy"


def test_an_unreadable_template_is_never_treated_as_kind_zero():
    """-1, not 0, and the difference is a real grant being silently widened.

    Kind 0 is profile metadata. If a template that could not be parsed defaulted to kind 0, an app
    granted exactly `sign_event:0` would be handed every unparseable request as though it had asked
    for it — including one deliberately malformed to look that way.
    """
    out = _java("""
      System.out.println("unreadable=" + Nip46Core.allowed("sign_event:0", "sign_event", -1));
      System.out.println("real0=" + Nip46Core.allowed("sign_event:0", "sign_event", 0));
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert got["unreadable"] == "false"
    assert got["real0"] == "true"


# --------------------------------------------------------------------------------------------
# Which encryption — the rule behind "it shows in my signer but the site never logs in".
# --------------------------------------------------------------------------------------------

def test_the_reply_scheme_follows_the_peer_and_defaults_to_nip44():
    """Answering everything in NIP-04 is how a pairing looks successful and does nothing.

    We mint the session — so our side lists the app — and then send an acknowledgement the other end
    cannot read. NIP-46 moved to NIP-44 and current clients may implement only that.
    """
    out = _java("""
      System.out.println("marked=" + Nip46Core.nip04First("aGVsbG8=?iv=abc"));
      System.out.println("plain=" + Nip46Core.nip04First("aGVsbG8="));
      System.out.println("nullct=" + Nip46Core.nip04First(null));
      System.out.println("default=" + Nip46Core.replyWithNip04(""));
      System.out.println("learned04=" + Nip46Core.replyWithNip04("nip04"));
      System.out.println("learned44=" + Nip46Core.replyWithNip04("nip44"));
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert got["marked"] == "true", "?iv= is NIP-04's own marker"
    assert got["plain"] == "false"
    assert got["nullct"] == "false"
    assert got["default"] == "false", "an unknown peer must be answered in NIP-44"
    assert got["learned04"] == "true"
    assert got["learned44"] == "false"


# --------------------------------------------------------------------------------------------
# The merge — the rule that keeps a handover from un-learning what the awake half knows.
# --------------------------------------------------------------------------------------------

def test_republishing_the_pairings_does_not_discard_what_the_service_learned():
    """`enc` is learned from traffic the web layer never sees, because it is the half that is asleep.

    The page republishes the list whenever a pairing changes. A straight replace would reset `enc`
    every time, so the next reply to a peer already known to speak NIP-04 would go out as NIP-44 — a
    well-formed event the far end cannot read, which is silence, not an error.
    """
    out = _java("""
      String pk = "aa";
      for (int i = 0; i < 5; i++) pk = pk + pk;                 // 64 hex chars
      Map<String, Nip46Core.Session> run = new LinkedHashMap<>();
      run.put(pk, new Nip46Core.Session(pk, "wss://r", "app", "", "nip04", 500));
      List<Nip46Core.Session> in = new ArrayList<>();
      in.add(new Nip46Core.Session(pk, "wss://r", "app", "", "", 0));
      Map<String, Nip46Core.Session> out = Nip46Core.merge(run, in);
      System.out.println("enc=" + out.get(pk).enc);
      System.out.println("last=" + out.get(pk).last);
      System.out.println("revoked=" + Nip46Core.merge(run, new ArrayList<Nip46Core.Session>()).size());
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert got["enc"] == "nip04", "the learned scheme must survive a republish"
    assert got["last"] == "500", "the newer timestamp is the service's, not the page's"
    assert got["revoked"] == "0", (
        "membership is still authoritative — a revoked app must actually stop being answered")


def test_a_malformed_session_is_dropped_rather_than_connected_to():
    """A short pubkey or a missing relay is a session that can only produce a socket to nowhere."""
    out = _java("""
      List<Nip46Core.Session> in = new ArrayList<>();
      in.add(new Nip46Core.Session("tooshort", "wss://r", "", "", "", 0));
      String pk = "bb"; for (int i = 0; i < 5; i++) pk = pk + pk;
      in.add(new Nip46Core.Session(pk, "", "", "", "", 0));
      in.add(new Nip46Core.Session(pk, "wss://ok", "", "", "", 0));
      Map<String, Nip46Core.Session> m = Nip46Core.merge(null, in);
      System.out.println("kept=" + m.size());
      System.out.println("relays=" + Nip46Core.relays(m).size());
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert got["kept"] == "1"
    assert got["relays"] == "1"


def test_one_socket_per_relay_however_many_apps_share_it():
    """The cost claim in the service's own header, checked rather than asserted in prose.

    Ten logins against this instance must be one connection, or "it costs about what a messaging app
    costs" stops being true precisely for the people who use the feature most.
    """
    out = _java("""
      List<Nip46Core.Session> in = new ArrayList<>();
      for (int i = 0; i < 10; i++) {
        String pk = String.format("%064x", i + 1);
        in.add(new Nip46Core.Session(pk, "wss://relay.poster.place", "app" + i, "", "", 0));
      }
      in.add(new Nip46Core.Session(String.format("%064x", 99), "wss://other", "x", "", "", 0));
      Map<String, Nip46Core.Session> m = Nip46Core.merge(null, in);
      System.out.println("sessions=" + m.size());
      System.out.println("relays=" + Nip46Core.relays(m).size());
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert got["sessions"] == "11"
    assert got["relays"] == "2", "sessions sharing a relay must share its socket"


def test_the_redial_backs_off_but_never_gives_up():
    """Both ends of this matter and they fail in opposite directions.

    No floor and a relay that refuses instantly is redialled in a hot loop, which cooks the battery of
    the phone whose battery is the reason the feature is opt-in. No cap and a phone that spent ten
    minutes in a tunnel is still waiting long after the signal came back — which the user experiences
    as exactly the bug this service was written to fix.
    """
    out = _java("""
      StringBuilder b = new StringBuilder();
      for (int i = 0; i <= 12; i++) b.append(Nip46Core.backoffMs(i)).append(' ');
      System.out.println(b.toString().trim());
    """)
    vals = [int(x) for x in out.split()]
    assert min(vals) >= 2000, "a refused relay must not be redialled in a hot loop"
    assert max(vals) <= 60000, "a redial must never drift past a minute"
    assert vals[1] < vals[4], "it must actually back off"
    assert vals[-1] == 60000, "and then stay at the cap rather than growing"


def test_the_subscription_asks_for_a_window_not_for_now():
    """The clock-skew budget, and it is the same number the web signer uses.

    Two machines pairing over a QR have two clocks by definition, the relay applies `since`
    server-side, and the requesting app stamps its request with ITS clock. A desktop a minute behind
    this phone had every request dropped before it arrived: the phone said "logged in", the desktop
    sat on "waiting for the signer", and nothing anywhere raised an error.
    """
    out = _java("""
      System.out.println("skew=" + Nip46Core.SINCE_SKEW);
      System.out.println("since=" + Nip46Core.since(1000000));
      System.out.println("floor=" + Nip46Core.since(10));
    """)
    got = dict(kv.split("=") for kv in out.split())
    assert int(got["skew"]) >= 900, "a smaller window is a skewed desktop that silently never pairs"
    assert int(got["since"]) == 1000000 - int(got["skew"])
    assert int(got["floor"]) == 0, "a fresh device's clock must not produce a negative since"

    js = _read(APP_JS)
    m = re.search(r"NIP46_SINCE_SKEW\s*=\s*(\d+)", js)
    assert m, "NIP46_SINCE_SKEW is gone from the web signer"
    assert int(m.group(1)) == int(got["skew"]), (
        "the two halves answer the same pairings and must agree on the window; they drifted once "
        "already, when only one side was fixed")


# --------------------------------------------------------------------------------------------
# The crypto — RUN, and checked against a second implementation.
# --------------------------------------------------------------------------------------------

def test_the_native_signature_matches_the_repos_own_python_implementation():
    """Two independent implementations, one answer. A round trip against itself proves nothing.

    This is the check that would have caught a wrong signature — an event that is perfectly
    well-formed, publishes without complaint, and is rejected by every relay and client that verifies
    it. The vector is BIP-340's own index 0, so a third opinion is on record too.
    """
    out = _java("""
      byte[] sec = Nostr.unhex("0000000000000000000000000000000000000000000000000000000000000003");
      byte[] aux = new byte[32];
      byte[] msg = new byte[32];
      System.out.println("pub=" + Nostr.hex(Nostr.pubkey(sec)));
      System.out.println("sig=" + Nostr.hex(Nostr.sign(msg, sec, aux)));
      System.out.println("verify=" + Nostr.verify(msg, Nostr.pubkey(sec), Nostr.sign(msg, sec, aux)));
    """)
    got = dict(kv.split("=") for kv in out.split())

    from app.services.nostr import bip340
    sec = bytes.fromhex("00" * 31 + "03")
    assert got["pub"] == bip340.pubkey_from_seckey(sec).hex()
    assert got["sig"] == bip340.sign(bytes(32), sec, bytes(32)).hex(), (
        "the Java schnorr disagrees with the Python one — one of them signs events nothing accepts")
    assert got["verify"] == "true"


def test_python_can_read_what_the_native_signer_encrypts():
    """Interop, in the direction that actually matters.

    The reply this service sends has to be readable by whatever asked — jumble.social, Coracle, this
    app's own desktop build. None of those can be run here, but agreeing byte-for-byte with a
    SEPARATE implementation of both schemes is the strongest evidence available off-device, and it is
    far stronger than the round trip against itself that would pass with a wrong conversation key on
    both sides.
    """
    out = _java("""
      byte[] s1 = Nostr.unhex("315e59ff51cb9209768cf7da80791ddcaae56ac9775eb25b6dee1234bc5d2268");
      byte[] p2 = Nostr.pubkey(Nostr.unhex(
          "0000000000000000000000000000000000000000000000000000000000000003"));
      String msg = "{\\"id\\":\\"7\\",\\"result\\":\\"ack\\"}";
      System.out.println("k44=" + Crypt.nip44Encrypt(Crypt.conversationKey(s1, p2), msg, null));
      System.out.println("k04=" + Crypt.nip04Encrypt(s1, p2, msg));
      System.out.println("ck=" + Nostr.hex(Crypt.conversationKey(s1, p2)));
    """)
    got = dict(kv.split("=", 1) for kv in out.split("\n"))

    from app.services.nostr import bip340, nip04, nip44
    s1 = bytes.fromhex("315e59ff51cb9209768cf7da80791ddcaae56ac9775eb25b6dee1234bc5d2268")
    s2 = bytes.fromhex("00" * 31 + "03")
    p1 = bip340.pubkey_from_seckey(s1)
    want = '{"id":"7","result":"ack"}'

    assert nip44.decrypt_from(s2, p1, got["k44"]) == want, (
        "the far end cannot read a NIP-44 reply from this signer")
    assert nip04.decrypt(s2, p1, got["k04"]) == want, (
        "the far end cannot read a NIP-04 reply from this signer")
    # And the payload really is in the scheme the selection rule will claim it is.
    assert "?iv=" not in got["k44"] and "?iv=" in got["k04"]


def test_the_check_can_fail():
    """A crypto test that cannot fail is a crypto test that proves nothing.

    Re-runs the same comparison against a deliberately wrong key, so a future change that quietly
    stops exercising the real path shows up here rather than in a signer nobody can log in with.
    """
    from app.services.nostr import bip340, nip44
    s1 = bytes.fromhex("315e59ff51cb9209768cf7da80791ddcaae56ac9775eb25b6dee1234bc5d2268")
    wrong = bytes.fromhex("00" * 31 + "05")
    out = _java("""
      byte[] s1 = Nostr.unhex("315e59ff51cb9209768cf7da80791ddcaae56ac9775eb25b6dee1234bc5d2268");
      byte[] p2 = Nostr.pubkey(Nostr.unhex(
          "0000000000000000000000000000000000000000000000000000000000000003"));
      System.out.println(Crypt.nip44Encrypt(Crypt.conversationKey(s1, p2), "hello", null));
    """).strip()
    with pytest.raises(Exception):
        nip44.decrypt_from(wrong, bip340.pubkey_from_seckey(s1), out)


# --------------------------------------------------------------------------------------------
# NIP-55 answers get_public_key with an npub. Ours answered hex, and said so in bech32.
# --------------------------------------------------------------------------------------------

def test_the_java_bech32_agrees_with_the_repos_python_one():
    """The checksum is the half that is silently wrong when it is wrong.

    A bad checksum still LOOKS like an npub — right prefix, right length, right alphabet — and only
    fails in whatever app you hand it to, which is the same class of failure this whole fix is about.
    So it is checked against `app/services/nostr/bech32.py`, a separate implementation in another
    language, rather than against itself.
    """
    out = _java("""
      String[] keys = {
        "0000000000000000000000000000000000000000000000000000000000000001",
        "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6",
        "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d",
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      };
      for (String k : keys) System.out.println(k + " " + Bech32.npub(Nostr.unhex(k)));
    """)
    from app.services.nostr import bech32 as pb
    seen = 0
    for line in out.strip().splitlines():
        hexk, got = line.split()
        assert got == pb.encode("npub", bytes.fromhex(hexk)), f"bech32 differs for {hexk}"
        seen += 1
    assert seen == 4
    # NIP-19's own published vector, so a third opinion is on the record too.
    assert "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6" in out


def test_get_public_key_answers_with_the_npub():
    """`unknown letter "b"`, which is what a client says when it bech32-decodes hex.

    NIP-55 answers this verb with the bech32 form — Amber does, so clients parse it that way. We
    returned hex. Our OWN client hid it by accepting either form, so only a third-party app could
    see it; its comment already documented the contract as "npub for get_public_key".
    """
    src = _read(os.path.join(SIGNER, "SignerActivity.java"))
    branch = src[src.index('if (TextUtils.isEmpty(type) || "get_public_key".equals(type))'):]
    branch = branch[:branch.index('} else if ("sign_event"')]
    assert "Bech32.npub" in branch, "get_public_key is answering with hex again"
    for extra in ('putExtra("signature", npub)', 'putExtra("result", npub)'):
        assert extra in branch, f"{extra} is missing — clients read one or the other"


def test_the_signed_event_still_carries_a_HEX_pubkey():
    """The npub belongs in the ANSWER to get_public_key and nowhere else.

    An event whose `pubkey` field is bech32 is invalid: its id is the sha256 of a serialisation
    containing that field, so it would be rejected by every relay and fail verification everywhere,
    while looking fine in a log. This is the obvious over-correction and it is worth a test of its
    own, since both values sit in the same method one branch apart.
    """
    src = _read(os.path.join(SIGNER, "SignerActivity.java"))
    branch = src[src.index('} else if ("sign_event".equals(type))'):]
    branch = branch[:branch.index("} else {")]
    assert 'ev.put("pubkey", pub)' in branch, "the event's pubkey is no longer the hex key"
    assert "npub" not in branch, "an npub leaked into the signed event"


# --------------------------------------------------------------------------------------------
# The wiring. Read as text, because the service cannot be compiled here.
# --------------------------------------------------------------------------------------------

def test_the_service_is_declared_and_is_not_capped_at_six_hours_a_day():
    """`dataSync` is the obvious type and the wrong one.

    Android 15 caps it at six hours in any twenty-four, so a signer declared that way silently stops
    answering for most of the day — which is this bug again, with a different cause and identical
    symptoms. StayAwakeService learned this already; the comment there is why this one was written
    with `specialUse` from the start.
    """
    xml = _read(MANIFEST)
    m = re.search(r"<service\s[^>]*android:name=\"\.signer\.SignerRelayService\".*?</service>",
                  xml, re.S)
    assert m, "SignerRelayService is not declared — Android will refuse to start it"
    block = m.group(0)
    assert 'android:foregroundServiceType="specialUse"' in block
    assert "dataSync" not in block
    assert 'android:exported="false"' in block, "nothing outside this app may start the signer"
    assert "PROPERTY_SPECIAL_USE_FGS_SUBTYPE" in block, (
        "Play requires the subtype declaration for specialUse; without it the listing is rejected")
    assert "FOREGROUND_SERVICE_SPECIAL_USE" in xml


def test_the_websocket_library_is_actually_a_dependency():
    """Android has no WebSocket client, so without this the service compiles and cannot connect."""
    g = _read(GRADLE)
    assert re.search(r"implementation\s+['\"]com\.squareup\.okhttp3:okhttp:", g), (
        "okhttp is missing — SignerRelayService imports okhttp3 and the build will fail")


def test_the_signer_survives_a_reboot_independently_of_stay_connected():
    """Two switches, and folding them into one is a silent regression for the common case.

    Someone can sign for their desktop without opting into a permanent relay connection for
    notifications — that is the ordinary setup. Ordered after `if (!StayAwakeService.wanted) return;`
    a phone with the signer on and stay-connected off came back from a reboot answering nothing,
    which is indistinguishable from the signer being broken.
    """
    src = _read(BOOT)
    stay = src.index("StayAwakeService.wanted(ctx)) return")
    signer = src.index("SignerRelayService.wanted(ctx)")
    assert signer < stay, (
        "the signer restart sits after the stay-connected early return, so a user who enabled only "
        "the signer gets no signer after a reboot")


def test_the_web_half_hands_over_and_stops_listening_itself():
    """Two signers answering one request is two events published for it.

    The handover has to be explicit, one-way and confirmed: publish, check the service really came
    up, and only then close these sockets. If it is not confirmed, this half must carry on — which is
    also what happens in a browser and on the desktop build, where there is no service at all.
    """
    js = _read(APP_JS)
    assert "_pushNative" in js and "_standDown" in js
    # The pairing path hands over only AFTER confirming, and the resume path opens nothing if the
    # service took the job.
    assert re.search(r"if\(await this\._pushNative\(\)\)\s*this\._standDown\(\)", js), (
        "the pairing path must stand down only on a confirmed handover")
    assert re.search(r"if\(!native\)\s*\{", js), (
        "resume() must not open sockets when the service already has the job")
    # A revoked app must stop being answered by the half that is awake.
    revoke = js[js.index("revoke(clientPk)"):]
    assert "_pushNative" in revoke[:800], (
        "revoke() does not tell the service, so a signed-out app keeps being signed for")


def test_the_secret_is_never_handed_to_the_service():
    """The pairing secret is only ever needed for the ACK, which the page sends before handing over.

    Sending it anyway would put a credential in a second store for no purpose. Checked because the
    obvious way to write `_pushNative` is to serialise the session objects wholesale, which would
    include it.
    """
    js = _read(APP_JS)
    push = js[js.index("async _pushNative()"):]
    push = push[:push.index("_standDown")]
    assert "secret" not in push, "the pairing secret is being published to the native service"


def test_the_service_reports_what_it_measured():
    """A panel that reports the page's intention is how "the signer is on" sits above a signer that
    has answered nothing for hours. The counters are on the SERVICE and are read from it."""
    plugin = _read(os.path.join(SIGNER, "SignerPlugin.java"))
    for field in ("serviceRunning", "connected", "answered", "lastRequestAt", "lastError"):
        assert field in plugin, f"status() does not report {field}"
    assert "SignerRelayService.running" in plugin
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    assert "public static boolean running" in svc, (
        "the counters must be static: the case worth explaining is the one where the page is gone")


def test_no_key_means_no_sockets():
    """A phone with no signing key must refuse every request, so holding connections open for it is
    battery spent to answer nothing."""
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    reload_body = svc[svc.index("private void reload()"):]
    reload_body = reload_body[:reload_body.index("private OkHttpClient http()")]
    assert "SignerKey.load(this) == null" in reload_body and "closeAll()" in reload_body


def test_the_only_thing_that_runs_on_a_timer_is_the_keepalive():
    """The battery budget, asserted rather than promised in a comment.

    Between requests this service holds a parked TCP socket, which costs nothing, and sends one ping,
    which costs a radio wake-up. So the ping interval IS the power draw. Below three minutes is
    wake-ups bought for no extra safety; above five, carrier NATs start expiring the idle mapping and
    the socket goes dead-but-open — answering nothing while looking perfectly healthy, which is the
    bug the service exists to remove.
    """
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    m = re.search(r"\.pingInterval\((\d+),\s*TimeUnit\.SECONDS\)", svc)
    assert m, "the keepalive is gone — a dead socket would never be noticed"
    secs = int(m.group(1))
    assert 180 <= secs <= 300, (
        f"ping interval is {secs}s; under 180 is battery spent for nothing, over 300 outlives the "
        f"five-minute NAT mapping this is sized against")


def test_the_signer_never_does_its_work_on_the_ui_thread():
    """Signing for three apps made the whole app unusable, and nothing logged a thing.

    `onMessage` posted to `Looper.getMainLooper()`, so every frame a relay sent was parsed on the
    thread that draws — the kind/session filter runs AFTER `new JSONArray(raw)`, so even traffic
    meant for nobody cost main-thread time on every open socket. A request actually addressed to us
    then did a Keystore load, an ECDH + NIP-44 decrypt, a pure-Java secp256k1 Schnorr signature, and
    a second ECDH + encrypt + signature for the reply — all there. Pure-BigInteger point
    multiplication is tens to hundreds of milliseconds, so one signature drops frames and three
    apps' worth arrive whenever they like, including mid-scroll. Reported as "buttons laggy,
    scrolling is terrible, the APK is unusable", with nothing in any log because from the service's
    side every request SUCCEEDED.

    The confinement matters as much as the thread: `sessions`/`socks`/`failures` are plain Maps whose
    every mutation is single-threaded today. Moving `recv` to a pool would trade jank for corruption,
    so the whole service moves to ONE HandlerThread — which is why `reload()` and `closeAll()` must
    be posted rather than called inline from the lifecycle callbacks, which arrive on main.
    """
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    assert "getMainLooper()" not in svc, (
        "the signer service is back on the main looper — every relay message, every decrypt and "
        "every signature would run on the thread that draws the UI"
    )
    assert re.search(r"new\s+Handler\(\s*thread\.getLooper\(\)\s*\)", svc), (
        "the service's Handler is no longer backed by its own HandlerThread"
    )
    assert re.search(r"new\s+HandlerThread\(", svc), "the work thread is gone"
    # The lifecycle callbacks arrive on main, so anything they touch that `recv` also touches has to
    # be handed to the work thread rather than run where it was called.
    start = svc[svc.index("public int onStartCommand("):]
    start = start[:start.index("private void reload()")]
    assert re.search(r"handler\.post\(this::reload\)", start), (
        "onStartCommand calls reload() inline, racing the work thread on sessions/socks"
    )
    assert re.search(r"handler\.post\(this::closeAll\)", start), (
        "the STOP branch closes sockets on the main thread, racing the work thread on socks"
    )
    destroy = svc[svc.index("public void onDestroy()"):]
    assert "quitSafely()" in destroy, (
        "onDestroy does not stop the work thread safely — quit() would drop the queued close and "
        "leak every open WebSocket"
    )


def test_native_secp256k1_proves_itself_before_it_is_trusted_and_is_never_imported():
    """The fast path may not be reached by an import, and may not be believed without a check.

    Amber is quick because it signs in C. The pure-Java signer here is 36ms per Schnorr signature on
    a warmed DESKTOP core — four point multiplications, over half of them a self-verify — against
    about 50 MICROseconds for libsecp256k1, twice per NIP-46 request. That is the whole of "this
    signer is slower than amber to get events published".

    TWO THINGS MUST HOLD, and neither is visible at a glance:

      * REFLECTION, never an import. `Nostr` and `Crypt` are compiled and RUN by
        test_android_nip55_signer.py under plain javac/java — no Android, no JNI — so the shipped
        crypto can be checked byte-for-byte against the repo's own Python implementation. A
        compile-time reference to fr.acinq.secp256k1 breaks that, and the cross-check is worth more
        than the tidiness. It is also what makes a missing library or an ABI with no .so degrade to
        the Java path rather than fail to start.
      * IT PROVES ITSELF FIRST. None of this can be run on the machine it was written on, and wrong
        signing code does not throw — it produces signatures a relay rejects, or ones that verify
        against themselves and nothing else. So the native pubkey is compared with the Java one and
        the first native signature is verified with `Nostr.verify` before anything real is signed.
    """
    nat = _read(os.path.join(SIGNER, "Native.java"))
    assert "import fr.acinq" not in nat, (
        "the native library is imported — Nostr/Crypt would stop compiling under plain javac and the "
        "cross-check against the Python implementation would be lost"
    )
    assert "Class.forName(\"fr.acinq.secp256k1.Secp256k1\")" in nat, "the reflection lookup is gone"
    assert "Nostr.pubkey(" in nat and "Nostr.verify(" in nat, (
        "the native path no longer checks itself against the Java implementation before being used"
    )
    # And the callers must treat null as "use the Java path", not as a failure.
    nos = _read(os.path.join(SIGNER, "Nostr.java"))
    for fn in ("Native.sign(", "Native.pubkey("):
        assert fn in nos, f"{fn} is not wired into Nostr"
    assert nos.count("if (fast != null) return fast;") >= 2, (
        "a null from the native path must fall through to the Java implementation"
    )
    gradle = _read(os.path.join(ANDROID, "app", "build.gradle"))
    assert "secp256k1-kmp-jni-android" in gradle, "the native dependency is missing from the build"


def test_a_new_pairing_never_revokes_an_existing_one_by_name():
    """A name is a PRODUCT, not an identity, and this app kept mistaking one for the other.

    Every PosterChan client announces itself as "PosterChan"; every primal.net login announces
    "PrimalWeb". Matching a new pairing against existing ones BY NAME therefore treats four different
    machines as four attempts at the same one. It was reported four ways in a row — "i signed in 4
    devices but only see 2?", "i choose keep them all and nothing goes on", "i could only sign in 1
    device", "it is still thinking all posterchan devices are the same" — and every one of them was
    that match.

    Inverting the prompt's buttons so KEEPING was the default only made the destruction less likely.
    The question itself is unanswerable: nothing in the client knows whether two pairings are one
    laptop paired twice or two laptops, and neither does the person being asked, because both rows
    say the same word. Amber does not ask. Telling them apart is the LIST's job — which is why the
    rows carry "added …" and mark the never-used ones.

    Measured after removing it: four same-named devices pair, all four get their ACK, all four rows
    persist, and no extra dialog appears.
    """
    js = _read(APP_JS)
    body = js[js.index("async function onQrScanned(uri)"):]
    body = body[:body.index("await Nip46Signer.start(uri)")]
    assert "revoke(" not in body, (
        "pairing a device revokes another one again — a new pairing must never delete an existing "
        "session, whatever the two are called"
    )
    assert "already have" not in body, "the name-clash prompt is back"
    assert not re.search(r"\.name\s*\|\|\s*''\s*\)\s*===\s*name", body), (
        "sessions are being matched by name again"
    )


def test_the_battery_check_runs_at_boot_and_re_asks_on_every_new_build():
    """Android sets apps to "Optimized" by itself and OEMs re-apply it after an update.

    An optimized app has its NETWORK deferred by Doze while the screen is off, so a request sits on
    the relay until a maintenance window: the other device shows "waiting for your signer…", the note
    stays in drafts, and nothing logs a fault because the service is alive and subscribed — the
    packets simply do not move. The settings panel said so, but only to somebody already looking.

    The two things that make this check useless if they rot: it stops being CALLED at boot, or its
    memory stops being keyed on the BUILD. "Asked once, ever" goes quiet permanently the first time
    somebody says not now — and an app update is exactly the moment the restriction comes back.
    """
    js = _read(APP_JS)
    assert "_signerBatteryCheck()" in js, "the battery check is never called"
    body = js[js.index("async function _signerBatteryCheck()"):]
    body = body[:body.index("async function _signerBackgroundHint")]
    assert "serviceWanted" in body, (
        "the check is not gated on the signer being wanted — it would nag somebody who has never "
        "paired an app"
    )
    assert "batteryExempt !== false" in body, (
        "the check does not read what the PHONE answered; it must not fire when already exempt or "
        "on a version too old for the exemption to exist"
    )
    assert "__PC_APP_BUILD__" in body and "pc_signer_batt_asked" in body, (
        "the check no longer re-asks per build — declining once would silence it for ever, through "
        "every future update that re-applies the restriction"
    )


def test_the_work_thread_is_not_in_the_background_cgroup():
    """Getting the crypto off the UI thread was right; taking it out of the foreground scheduler
    with it was not, and it is the same one-word mistake either way.

    THREAD_PRIORITY_BACKGROUND does not merely lower a nice value on Android — it moves the thread
    into the background CGROUP, capped at a small share of one core and on most devices confined to
    the little cluster. This thread does the only CPU-heavy work in the app: FOUR secp256k1 point
    multiplications per NIP-46 request (measured 36ms per Schnorr signature on a warmed DESKTOP
    core, because sign() also self-verifies), in pure-Java BigInteger. Inside that cap the whole of
    it lands on the one number a person feels — how long their other device waits before its note is
    published. Reported as "this signer is slower than amber… waiting over a min".
    """
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    # The CONSTRUCTOR ARGUMENT, not a search of the whole file — the comment above that line has to
    # be free to name the constant it is warning about, and a blanket search made this fail on its
    # own explanation.
    m = re.search(r"new\s+HandlerThread\(([^)]*)\)", svc)
    assert m, "the work thread is gone"
    arg = m.group(1)
    assert "THREAD_PRIORITY_BACKGROUND" not in arg, (
        "the signer's work thread is in Android's background cgroup — every signature it makes is "
        "throttled to a fraction of a core, and the other device waits for it"
    )
    assert "THREAD_PRIORITY_DEFAULT" in arg, (
        "the work thread no longer states its priority; it must be DEFAULT, and deliberately so"
    )


def test_the_signer_never_takes_a_wakelock():
    """A wakelock is how a background service becomes the thing at the top of the battery screen.

    A foreground service already keeps the process resident; a wakelock additionally stops the CPU
    idling, which for a service that is asleep between requests is pure waste. If one is ever needed,
    it needs to be argued for here rather than added quietly.
    """
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    for banned in ("WakeLock", "acquire(", "PARTIAL_WAKE_LOCK"):
        assert banned not in svc, f"the signer service took a wakelock ({banned})"


def test_nothing_paired_means_the_service_stops():
    """Revoking the last app is exactly when someone expects the battery cost to stop.

    A foreground service with no sockets still pins the process and holds a notification, all to
    answer requests that cannot arrive because nothing is paired. A service that lingers there is why
    people report that turning a feature off "didn't do anything".
    """
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    body = svc[svc.index("private void reload()"):]
    body = body[:body.index("private OkHttpClient http()")]
    assert "sessions.isEmpty()" in body and "stopSelf()" in body, (
        "the service keeps running with nothing paired")


def test_the_notification_is_not_rebuilt_for_every_request():
    """One desktop action is several requests — encrypt, wrap, sign.

    Posting a rebuilt Notification for each is a Binder round trip to the system server (and a shade
    animation on some OEM builds) to redisplay a string that did not change.
    """
    svc = _read(os.path.join(SIGNER, "SignerRelayService.java"))
    note = svc[svc.index("private void note()"):]
    note = note[:note.index("\n    }") + 6]
    assert "equals(shown)" in note and "return" in note, (
        "note() posts unconditionally, so every request redraws the notification")


def test_the_signer_does_not_route_the_apps_http_through_okhttp():
    """The app's own HTTP goes through the WebView so it inherits the user's proxy and Tor settings.

    An OkHttp client would silently bypass both. It exists here for one thing — a WebSocket per relay
    — and this asserts it stayed that way.
    """
    import glob
    users = []
    for p in glob.glob(os.path.join(ANDROID, "app", "src", "main", "java", "**", "*.java"),
                       recursive=True):
        if "okhttp3" in _read(p):
            users.append(os.path.basename(p))
    assert users == ["SignerRelayService.java"], (
        f"okhttp has spread beyond the signer's WebSocket: {users}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------------------------
# A link in an email did nothing in the APK. Not a signer bug, but the same shape: correct web
# code made inert by a WebView policy, failing with no error anywhere.
# --------------------------------------------------------------------------------------------

def test_a_target_blank_link_can_actually_open():
    """Mail renders untrusted HTML in a sandboxed iframe with `<base target="_blank">`.

    That is right in a browser and inert in a WebView: `target="_blank"` asks for a NEW WINDOW, and a
    WebView refuses to make one unless multiple windows are enabled AND onCreateWindow is handled.
    Chromium drops the request silently — no navigation, no error, no log line.
    """
    main = _read(os.path.join(ANDROID, "app", "src", "main", "java", "place", "poster", "app",
                              "MainActivity.java"))
    assert "setSupportMultipleWindows(true)" in main, (
        "without this a target=_blank click is discarded before anything is asked")
    assert "onCreateWindow" in main, "multiple windows are enabled with nothing to handle them"
    assert "ACTION_VIEW" in main, "the link is never handed to the system"


def test_the_file_picker_is_not_collateral_damage():
    """`BridgeWebChromeClient` is what serves the file chooser and the camera/mic permission prompts.

    Replacing it with a plain WebChromeClient fixes the link and silently breaks uploads — the kind
    of trade that surfaces weeks later, in a different feature, with no connection to this change.
    """
    main = _read(os.path.join(ANDROID, "app", "src", "main", "java", "place", "poster", "app",
                              "MainActivity.java"))
    assert "BridgeWebChromeClient(getBridge())" in main, (
        "the chrome client no longer extends Capacitor's, so the file chooser and the permission "
        "prompts are gone")


def test_the_throwaway_webview_is_not_destroyed_mid_callback():
    """Tearing a WebView down while it is dispatching to you is a native crash introduced by a fix
    for a dead link."""
    main = _read(os.path.join(ANDROID, "app", "src", "main", "java", "place", "poster", "app",
                              "MainActivity.java"))
    hand = main[main.index("private void hand(final WebView v, Uri target)"):]
    hand = hand[:hand.index("((WebView.WebViewTransport)")]
    assert "v.post(" in hand, "destroy() is called inside the callback rather than posted"
