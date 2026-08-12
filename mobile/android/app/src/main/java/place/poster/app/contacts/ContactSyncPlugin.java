package place.poster.app.contacts;

import android.Manifest;
import android.content.Context;
import android.content.SharedPreferences;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashSet;
import java.util.Iterator;
import java.util.Map;
import java.util.Set;

/**
 * The WebView's end of "put my PosterChan contacts in the phone's own Contacts app".
 *
 * SAME SPLIT AS THE MUSIC CONTROLS AND THE CALENDAR WIDGET, for the same reason: the data is an
 * encrypted Nostr event and the client is what holds the session that can read it, so JS hands over
 * cards that are already plain and this writes them into ContactsContract. Nothing here parses a
 * vCard, talks to a relay or decrypts anything — a second vCard parser in Java is how the phone book
 * and the app end up disagreeing about somebody's phone number.
 *
 * ONE WAY, app → phone. Edits made on the phone are not read back; the next push overwrites them.
 * That is stated in docs/CONTACTS.md rather than left to be discovered, and it is why the account
 * type declares no edit schema (res/xml/contacts_structure.xml) — an AOSP-derived Contacts app then
 * shows our cards as read-only, which matches what actually happens.
 *
 * THREE CALLS PER SWEEP, and the shape is what keeps it cheap:
 *   begin(owner) → the hash of every card we last wrote. JS diffs against that and sends only what
 *                  changed. Without this the client would have to push every base64 PHOTO across the
 *                  bridge on every visit to the screen — the same "4 bytes of string per 3 of data,
 *                  held as UTF-16" cost that sizes folder sync's chunks, except paid for nothing.
 *   put(cards)   → upsert a batch, keyed on the card's UID.
 *   commit(uids) → the reconcile: anything of ours NOT in that list is deleted from the phone.
 *
 * `owner` is the signed-in pubkey. It is compared on every begin() and a mismatch wipes first — the
 * belt to sign-out's braces, for the phone that was killed before the client could say goodbye.
 */
@CapacitorPlugin(
    name = "ContactSync",
    permissions = { @Permission(alias = "contacts", strings = {
        Manifest.permission.READ_CONTACTS, Manifest.permission.WRITE_CONTACTS }) }
)
public class ContactSyncPlugin extends Plugin {

  private static final String PREFS = "pc_contact_sync";
  private static final String KEY_OWNER = "owner";
  private static final String KEY_HASHES = "hashes";

  private SharedPreferences prefs() {
    return getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
  }

  private boolean granted() {
    return getPermissionState("contacts") == PermissionState.GRANTED;
  }

  /** What the client shows on the switch: whether this build can do it, and whether it is on. */
  @PluginMethod
  public void status(PluginCall call) {
    JSObject out = new JSObject();
    boolean ok = granted();
    boolean acct = ContactWriter.hasAccount(getContext());
    out.put("granted", ok);
    out.put("account", acct);
    out.put("owner", prefs().getString(KEY_OWNER, ""));
    out.put("count", (ok && acct) ? ContactWriter.count(getContext()) : 0);
    call.resolve(out);
  }

  /**
   * Turn it on: ask for the contacts permission, then create the account.
   *
   * The prompt fires HERE — at the moment the user flips the switch, with the explanation already on
   * screen beside it — rather than at app start, which is the difference between a granted prompt and
   * a reflexively dismissed one (the same reasoning as the music notification permission). A refusal
   * is not an error: it resolves `granted:false` and the client puts the switch back.
   */
  @PluginMethod
  public void enable(PluginCall call) {
    if (!granted()) {
      requestPermissionForAlias("contacts", call, "contactsPermission");
      return;
    }
    finishEnable(call);
  }

  @PermissionCallback
  private void contactsPermission(PluginCall call) {
    finishEnable(call);
  }

  private void finishEnable(PluginCall call) {
    JSObject out = new JSObject();
    if (!granted()) {
      out.put("granted", false);
      call.resolve(out);
      return;
    }
    boolean made = ContactWriter.ensureAccount(getContext());
    out.put("granted", true);
    out.put("account", made);
    call.resolve(out);
  }

  /**
   * Turn it off, and on sign-out / account switch.
   *
   * Removing the ACCOUNT is what makes this complete: the provider deletes every raw contact under
   * it, so there is no sweep to half-finish. The stored hashes go too — keeping them would tell the
   * next sweep that contacts it has not written are already on the phone.
   */
  @PluginMethod
  public void disable(PluginCall call) {
    ContactWriter.removeAccount(getContext());
    prefs().edit().remove(KEY_OWNER).remove(KEY_HASHES).apply();
    call.resolve();
  }

  /** Start a sweep: wipe if the account changed, and hand back what we believe is on the phone. */
  @PluginMethod
  public void begin(PluginCall call) {
    JSObject out = new JSObject();
    if (!granted()) {
      // Revoked in system settings since the switch was turned on. Say so rather than silently
      // writing nothing for ever — the client turns the switch back off.
      out.put("granted", false);
      call.resolve(out);
      return;
    }
    String owner = call.getString("owner", "");
    String had = prefs().getString(KEY_OWNER, "");
    if (!ContactWriter.ensureAccount(getContext())) {
      call.reject("could not create the PosterChan contacts account");
      return;
    }
    boolean wiped = false;
    if (!had.isEmpty() && !had.equals(owner)) {
      ContactWriter.wipe(getContext());
      prefs().edit().remove(KEY_HASHES).apply();
      wiped = true;
    }
    prefs().edit().putString(KEY_OWNER, owner == null ? "" : owner).apply();

    // The hashes are only a claim about the phone; the raw contacts are the fact. Anything we have a
    // hash for but no row for is dropped, so a card the user deleted by hand comes back on the next
    // sweep instead of being skipped for ever as "unchanged".
    Map<String, Long> have = ContactWriter.existing(getContext());
    JSObject hashes = new JSObject();
    JSONObject stored = readHashes();
    for (Iterator<String> it = stored.keys(); it.hasNext(); ) {
      String uid = it.next();
      if (have.containsKey(uid)) hashes.put(uid, stored.optString(uid, ""));
    }
    out.put("granted", true);
    out.put("hashes", hashes);
    out.put("count", have.size());
    out.put("wiped", wiped);
    call.resolve(out);
  }

  /** Upsert a batch of already-decrypted cards. */
  @PluginMethod
  public void put(PluginCall call) {
    if (!granted()) { call.reject("contacts permission is not granted"); return; }
    JSArray cards = call.getArray("cards");
    if (cards == null) { call.resolve(new JSObject().put("written", 0)); return; }
    JSONArray arr = new JSONArray();
    for (int i = 0; i < cards.length(); i++) {
      Object o = cards.opt(i);
      if (o instanceof JSONObject) arr.put(o);
    }
    Map<String, Long> have = ContactWriter.existing(getContext());
    Set<String> landed = ContactWriter.write(getContext(), arr, have);

    // Record a hash ONLY for a card whose batch was applied AND which is on the phone now. Recording
    // one for a card the provider refused would mark it "already up to date" for ever — the update
    // would never be retried and nothing would say so.
    Map<String, Long> after = ContactWriter.existing(getContext());
    JSONObject stored = readHashes();
    for (int i = 0; i < arr.length(); i++) {
      JSONObject c = arr.optJSONObject(i);
      if (c == null) continue;
      String uid = c.optString("uid", "");
      if (uid.isEmpty() || !landed.contains(uid) || !after.containsKey(uid)) continue;
      try { stored.put(uid, c.optString("h", "")); } catch (Throwable ignored) {}
    }
    writeHashes(stored);
    call.resolve(new JSObject().put("written", landed.size()));
  }

  /**
   * THE RECONCILE. Everything of ours that is not in `uids` is deleted from the phone.
   *
   * This is the half that is easy to leave out, and leaving it out is the exact bug docs/CONTACTS.md
   * already records against the CardDAV path: a contact deleted in the web UI stays on the phone and
   * can be edited back into existence.
   */
  @PluginMethod
  public void commit(PluginCall call) {
    if (!granted()) { call.reject("contacts permission is not granted"); return; }
    JSArray list = call.getArray("uids");
    if (list == null) { call.reject("commit needs the full uid list"); return; }
    Set<String> keep = new HashSet<>();
    for (int i = 0; i < list.length(); i++) {
      Object o = list.opt(i);
      if (o != null) keep.add(String.valueOf(o));
    }
    Map<String, Long> have = ContactWriter.existing(getContext());
    int removed = ContactWriter.prune(getContext(), keep, have);

    JSONObject stored = readHashes();
    JSONObject kept = new JSONObject();
    for (Iterator<String> it = stored.keys(); it.hasNext(); ) {
      String uid = it.next();
      if (keep.contains(uid)) {
        try { kept.put(uid, stored.optString(uid, "")); } catch (Throwable ignored) {}
      }
    }
    writeHashes(kept);
    call.resolve(new JSObject().put("removed", removed).put("count", have.size() - removed));
  }

  private JSONObject readHashes() {
    try {
      return new JSONObject(prefs().getString(KEY_HASHES, "{}"));
    } catch (Throwable t) {
      return new JSONObject();
    }
  }

  private void writeHashes(JSONObject o) {
    prefs().edit().putString(KEY_HASHES, o == null ? "{}" : o.toString()).apply();
  }
}
