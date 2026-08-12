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

And, since it became TWO WAY, every way the second direction eats data:

  * the push runs before the pull, so an edit made on the phone is overwritten by the app's stale
    copy before anything read it — no error, no trace;
  * DIRTY is cleared for a row whose change was never stored, which marks it uploaded for ever;
  * a deletion made on the phone is read as "a card the phone is missing" and re-inserted, which the
    next pull deletes again, for as long as the app is open;
  * a contact created on the phone is pruned before it has been stored, or gets a new uid on every
    sweep and becomes several people;
  * the edit schema offers a field we do not round-trip, so the user watches themself type something
    that disappears on the next push.

The Java is also TYPE-CHECKED here, against the hand-written stubs in tests/androidstubs — a broken
column constant is a failing test in a second rather than a broken APK build on CI.
"""
import os
import re
import shutil
import subprocess

import pytest

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
READER = _read(JAVA, "contacts", "ContactReader.java")
VCARD_JS = _read(ROOT, "static", "js", "client", "vcard.js")
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
    # Every name the client asks for has to be one Java answers to. A typo here is invisible: the
    # lookup returns null, which every caller reads as "not the packaged app".
    asked = set(re.findall(r"capPlugin\(\s*'([A-Za-z]+)'", CONTACTS_JS))
    assert asked == {"ContactSync"}, f"contacts.js asks for {asked}"


def test_the_plugin_is_registered_before_the_bridge_is_built():
    """registerPlugin() only counts BEFORE super.onCreate(): that is where Capacitor builds the
    bridge and writes the plugin map into the WebView. Registered after, the class is loaded, the
    APK builds, every test that greps for the call passes — and JS never sees the plugin."""
    body = re.search(r"public void onCreate\(Bundle[^)]*\)\s*\{(.*?)\n    \}", MAIN, re.S)
    assert body, "MainActivity.onCreate not found"
    inner = body.group(1)
    reg = inner.index("registerPlugin(place.poster.app.contacts.ContactSyncPlugin.class)")
    sup = inner.index("super.onCreate(")
    assert reg < sup, "ContactSync is registered after the bridge is built — JS will never see it"


def test_the_lookup_does_not_depend_on_a_function_this_bundle_does_not_ship():
    """`Capacitor.registerPlugin` is @capacitor/core's, and the APK does not ship @capacitor/core:
    the only Capacitor JS in it is the WebView-injected native-bridge, which defines getPlatform /
    nativePromise / nativeCallback and NOT registerPlugin. So the documented fallback is a no-op
    here and `Capacitor.Plugins[name]` was the ONLY path there has ever been — which is why a bridge
    that did not arrive made every native feature report "not the packaged app" and say nothing."""
    assert "_rawNative()" in APPJS, "capPlugin lost its raw-channel fallback"
    assert "window.androidBridge" in APPJS, "the WebView's own channel is the layer under all of it"
    assert "cap.nativePromise(pluginId, methodName" in APPJS
    bridge = os.path.join(ROOT, "mobile", "node_modules", "@capacitor", "android", "capacitor",
                          "src", "main", "assets", "native-bridge.js")
    if not os.path.exists(bridge):
        pytest.skip("mobile/node_modules not installed")
    src = _read(bridge)
    assert "registerPlugin" not in src, (
        "native-bridge.js now ships registerPlugin — re-check whether the raw fallback is still needed")
    assert "cap.nativePromise =" in src, "the raw fallback rides nativePromise when it is there"


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
    # The edit schema is what lets the phone's Contacts app edit our cards and save new ones into
    # this account. Its CONTENTS are pinned separately, below.
    assert "android.provider.CONTACTS_STRUCTURE" in body
    assert "@xml/contacts_structure" in body
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
    # Now that the phone can edit our cards it matters a second time, and in the other direction: a
    # write WITHOUT the parameter is recorded as a user edit, so every push would mark the whole
    # address book dirty and the next pull would upload our own writes back as the user's.
    for call in re.findall(r"getContentResolver\(\)\.(?:delete|update)\(\s*([A-Za-z_.()]+)", READER):
        assert call.startswith("ContactWriter.syncUri"), \
            f"the reader writes through a plain URI ({call}) — that is a user edit, not a sync"


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


# --------------------------------------------------------------------------------------------
# TWO WAY: the phone's own edits, coming back.
# --------------------------------------------------------------------------------------------

def test_the_sweep_pulls_before_it_pushes():
    """The order is the whole design. A push is an overwrite of the phone's copy, so pushing first
    destroys an edit made there before anything has read it — no error, nothing in any log, and the
    user finds out when they ring the old number."""
    sweep = re.search(r"function syncPhonebook\(force\)\{(.*?)\n    \}", CONTACTS_JS, re.S)
    assert sweep, "syncPhonebook moved — re-point this test"
    body = sweep.group(1)
    pull = body.index("pullPhone(")
    push = body.index("pushPhonebook(")
    assert pull < push, "the push must not run before the pull"
    # …and what the pull stored has to be re-read, or the push sends the state from before the merge
    # and undoes the phone's edit on the phone.
    assert "await load()" in body[pull:push], \
        "the merged cards must be re-read before the push, or the push sends stale state"
    # The screen's own load ends in a full sweep, not a bare push.
    assert "try{ syncPhonebook(); }catch(_){}" in CONTACTS_JS


def test_dirty_is_cleared_only_for_rows_the_app_actually_stored():
    """RawContacts.DIRTY is the only record that the phone changed something. Clearing it for a row
    whose change was not stored marks that edit as uploaded for ever — the same shape as recording a
    hash for a batch the provider refused."""
    taken = re.search(r"public static JSONArray taken\(Context ctx, JSONArray rows\) \{(.*?)\n  \}",
                      READER, re.S)
    assert taken, "ContactReader.taken moved — re-point this test"
    body = taken.group(1)
    # Only the rows handed in, and only at the version they were read at.
    assert "rows.optJSONObject(i)" in body
    assert re.search(r'RawContacts\._ID \+ "=\? AND " \+ RawContacts\.VERSION \+ "=\?', body), \
        "clearing DIRTY without a version guard loses an edit made while the sweep was in flight"
    assert "v.put(RawContacts.DIRTY, 0)" in body
    # A row whose clear did not take is NOT reported as cleared.
    assert "if (ok) cleared.put(rawId);" in body
    # The client only ever acknowledges what it stored: the ack is built after the write, in the
    # same iteration, and a throw skips it.
    pull_js = re.search(r"async function pullPhone\(\)\{(.*?)\n    \}", CONTACTS_JS, re.S)
    assert pull_js, "pullPhone moved — re-point this test"
    js = pull_js.group(1)
    assert js.index("ack.push(") > js.index("await jput("), \
        "a row must be acknowledged after its save, never before"
    assert "}catch(_){ /* not acknowledged" in js


def test_a_deletion_made_on_the_phone_is_never_re_added():
    """A tombstone (DELETED=1) is the only record of the deletion, and the app's copy still holds
    that person — which is exactly what a push writes back. Re-inserted, the next pull deletes them
    again, and the two halves fight for as long as the app is open."""
    # A tombstone is not a live row…
    assert 'RawContacts.DELETED + "=0"' in WRITER, \
        "existing() must exclude tombstones, or a push writes onto a deleted contact"
    # …and neither half of the push touches a uid with an unacknowledged phone-side change.
    assert "public static Set<String> pending(Context ctx)" in READER
    assert "if (skip != null && skip.contains(uid)) continue;" in WRITER
    assert "if (hold != null && hold.contains(e.getKey())) continue;" in WRITER
    put = re.search(r"public void put\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S)
    commit = re.search(r"public void commit\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S)
    assert put and commit
    assert "ContactReader.pending(getContext())" in put.group(1), \
        "put() must hold back cards the phone has changed"
    assert "ContactReader.pending(getContext())" in commit.group(1), \
        "the reconcile must hold back a contact created on the phone and not yet stored"
    # And the client treats a deletion as a deletion rather than a card to write.
    assert "action === 'delete'" in CONTACTS_JS and "method:'DELETE'" in CONTACTS_JS
    assert "action: have ? 'delete' : 'drop'" in VCARD_JS


def test_a_contact_created_on_the_phone_becomes_exactly_one_card():
    """It has no uid of ours. Minting one in the CLIENT and losing it to a crash before the card
    reaches the server would mint a second one next sweep — one person, two cards, for ever."""
    mint = re.search(r"public static int mintSourceIds\(Context ctx\) \{(.*?)\n  \}", READER, re.S)
    assert mint, "ContactReader.mintSourceIds moved — re-point this test"
    body = mint.group(1)
    assert "UUID.randomUUID()" in body
    assert "v.put(RawContacts.SOURCE_ID," in body, "the uid must be written onto the row"
    assert "IS NULL OR " in body and "SOURCE_ID" in body, \
        "the write must be guarded on the row still having no uid"
    # Minted BEFORE the versions are read — writing SOURCE_ID bumps RawContacts.VERSION, and a
    # version read before that write would never match at acknowledge time.
    pull = re.search(r"public void pull\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S)
    assert pull, "ContactSyncPlugin.pull moved — re-point this test"
    pbody = pull.group(1)
    assert pbody.index("mintSourceIds") < pbody.index("ContactReader.changes"), \
        "uids must be stamped before the rows (and their versions) are read"
    # A row with no uid is not reported at all — it gets one on the next pass instead of being
    # uploaded as an anonymous card.
    assert 'if (uid == null || uid.isEmpty()) continue;' in READER
    # …and the client stores it under the uid the row now carries, never a fresh one.
    assert "made.uid = uid;" in VCARD_JS


def test_the_edit_schema_offers_exactly_what_we_round_trip():
    """A field the phone offers and we then discard is worse than one it never showed: the user
    watches themself type it and it disappears on the next push, hours later, with nothing said."""
    assert "<EditSchema>" in STRUCT_XML, \
        "no edit schema = a read-only account: the phone cannot edit or add contacts at all"
    kinds = set(re.findall(r'<DataKind\s+kind="([a-zA-Z]+)"', STRUCT_XML))
    assert kinds == {"name", "phone", "email", "organization", "postal", "event", "note"}, kinds
    # Every kind above is written by ContactWriter AND read back by ContactReader — the two lists
    # are the definition of "round-trip".
    for mime in ("StructuredName", "Phone", "Email", "Organization", "StructuredPostal", "Note",
                 "Event"):
        assert f"{mime}.CONTENT_ITEM_TYPE" in WRITER, mime
        assert f"{mime}.CONTENT_ITEM_TYPE.equals(mime)" in READER, f"{mime} is written but not read"
    # Photo is deliberately NOT editable on the phone: it is written from the app and cannot be read
    # back reliably (the provider hands us its own thumbnail, not the bytes we sent).
    assert 'kind="photo"' not in STRUCT_XML
    for never in ("nickname", "website", "im", "relation", "sip_address", "groupMembership"):
        assert f'kind="{never}"' not in STRUCT_XML, f"{never} is offered and then discarded"
    # A birthday is a vCard BDAY; an anniversary is an Event row with nowhere to go.
    assert 'type="birthday"' in STRUCT_XML and 'type="anniversary"' not in STRUCT_XML


def test_a_push_only_rewrites_the_rows_this_feature_owns():
    """The push used to delete every data row on the contact and rebuild it. With the phone able to
    edit them, that also destroys anything another app put there — the same mistake vcard.js exists
    to prevent on the card itself."""
    assert "MANAGED_MIMES" in WRITER
    assert "managedSelection()" in WRITER and "managedArgs(rawId)" in WRITER
    build = WRITER[WRITER.index("private static void buildCard("):WRITER.index("/** A Data row that")]
    assert 'Data.RAW_CONTACT_ID + "=?", new String[]{String.valueOf(rawId)}' not in build, \
        "deleting every data row destroys the fields the phone owns"


def test_a_phone_edit_keeps_what_the_phone_cannot_model():
    """The merge is vcard.js's, so the photo, the Apple-style grouped labels, the foreign PRODID and
    every X-* field survive an edit made on the phone. tests/test_vcard.py asserts the behaviour; this
    asserts the client actually goes through it rather than building a card from eight fields."""
    assert "V().phonePlan(" in CONTACTS_JS
    assert "V().serialize(step.card)" in CONTACTS_JS
    assert "out.other = (card && card.other) ? card.other.slice() : (out.other || []);" in VCARD_JS
    # The loser of a conflict is stored BEFORE the winner overwrites it.
    plan = re.search(r"async function pullPhone\(\)\{(.*?)\n    \}", CONTACTS_JS, re.S).group(1)
    assert plan.index("step.copy") < plan.index("step.card"), \
        "the conflict copy must be written before the version it is a copy of is replaced"


def test_a_pull_checks_the_account_it_is_reading_for():
    """Without it, the first sweep after somebody else signs in on this phone uploads the PREVIOUS
    user's phone book into the new account — a leak, not a mess."""
    pull = re.search(r"public void pull\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S).group(1)
    assert 'ownerGuard(call.getString("owner", ""))' in pull
    assert "if (!wiped) {" in pull, "a wipe must not then read the rows it just removed"
    assert "ContactWriter.wipe(getContext());" in PLUGIN


def test_the_java_type_checks_against_the_platform_stubs():
    """javac over the real sources. The Gradle build only runs on CI, so without this a wrong column
    constant or a method that does not exist is found half an hour later, by a robot."""
    if shutil.which("javac") is None:
        pytest.skip("no JDK")
    import tempfile
    src = [os.path.join(JAVA, "contacts", f) for f in
           ("ContactWriter.java", "ContactReader.java", "ContactSyncPlugin.java")]
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run(
            ["javac", "-nowarn", "-d", out,
             "-sourcepath", os.path.join(ROOT, "tests", "androidstubs") + os.pathsep
                            + os.path.join(ANDROID, "src", "main", "java")] + src,
            capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]


def test_the_push_is_skipped_when_nothing_changed():
    """It runs at the end of every load and from app start. Sending every base64 PHOTO across the
    bridge each time is the cost that would make it unusable on a real address book."""
    assert "if(!force && sig === _pushSig) return;" in CONTACTS_JS
    assert "if(known[c.uid] === c.h) continue;" in CONTACTS_JS
    # The plugin's hashes are a claim about the phone; the raw contacts are the fact. A card the user
    # deleted by hand must come back rather than be skipped for ever as "unchanged".
    assert "if (have.containsKey(uid)) hashes.put(uid" in PLUGIN


# ---- the row itself, on a packaged build ------------------------------------------------------
#
# THE BUG THIS BLOCK EXISTS FOR. On the APK the ⋯ → Addressbooks panel showed no phone-book row at
# all — not the switch, and not the sentence written to explain the switch's absence either — so a
# detection bug was indistinguishable from a browser, and from an APK built before the feature. The
# cause is a shape a grep cannot see: `phonebookRow` returned '' whenever `Capacitor.getPlatform()`
# was not 'android', i.e. it asked the very object that can be missing. Reproduced below by running
# the shipped contacts.js in each world a half-arrived Capacitor produces (contacts_device_sim.js);
# `raw-bridge` — none of Capacitor's JS in the page, only the WebView's own Java channel — is the one
# that rendered nothing, and now renders a working switch.

DEVICE_SIM = os.path.join(ROOT, "tests", "client", "contacts_device_sim.js")
PACKAGED = ("full", "plugin-map-only", "raw-bridge", "raw-bridge-no-plugin",
            "no-capacitor", "no-plugin")


def _device(env):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    import json
    r = subprocess.run(["node", DEVICE_SIM, json.dumps({"env": env})],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout)


@pytest.mark.parametrize("env", PACKAGED)
def test_the_phone_book_row_is_never_empty_on_a_packaged_build(env):
    res = _device(env)
    assert res["row"], f"{env}: the packaged app rendered NO phone-book row at all"
    assert res["hasSwitch"] or res["hasRetry"], f"{env}: neither a switch nor a way to re-ask"
    if not res["hasSwitch"]:
        # No switch is allowed only when it SAYS why, and names the piece that is missing — the next
        # report has to be diagnosable rather than "nothing happens".
        assert res["saysUpdate"] or res["saysBridge"], f"{env}: a missing switch with no reason"
        assert "platform=" in res["why"] and "plugin=" in res["why"], f"{env}: no diagnostics"


def test_a_page_with_no_capacitor_js_at_all_still_reaches_the_plugin():
    """The reported failure, reproduced: `window.Capacitor` never reached the page. Pre-fix this
    rendered an empty string; the WebView's own androidBridge was there the whole time."""
    res = _device("raw-bridge")
    assert res["hasSwitch"], "the switch is missing on a phone whose native channel is up"
    assert ["status", {}] in res["nativeCalls"], "the probe never reached native"


@pytest.mark.parametrize("env", ("browser", "web-on-android"))
def test_the_row_stays_away_from_anything_that_is_not_the_packaged_app(env):
    """The other half. In a browser — including Chrome on an Android phone — there is no plugin to
    offer and CardDAV really is the answer, so an over-eager detector would put a switch that cannot
    work in front of every web user."""
    res = _device(env)
    assert not res["row"], f"{env}: offered a phone-book switch that cannot exist"
