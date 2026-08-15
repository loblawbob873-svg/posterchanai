"""The native signer's crypto, checked byte-for-byte against the Python this repo already uses.

`mobile/android/.../signer/Nostr.java` and `Crypt.java` are hand-written secp256k1, BIP-340, NIP-04
and NIP-44 v2. They exist so this app can answer another app's NIP-55 request from an Intent —
without a WebView, and therefore without a foreground service and its battery — but hand-written
crypto that has never been compared to a reference is how you ship a key-destroying bug.

So nothing here asserts what the Java "should" produce. It COMPILES the shipped Java, RUNS it, and
compares against `app/services/nostr/{bip340,nip04,nip44}.py` — the implementation every event this
node has ever signed came from. BIP-340 is deterministic given its aux, and NIP-44 is deterministic
given its nonce, so byte-equality is achievable rather than approximate: two implementations that
merely both "verify" can still disagree, and that disagreement is exactly what must not ship.

The failure modes this is aimed at are the quiet ones. A wrong signature is caught by any relay. A
wrong ECDH or a wrong padding is not caught by anything — it produces ciphertext that no other client
can read, and you learn about it from somebody else's empty DM window.

Gradle only runs in CI, so the Android glue (the Activity, the Keystore) is covered by
`tests/test_android_nip55_provider.py`; what is here is the part that can be executed on any machine
with a JDK, which is the part where a bug is invisible.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                   "place", "poster", "app", "signer")

pytestmark = pytest.mark.skipif(
    shutil.which("javac") is None or shutil.which("java") is None, reason="no JDK")


HARNESS = r"""
// Declared IN the package so it can reach `chacha20` and `paddedLen`, which are deliberately
// package-private: they are internals of NIP-44, not API, and widening them for a test would make
// the test the reason the surface is bigger.
package place.poster.app.signer;

public class Harness {
  public static void main(String[] a) throws Exception {
    String op = a[0];
    if (op.equals("pubkey")) {
      System.out.println(Nostr.hex(Nostr.pubkey(Nostr.unhex(a[1]))));
    } else if (op.equals("sign")) {
      System.out.println(Nostr.hex(Nostr.sign(Nostr.unhex(a[2]), Nostr.unhex(a[1]), Nostr.unhex(a[3]))));
    } else if (op.equals("shared")) {
      System.out.println(Nostr.hex(Nostr.sharedX(Nostr.unhex(a[1]), Nostr.unhex(a[2]))));
    } else if (op.equals("convkey")) {
      System.out.println(Nostr.hex(Crypt.conversationKey(Nostr.unhex(a[1]), Nostr.unhex(a[2]))));
    } else if (op.equals("n44enc")) {
      System.out.println(Crypt.nip44Encrypt(Nostr.unhex(a[1]), a[2], Nostr.unhex(a[3])));
    } else if (op.equals("n44dec")) {
      System.out.println(Crypt.nip44Decrypt(Nostr.unhex(a[1]), a[2]));
    } else if (op.equals("n04enc")) {
      System.out.println(Crypt.nip04Encrypt(Nostr.unhex(a[1]), Nostr.unhex(a[2]), a[3]));
    } else if (op.equals("n04dec")) {
      System.out.println(Crypt.nip04Decrypt(Nostr.unhex(a[1]), Nostr.unhex(a[2]), a[3]));
    } else if (op.equals("padlen")) {
      System.out.println(Crypt.paddedLen(Integer.parseInt(a[1])));
    } else if (op.equals("chacha")) {
      System.out.println(Nostr.hex(Crypt.chacha20(Nostr.unhex(a[1]), Nostr.unhex(a[2]), Nostr.unhex(a[3]))));
    } else if (op.equals("eventid")) {
      System.out.println(Nostr.eventId(a[1], Long.parseLong(a[2]), Integer.parseInt(a[3]), a[4], a[5]));
    } else if (op.equals("tagsjson")) {
      // argv after the op: one tag's values. Built as a LIST, which is the shape both signing paths
      // now hand to Nostr.tagsJson — the point being that no JSON library touches it.
      java.util.List<String> one = new java.util.ArrayList<>();
      for (int i = 1; i < a.length; i++) one.add(a[i]);
      System.out.println(Nostr.tagsJson(java.util.Collections.singletonList(one)));
    } else if (op.equals("serialize")) {
      System.out.println(Nostr.serialize(a[1], Long.parseLong(a[2]), Integer.parseInt(a[3]), a[4], a[5]));
    } else if (op.equals("verify")) {
      System.out.println(Nostr.verify(Nostr.unhex(a[1]), Nostr.unhex(a[2]), Nostr.unhex(a[3])));
    }
  }
}
"""


@pytest.fixture(scope="module")
def java(tmp_path_factory):
    """Compile the SHIPPED Java once. A copy in the test would test the copy."""
    out = tmp_path_factory.mktemp("signerjava")
    hs = out / "Harness.java"
    hs.write_text(HARNESS, encoding="utf-8")
    files = [os.path.join(SRC, f) for f in ("Nostr.java", "Crypt.java", "Native.java")]
    r = subprocess.run(["javac", "-d", str(out / "classes"), *files, str(hs)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail("the signer Java does not compile:\n" + r.stderr[:2000])

    def run(*args):
        p = subprocess.run(["java", "-cp", str(out / "classes"),
                            "place.poster.app.signer.Harness", *[str(a) for a in args]],
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise AssertionError(p.stderr.strip()[:900])
        return p.stdout.strip()
    return run


# A handful of keys with awkward shapes, not just one pretty pair: a leading-zero scalar and a
# high one are where a fixed-width conversion is most likely to be wrong, and a 32-byte big-endian
# bug is invisible on a key whose bytes happen to be uniform.
KEYS = ["11" * 32, "01" + "00" * 30 + "07", "fe" * 32,
        "0000000000000000000000000000000000000000000000000000000000000042"]


def test_it_compiles(java):
    assert java("padlen", 1) == "32"


def test_pubkeys_match_the_reference(java):
    from app.services.nostr import bip340
    for sec in KEYS:
        want = bip340.pubkey_from_seckey(bytes.fromhex(sec)).hex()
        assert java("pubkey", sec) == want, f"pubkey differs for {sec[:8]}…"


def test_signatures_are_byte_identical(java):
    """Deterministic given aux, so anything but byte-equality means the two disagree."""
    from app.services.nostr import bip340
    for sec in KEYS:
        for msg in ("ab" * 32, "00" * 32, "ff" * 32):
            aux = "5a" * 32
            want = bip340.sign(bytes.fromhex(msg), bytes.fromhex(sec), bytes.fromhex(aux)).hex()
            assert java("sign", sec, msg, aux) == want, "signature differs"


def test_it_verifies_what_python_signed(java):
    """The other direction. A signer that cannot read the reference's output is not compatible."""
    from app.services.nostr import bip340
    sec = bytes.fromhex(KEYS[0])
    msg = bytes.fromhex("c1" * 32)
    sig = bip340.sign(msg, sec, bytes.fromhex("07" * 32))
    pub = bip340.pubkey_from_seckey(sec)
    assert java("verify", msg.hex(), pub.hex(), sig.hex()) == "true"


def test_ecdh_matches(java):
    """NIP-04 uses this raw as an AES key, so a mismatch is unreadable ciphertext, not an error."""
    from app.services.nostr import bip340
    from app.services.nostr.nip44 import _ecdh_x
    for sec in KEYS[:3]:
        peer = bip340.pubkey_from_seckey(bytes.fromhex("22" * 32)).hex()
        want = _ecdh_x(bytes.fromhex(sec), bytes.fromhex(peer)).hex()
        assert java("shared", sec, peer) == want


def test_conversation_key_matches(java):
    from app.services.nostr import bip340
    from app.services.nostr.nip44 import get_conversation_key
    sec = KEYS[0]
    peer = bip340.pubkey_from_seckey(bytes.fromhex("33" * 32)).hex()
    want = get_conversation_key(bytes.fromhex(sec), bytes.fromhex(peer)).hex()
    assert java("convkey", sec, peer) == want


def test_chacha20_matches(java):
    """Implemented by hand because Android exposes it only from API 28 and this app supports 23."""
    from app.services.nostr.nip44 import _chacha20
    for data in (b"a", b"x" * 64, b"y" * 65, bytes(range(256))):
        key, nonce = bytes.fromhex("2b" * 32), bytes.fromhex("07" * 12)
        want = _chacha20(key, nonce, data).hex()
        assert java("chacha", key.hex(), nonce.hex(), data.hex()) == want, \
            f"chacha differs at {len(data)} bytes"


def test_padding_matches_across_the_boundaries(java):
    """The padded length changes rule at 32 and again at 256; both are off-by-one country."""
    from app.services.nostr.nip44 import _calc_padded_len
    for n in [1, 2, 31, 32, 33, 63, 64, 65, 255, 256, 257, 511, 512, 1000, 65535]:
        assert int(java("padlen", n)) == _calc_padded_len(n), f"padded length differs at {n}"


def test_nip44_ciphertext_is_byte_identical(java):
    """Deterministic given the nonce, so this compares the whole payload, not just round-tripping."""
    from app.services.nostr import bip340
    from app.services.nostr.nip44 import encrypt, get_conversation_key
    sec = bytes.fromhex(KEYS[0])
    peer = bip340.pubkey_from_seckey(bytes.fromhex("44" * 32))
    ck = get_conversation_key(sec, peer)
    nonce = bytes.fromhex("9e" * 32)
    for text in ["hi", "a" * 200, "unicode — ünï ✓ 日本語 🎉", "x" * 1000]:
        want = encrypt(text, ck, nonce)
        assert java("n44enc", ck.hex(), text, nonce.hex()) == want, "NIP-44 payload differs"


def test_nip44_reads_what_python_wrote(java):
    from app.services.nostr import bip340
    from app.services.nostr.nip44 import encrypt, get_conversation_key
    sec = bytes.fromhex(KEYS[0])
    peer = bip340.pubkey_from_seckey(bytes.fromhex("44" * 32))
    ck = get_conversation_key(sec, peer)
    for text in ["hello", "unicode — ünï ✓ 日本語 🎉", "z" * 900]:
        assert java("n44dec", ck.hex(), encrypt(text, ck)) == text


def test_python_reads_what_the_signer_wrote(java):
    """The direction that matters in practice: another client decrypting our DM."""
    from app.services.nostr import bip340
    from app.services.nostr.nip44 import decrypt, get_conversation_key
    sec = bytes.fromhex(KEYS[0])
    peer_sec = bytes.fromhex("44" * 32)
    peer = bip340.pubkey_from_seckey(peer_sec)
    ck = get_conversation_key(sec, peer)
    payload = java("n44enc", ck.hex(), "from the phone", "3c" * 32)
    assert decrypt(payload, get_conversation_key(peer_sec, bip340.pubkey_from_seckey(sec))) \
        == "from the phone"


def test_a_tampered_nip44_payload_is_refused(java):
    """The MAC is the only thing standing between a peer and chosen ciphertext."""
    from app.services.nostr import bip340
    from app.services.nostr.nip44 import encrypt, get_conversation_key
    sec = bytes.fromhex(KEYS[0])
    ck = get_conversation_key(sec, bip340.pubkey_from_seckey(bytes.fromhex("44" * 32)))
    good = encrypt("secret", ck)
    bad = good[:-4] + ("AAAA" if good[-4:] != "AAAA" else "BBBB")
    with pytest.raises(AssertionError) as e:
        java("n44dec", ck.hex(), bad)
    assert "MAC" in str(e.value) or "version" in str(e.value) or "padding" in str(e.value)


def test_nip04_round_trips_with_python_both_ways(java):
    """NIP-04 has a random IV, so this checks interoperability rather than byte-equality."""
    from app.services.nostr import bip340, nip04
    sec = bytes.fromhex(KEYS[0])
    peer_sec = bytes.fromhex("55" * 32)
    peer = bip340.pubkey_from_seckey(peer_sec)
    mine = bip340.pubkey_from_seckey(sec)

    payload = java("n04enc", sec.hex(), peer.hex(), "hello from java")
    assert nip04.decrypt(peer_sec, mine, payload) == "hello from java"

    other = nip04.encrypt(peer_sec, mine, "hello from python")
    assert java("n04dec", sec.hex(), peer.hex(), other) == "hello from python"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_event_ids_match_the_reference(java):
    """A wrong event id is a valid signature over the wrong thing.

    Relays that check it reject the event; relays that do not store something nobody can verify. It
    is the single most consequential byte string in the protocol, and it is produced by string
    concatenation, which is where escaping bugs live.

    Written by hand rather than through org.json ON PURPOSE: Android's `JSONObject.toString()`
    escapes a forward slash as `\\/`. That is legal JSON and a DIFFERENT sha256 — so every event
    containing a URL would have been signed with an id no other implementation computes.
    """
    from app.services.nostr import bip340
    from app.services.nostr.event import _canonical
    import hashlib
    pub = bip340.pubkey_from_seckey(bytes.fromhex(KEYS[0])).hex()
    cases = [
        (1700000000, 1, "[]", "hello"),
        # The slash case, which is the whole reason this is not org.json.
        (1700000001, 1, "[]", "see https://poster.place/client for details"),
        (1700000002, 1, '[["e","' + "ab" * 32 + '"],["p","' + "cd" * 32 + '"]]', "reply"),
        (1700000003, 5, "[]", ""),
        (1700000004, 1, "[]", 'quotes " and \\ backslash and\\nnewline\\ttab'),
        (1700000005, 1, "[]", "unicode — ünï ✓ 日本語 🎉"),
    ]
    for created, kind, tags, content in cases:
        want = hashlib.sha256(_canonical(pub, created, kind, json.loads(tags), content)).hexdigest()
        got = java("eventid", pub, created, kind, tags, content)
        assert got == want, f"event id differs for content={content[:40]!r}"


def test_the_serialization_itself_matches(java):
    """Compared as bytes, so a failure says WHERE the two differ rather than just that they do."""
    from app.services.nostr import bip340
    from app.services.nostr.event import _canonical
    pub = bip340.pubkey_from_seckey(bytes.fromhex(KEYS[0])).hex()
    content = 'a "quoted" / slashed \\ path'
    want = _canonical(pub, 1700000000, 1, [], content).decode("utf-8")
    assert java("serialize", pub, 1700000000, 1, "[]", content) == want

def test_a_tag_with_a_url_in_it_serializes_like_every_other_client(java):
    """QUOTE POSTS. The tags were serialized with `ev.getJSONArray("tags").toString()`, and Android's
    org.json renders a forward slash as `\\/` — legal JSON, different bytes, different event id.

    Nothing broke while no tag held a slash: `client`, `p` and `e` are names and hex. A quote post is
    the first tag with a URL in it — `["q", <id>, "wss://poster.place/relay", <pubkey>]` — so the
    phone hashed `wss:\\/\\/poster.place\\/relay`, signed THAT id, and the relay (which recomputes from
    the tags as received) refused the event. Reported as "quote posts go into infinite pending state
    and never get posted" while ordinary posts and replies were fine. `imeta`, which carries an
    uploaded image's URL, is the same shape and was next.
    """
    import hashlib
    from app.services.nostr import bip340
    from app.services.nostr.event import _canonical

    tag = ["q", "ab" * 32, "wss://poster.place/relay", "cd" * 32]
    got = java("tagsjson", *tag)
    assert "\\/" not in got, f"a forward slash is being escaped: {got}"
    assert got == json.dumps([tag], separators=(",", ":"), ensure_ascii=False), got

    # …and the id that comes out of it agrees with the reference implementation.
    pub = bip340.pubkey_from_seckey(bytes.fromhex(KEYS[0])).hex()
    want = hashlib.sha256(_canonical(pub, 1700000006, 1, [tag], "look at this")).hexdigest()
    assert java("eventid", pub, 1700000006, 1, got, "look at this") == want


def test_every_kind_of_tag_value_survives_the_hand_serializer(java):
    """The serializer is hand-written, so the escapes it DOES need are worth pinning: a quote, a
    backslash, a newline and a tab all appear in real tag values (alt text, content warnings)."""
    tag = ["alt", 'he said "hi"', "back\\slash", "two\nlines", "a\tb", "ünï ✓ 日本語"]
    got = java("tagsjson", *tag)
    assert got == json.dumps([tag], separators=(",", ":"), ensure_ascii=False), got

