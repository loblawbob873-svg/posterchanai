"""The Android glue that makes this app a NIP-55 signer for OTHER apps.

The crypto is checked by running it (`test_android_nip55_signer.py`). This checks the wiring, which
Gradle only builds in CI: the intent filter that makes us appear in the signer picker at all, the
plugin registration without which JS never sees the plugin, and the properties that decide whether
this is cheap or expensive.

WHY IT IS AN ACTIVITY AND NOT A SERVICE, since that is the whole efficiency claim. A NIP-55 request
arrives as an Intent; Android starts the Activity, it answers with `setResult`, it finishes, and the
process goes away. Nothing is resident between requests. The relay-based signer it complements has to
keep a WebView alive to be reachable, which costs a foreground service and its battery.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIG = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                   "place", "poster", "app", "signer")
MANIFEST = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "AndroidManifest.xml")
MAIN = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                    "place", "poster", "app", "MainActivity.java")
APP_JS = os.path.join(ROOT, "static", "js", "client", "app.js")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _code(p):
    """The file with its comments removed.

    Necessary rather than fussy: these files EXPLAIN the rules they follow, so a plain text search
    for "getKey" or "<service" matches the sentence saying there is deliberately no getKey, and the
    test fails on correct code while claiming the opposite. Twice now in this codebase.
    """
    src = _read(p)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)      # block and javadoc
    src = re.sub(r"//[^\n]*", "", src)                   # line
    return src


def test_the_nostrsigner_scheme_is_declared():
    """Without this filter no other app can find us; the picker simply never lists PosterChan."""
    m = _read(MANIFEST)
    act = m[m.index(".signer.SignerActivity"):]
    act = act[:act.index("</activity>")]
    assert 'android:scheme="nostrsigner"' in act, "the nostrsigner: scheme is not declared"
    assert 'android:exported="true"' in act, "an unexported activity cannot be invoked by other apps"
    assert "android.intent.action.VIEW" in act


def test_the_signing_prompt_is_not_left_lying_around():
    """What another app asked to sign must not sit in the recents preview afterwards."""
    m = _read(MANIFEST)
    act = m[m.index(".signer.SignerActivity"):]
    act = act[:act.index("</activity>")]
    assert 'android:excludeFromRecents="true"' in act
    assert 'android:noHistory="true"' in act


def test_it_is_an_activity_not_a_service():
    """The efficiency claim, asserted. A service would be resident; an Activity is not."""
    m = re.sub(r"<!--.*?-->", "", _read(MANIFEST), flags=re.S)
    act = m[m.index(".signer.SignerActivity"):]
    assert "<service" not in act[:act.index("</activity>")], \
        "the signer declares a service, which means it is running when nobody is asking"
    src = _code(os.path.join(SIG, "SignerActivity.java"))
    assert "extends Activity" in src
    assert "startForeground" not in src, "the signer starts a foreground service"


def test_the_plugin_is_registered():
    """A plugin that lives in this app is not auto-registered; JS never sees it otherwise."""
    assert "place.poster.app.signer.SignerPlugin.class" in _read(MAIN), \
        "SignerPlugin is not registered, so the client cannot turn the signer on"


def test_the_key_only_travels_one_way():
    """The entire reason to move the key out of the WebView.

    A `getKey` here would hand it back to the layer it was moved away from — and a script in the page
    is exactly what it is being protected against.
    """
    src = _code(os.path.join(SIG, "SignerPlugin.java"))
    for bad in ("getKey", "exportKey", "readKey"):
        assert bad not in src, f"SignerPlugin exposes {bad}, which undoes the whole point"
    assert "SignerKey.store" in src and "SignerKey.clear" in src


def test_the_key_is_sealed_by_the_keystore():
    src = _read(os.path.join(SIG, "SignerKey.java"))
    assert "AndroidKeyStore" in src, "the secret is stored without hardware protection"
    assert "AES/GCM/NoPadding" in src
    assert 'ALIAS = "pcsigner_v1"' in src, \
        "the signer shares the vault's Keystore alias, so revoking one would take the other"


def test_a_refusal_is_remembered():
    """Otherwise a denied app gets a fresh dialog on every retry — a loop with no way out."""
    src = _code(os.path.join(SIG, "SignerActivity.java"))
    assert '"never"' in src and "setGrant" in src


def test_a_failure_is_never_answered_as_success():
    """RESULT_OK with an empty extra is read by clients as a signature.

    That publishes an unsigned event, or stores an empty decryption over a real message — both
    silent, both destructive.
    """
    src = _code(os.path.join(SIG, "SignerActivity.java"))
    tail = src[src.index("} catch (Throwable t) {"):]
    assert "deny(" in tail[:600], "a failed signing falls through to a successful-looking result"
    assert "RESULT_CANCELED" in src


def test_the_client_can_turn_it_on_and_off():
    js = _read(APP_JS)
    assert "_renderNip55" in js, "there is no way to enable the native signer"
    seg = js[js.index("  async function _renderNip55(){"):]
    seg = seg[:seg.index("  async function _signerBackgroundHint(box){")]
    assert "P.enable(" in seg and "P.disable(" in seg
    assert "ME.mode !== 'local'" in seg, \
        "offered to sessions whose key lives in an extension or a remote signer, which have nothing to give"


# APIs that exist on a desktop JDK and NOT at this app's minSdk (23). Each one compiles cleanly
# against compileSdk 35 and throws at runtime on an older phone — and the crypto tests run on a JDK,
# so they are structurally blind to every entry here.
_TOO_NEW = {
    "BigInteger.TWO": 31,          # Java 9; Android API 31
    "BigInteger.ZERO.TWO": 31,
    "java.util.Base64": 26,        # which is why Crypt has its own encoder
    "String.chars(": 24,
    "Objects.requireNonNullElse": 30,
    "List.of(": 30,
    "Map.of(": 30,
    "Set.of(": 30,
    "Optional.isEmpty": 30,
    "String.isBlank": 30,
    "String.repeat(": 30,
    "String.strip(": 30,
}


def test_nothing_in_the_signer_needs_a_newer_android_than_we_support():
    """A green CI build and a dead signer on a third of devices is the failure this prevents.

    `BigInteger.TWO` was exactly that: Java 9, Android API 31, minSdk 23. It compiled against
    compileSdk 35 and would have thrown NoSuchFieldError at the first signature on anything older
    than Android 12. The JDK-based crypto tests pass either way, which is the point — they cannot
    see the platform this actually runs on.
    """
    import glob
    bad = []
    for f in sorted(glob.glob(os.path.join(SIG, "*.java"))):
        code = _code(f)
        for api, since in _TOO_NEW.items():
            if api in code:
                bad.append(f"{os.path.basename(f)}: {api} (Android API {since}, minSdk 23)")
    assert not bad, "APIs newer than minSdk:\n  " + "\n  ".join(bad)


def test_minsdk_is_what_this_check_assumes():
    """If minSdk rises, the list above is too strict and should be revisited rather than obeyed."""
    gradle = _read(os.path.join(ROOT, "mobile", "android", "variables.gradle"))
    m = re.search(r"minSdkVersion\s*=\s*(\d+)", gradle)
    assert m and int(m.group(1)) == 23, \
        "minSdk changed; the _TOO_NEW list in this test is calibrated to 23"


def test_every_spelling_of_the_peer_key_is_read():
    """The ecosystem uses three, and reading one fails silently with the other two.

    NIP-55 documents `pubKey` for the encryption verbs; this app's own client (Nip55Plugin) sends
    `pubkey`; `current_user` is a common fallback. Get it wrong and nip04/nip44 return an error about
    a bad peer key for a request that named it correctly — just not in the spelling we looked for.
    """
    src = _code(os.path.join(SIG, "SignerActivity.java"))
    for spelling in ('"pubKey"', '"pubkey"', '"current_user"'):
        assert spelling in src, f"the signer never reads {spelling}"
    # And our own client really does send the lowercase one, which is why it is not optional.
    client = _code(os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                                "place", "poster", "app", "nip55", "Nip55Plugin.java"))
    assert 'putIfSet(intent, "pubkey"' in client, \
        "Nip55Plugin no longer sends `pubkey`; re-check which spellings the signer must accept"


def test_a_missing_peer_key_is_refused_rather_than_guessed():
    src = _code(os.path.join(SIG, "SignerActivity.java"))
    assert "peer.length() != 64" in src, \
        "a malformed peer key reaches the crypto, where the failure is less legible"


def test_the_event_id_is_not_built_with_org_json():
    """Android's JSONObject escapes `/` as `\\/` — legal JSON, different sha256, wrong event id."""
    src = _code(os.path.join(SIG, "SignerActivity.java"))
    assert "Nostr.eventId(" in src, "the id is not built by the checked serializer"
    ser = _code(os.path.join(SIG, "Nostr.java"))
    assert "public static String serialize(" in ser
    assert re.search(r"case '/'", ser) is None, \
        "the serializer escapes a forward slash, which no other implementation does"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
