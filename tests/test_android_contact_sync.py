"""The APK writing your PosterChan contacts into the PHONE'S OWN Contacts app.

Same standing as tests/test_android_music_controls.py, and for the same reason: there is no device
here and the Gradle build runs on CI, so what is guarded is the WIRING — which is where this feature
fails silently. Every assertion below is a way for the phone book to end up wrong, or stale, or full
of somebody else's people, with nothing in any log to say so:

  * the plugin is not registered in MainActivity — a plugin living in this app is not auto-discovered,
    so `Capacitor.Plugins.ContactSync` is absent, the client's guarded lookup finds nothing, and the
    switch simply never appears. Exactly how the same trap ate a whole session on Folder Sync.
  * the JS and the Java disagree on the plugin's NAME, which fails identically.
  * the account type drifts between the Java constant, the authenticator XML and the manifest — the
    phone then keeps the old account (with the old contacts in it) while we write into a new one
    nothing displays.
  * the authenticator service is missing or unexported: no account type, so a RawContact under it
    cannot exist at all.
  * READ_CONTACTS or WRITE_CONTACTS goes missing from the manifest — the runtime request then
    resolves "denied" for a permission the app never declared.
  * CALLER_IS_SYNCADAPTER comes off the URIs. A delete then only sets DELETED=1 and the row stays,
    so a contact deleted in the web UI lives on in the dialer — the exact bug docs/CONTACTS.md
    already records against the CardDAV path.
  * the reconcile loses its DELETE half and the phone book only ever grows.
  * the batch gets flushed mid-card, which silently attaches one person's data rows to another
    (withValueBackReference indexes into the batch being applied).
  * the switch stops defaulting to OFF, or sign-out stops wiping the device copy.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


MANIFEST = _read(ANDROID, "src", "main", "AndroidManifest.xml")
MAIN = _read(JAVA, "MainActivity.java")
PLUGIN = _read(JAVA, "contacts", "ContactSyncPlugin.java")
WRITER = _read(JAVA, "contacts", "ContactWriter.java")
AUTH = _read(JAVA, "contacts", "PosterChanAuthenticator.java")
AUTHSVC = _read(JAVA, "contacts", "AuthenticatorService.java")
AUTH_XML = _read(ANDROID, "src", "main", "res", "xml", "contacts_authenticator.xml")
STRUCT_XML = _read(ANDROID, "src", "main", "res", "xml", "contacts_structure.xml")
CONTACTS_JS = _read(ROOT, "static", "js", "client", "contacts.js")
APPJS = _read(ROOT, "static", "js", "client", "app.js")

ACCOUNT_TYPE = "place.poster.app.contacts"


def test_plugin_is_registered_and_named_the_same_on_both_sides():
    assert "registerPlugin(place.poster.app.contacts.ContactSyncPlugin.class)" in MAIN
    name = re.search(r'@CapacitorPlugin\(\s*name\s*=\s*"([^"]+)"', PLUGIN)
    assert name, "ContactSyncPlugin lost its @CapacitorPlugin name"
    assert name.group(1) == "ContactSync"
    # …and that is the name the client asks for, through capPlugin — which is `registerPlugin` with a
    # fallback. `Capacitor.Plugins.<name>` is EMPTY for a plugin registered in Java with no JS package
    # of its own, and reading the map directly is how this feature would be invisible in the one build
    # it exists in.
    assert "PC.capPlugin ? PC.capPlugin('ContactSync'" in CONTACTS_JS
    assert "cap.registerPlugin(name)" in APPJS, "capPlugin must fall back to registerPlugin"


def test_the_account_type_is_the_same_string_everywhere():
    java = re.search(r'ACCOUNT_TYPE\s*=\s*"([^"]+)"', WRITER)
    assert java and java.group(1) == ACCOUNT_TYPE
    assert f'android:accountType="{ACCOUNT_TYPE}"' in AUTH_XML


def test_the_authenticator_is_declared_the_way_the_platform_binds_it():
    svc = re.search(r"<service\b[^>]*\.contacts\.AuthenticatorService[^>]*>.*?</service>",
                    MANIFEST, re.S)
    assert svc, "AuthenticatorService is not declared — the account type does not exist"
    body = svc.group(0)
    # The binder is the SYSTEM's, so this one has to be exported…
    assert 'android:exported="true"' in body
    assert "android.accounts.AccountAuthenticator" in body
    assert "@xml/contacts_authenticator" in body
    # …and guarded by answering only that action, or the authenticator goes to whoever asks.
    assert "ACTION_AUTHENTICATOR_INTENT.equals(intent.getAction())" in AUTHSVC
    # A one-way sync means the phone's Contacts app must not offer to edit our cards: an account type
    # with no edit schema is read-only to AOSP-derived Contacts apps.
    assert "android.provider.CONTACTS_STRUCTURE" in body
    assert "@xml/contacts_structure" in body
    assert "ContactsDataKind" not in STRUCT_XML, \
        "declaring data kinds gives the account an edit schema, i.e. an Edit button we overwrite"
    # Settings → Accounts → Add account lists every registered type; returning null there is a dead
    # entry, so addAccount opens the app instead.
    assert "KEY_INTENT" in AUTH and "MainActivity.class" in AUTH


def test_both_contacts_permissions_are_declared():
    assert 'android:name="android.permission.READ_CONTACTS"' in MANIFEST
    assert 'android:name="android.permission.WRITE_CONTACTS"' in MANIFEST
    # Both in ONE alias, so Android asks once (they are the same permission group).
    perm = re.search(r'@Permission\(alias\s*=\s*"contacts",\s*strings\s*=\s*\{(.*?)\}\)',
                     PLUGIN, re.S)
    assert perm, "the plugin declares no contacts permission alias"
    assert "READ_CONTACTS" in perm.group(1) and "WRITE_CONTACTS" in perm.group(1)
    # Asked for when the switch is flipped, not at app start — the reason has to be on screen.
    assert 'requestPermissionForAlias("contacts", call, "contactsPermission")' in PLUGIN
    assert "@PermissionCallback" in PLUGIN


def test_every_write_goes_through_the_sync_adapter_uri():
    """Without CALLER_IS_SYNCADAPTER the provider treats our writes as user edits — and a DELETE
    becomes a tombstone (DELETED=1) rather than a delete, so a contact removed in the web UI stays on
    the phone and can be edited back into existence."""
    assert "ContactsContract.CALLER_IS_SYNCADAPTER" in WRITER
    for call in re.findall(r"ContentProviderOperation\.new(?:Insert|Delete|Update)\(([^)]*)\)", WRITER):
        assert "syncUri(" in call, f"provider operation on a plain URI: {call}"
    for call in re.findall(r"getContentResolver\(\)\.delete\(\s*([A-Za-z_.()]+)", WRITER):
        assert call.startswith("syncUri"), f"delete on a plain URI ({call}) only tombstones the row"


def test_the_reconcile_deletes_as_well_as_inserts():
    """The half that is easy to leave out. Without it the phone book only ever grows."""
    commit = re.search(r"public void commit\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S)
    assert commit, "ContactSyncPlugin.commit moved — re-point this test"
    body = commit.group(1)
    assert "ContactWriter.prune(" in body
    assert 'call.reject("commit needs the full uid list")' in body, \
        "a missing uid list must refuse, never be read as 'delete everything'"
    prune = re.search(r"public static int prune\(.*?\n  \}", WRITER, re.S)
    assert prune and "deleteRaw(" in prune.group(0) and "keep.contains" in prune.group(0)
    # …and the client must call it on EVERY sweep, including one that wrote nothing — a deletion is
    # the one change that produces no card to push.
    assert "await P.commit({ uids:" in CONTACTS_JS


def test_a_batch_is_never_flushed_in_the_middle_of_a_card():
    """withValueBackReference indexes into the batch being applied. A chunk boundary inside a card
    points its data rows at whatever sits at that index in the NEXT batch — one person's phone number
    on somebody else's contact, with no error anywhere."""
    write = re.search(r"public static Set<String> write\(.*?\n  \}", WRITER, re.S)
    assert write, "ContactWriter.write moved — re-point this test"
    body = write.group(0)
    flush = body.index("if (ops.size() >= BATCH_OPS)")
    build = body.index("buildCard(ops,")
    assert build < flush, "the flush must come after a whole card has been built"
    # Exactly two flushes: the one guarded by the card boundary, and the final one after the loop.
    assert body.count("apply(ctx, ops)") == 2, \
        "an extra apply() in write() is an extra chance to split a card across two batches"
    build_body = WRITER[WRITER.index("private static void buildCard("):
                        WRITER.index("/** A Data row that")]
    assert "apply(" not in build_body, \
        "buildCard must never apply a batch — that is what puts a boundary inside a card"


def test_a_refused_batch_is_not_remembered_as_written():
    """applyBatch is a transaction: a refused chunk leaves those rows at their OLD contents. Record
    the new hash anyway and that person is 'already up to date' for ever — their new number never
    reaches the phone, and nothing anywhere says so. That is why write() returns the UIDs that landed
    rather than a count."""
    assert "public static Set<String> write(" in WRITER
    assert re.search(r"if \(apply\(ctx, ops\)\) ok\.addAll\(pending\);", WRITER)
    assert "private static boolean apply(" in WRITER
    assert "!landed.contains(uid) || !after.containsKey(uid)" in PLUGIN


def test_signing_out_takes_the_phones_copy_with_it():
    """Otherwise a handed-down phone keeps the previous account's people in its dialer, its share
    sheet and every messaging app on it."""
    assert "forgetDevice()" in CONTACTS_JS
    assert "_forgetPhonebook()" in APPJS
    logout = re.search(r"function logout\(\)\{(.*?)\n\n", APPJS, re.S)
    assert logout and "_forgetPhonebook().then(()=> location.reload())" in logout.group(1), \
        "logout must wipe before the reload cuts the bridge call off"
    switch = re.search(r"function _accountSwitch\(a\)\{(.*?)\n  \}", APPJS, re.S)
    assert switch and "_forgetPhonebook()" in switch.group(1)
    # Removing the ACCOUNT is what makes it complete — the provider deletes every row under it.
    assert "removeAccountExplicitly" in WRITER
    # …and the belt to that brace: a session that ended without the JS call is caught on the next
    # push, by owner.
    assert 'prefs().getString(KEY_OWNER, "")' in PLUGIN
    assert "ContactWriter.wipe(getContext());" in PLUGIN


def test_it_is_off_by_default_and_asks_before_it_writes():
    assert "CSet().get(PHONE_KEY, false)" in CONTACTS_JS, "the switch must default to OFF"
    # A refusal breaks nothing: the switch goes back and the user is told.
    assert "box.checked = false; CSet().set(PHONE_KEY, false);" in CONTACTS_JS
    # A permission revoked in system settings AFTER the switch was turned on must turn it back off,
    # not leave a switch that says "on" while nothing is ever written.
    assert "st.granted === false" in CONTACTS_JS


def test_the_push_is_skipped_when_nothing_changed():
    """It runs at the end of every load and from app start. Sending every base64 PHOTO across the
    bridge each time is the cost that would make it unusable on a real address book."""
    assert "if(!force && sig === _pushSig) return;" in CONTACTS_JS
    assert "if(known[c.uid] === c.h) continue;" in CONTACTS_JS
    # The plugin's hashes are a claim about the phone; the raw contacts are the fact. A card the user
    # deleted by hand must come back rather than be skipped for ever as "unchanged".
    assert "if (have.containsKey(uid)) hashes.put(uid" in PLUGIN
