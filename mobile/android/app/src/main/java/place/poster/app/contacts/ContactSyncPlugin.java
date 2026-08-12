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
 * TWO WAY, and the order is the whole design: PULL, then merge, then PUSH. A card edited in the
 * phone's Contacts app is only visible as a change until somebody overwrites it, and a push is
 * exactly that overwrite — push first and the edit is gone before it was ever read, with nothing
 * anywhere to say so. So the client reads what the phone changed, merges it into the encrypted card
 * (which it alone can read), stores it, and only then pushes the result back.
 *
 * FIVE CALLS PER SWEEP, and the shape is what keeps it cheap:
 *   pull(owner)  → what the phone changed since our last write: edits, creations, deletions.
 *   taken(rows)  → the rows the app CONFIRMS it stored. Only these are marked clean.
 *   begin(owner) → the hash of every card we last wrote. JS diffs against that and sends only what
 *                  changed. Without this the client would have to push every base64 PHOTO across the
 *                  bridge on every visit to the screen — the same "4 bytes of string per 3 of data,
 *                  held as UTF-16" cost that sizes folder sync's chunks, except paid for nothing.
 *   put(cards)   → upsert a batch, keyed on the card's UID.
 *   commit(uids) → the reconcile: anything of ours NOT in that list is deleted from the phone.
 *
 * Both halves of the push (put and commit) hold back UIDs with an unacknowledged phone-side change —
 * see ContactReader.pending(). That single guard is what stops the two directions fighting: a person
 * deleted on the phone is not re-inserted, and a person created on the phone is not pruned before
 * the app has managed to store them.
 *
 * `owner` is the signed-in pubkey. It is compared on every begin() AND every pull() — a mismatch
 * wipes first. On pull it matters more than anywhere else: without it, the first sweep after somebody
 * else signs in on this phone would upload the PREVIOUS user's phone book into the new account.
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

  /**
   * The account this device's phone book belongs to, checked and taken over if it has changed.
   *
   * Returns true if it wiped. Called by BOTH begin() and pull(), and pull() is the one that would
   * otherwise be a leak rather than a mess: reading a phone book left behind by the previous account
   * and storing it under the new one publishes somebody else's contacts into somebody else's
   * (encrypted, but not theirs) address book.
   */
  private boolean ownerGuard(String owner) {
    // AN ABSENT OWNER IS "I DON'T KNOW", NOT "SOMEBODY ELSE". The client only sweeps while it is
    // signed in, but `owner()` there reads the session at the moment of the call — a sign-out or an
    // account switch landing in the gap between the switch's check and this bridge call sends an
    // empty string, and read as a mismatch that WIPES the whole phone book and then records "" as
    // the owner, so the next sweep writes it all back. Written, gone, written, gone, with nothing in
    // any log. An empty owner now changes nothing at all, in either direction.
    if (owner == null || owner.isEmpty()) return false;
    String had = prefs().getString(KEY_OWNER, "");
    boolean wiped = false;
    if (!had.isEmpty() && !had.equals(owner)) {
      ContactWriter.wipe(getContext());
      prefs().edit().remove(KEY_HASHES).apply();
      wiped = true;
    }
    prefs().edit().putString(KEY_OWNER, owner == null ? "" : owner).apply();
    return wiped;
  }

  /**
   * WHAT THE PHONE CHANGED — the half that makes this two-way.
   *
   * Runs FIRST in a sweep. Everything it reports is still on the phone and stays there until the
   * client says, through taken(), that it stored it; nothing here forgets anything.
   */
  @PluginMethod
  public void pull(PluginCall call) {
    JSObject out = new JSObject();
    if (!granted()) { out.put("granted", false); call.resolve(out); return; }
    if (!ContactWriter.ensureAccount(getContext())) {
      call.reject("could not create the PosterChan contacts account");
      return;
    }
    boolean wiped = ownerGuard(call.getString("owner", ""));
    // A wipe just removed every row: there is nothing of this account's to read, and anything that
    // WAS dirty belonged to the account we just left.
    JSONArray rows = new JSONArray();
    if (!wiped) {
      ContactReader.mintSourceIds(getContext());   // before the versions are read — it bumps them
      rows = ContactReader.changes(getContext());
    }
    out.put("granted", true);
    out.put("wiped", wiped);
    out.put("rows", rows);
    // The hash of what we last PUSHED for each of those cards, so the client can tell "the phone
    // changed" from "both sides changed" without a second bridge call.
    JSObject known = new JSObject();
    JSONObject stored = readHashes();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject r = rows.optJSONObject(i);
      String uid = r == null ? "" : r.optString("uid", "");
      if (!uid.isEmpty() && stored.has(uid)) known.put(uid, stored.optString(uid, ""));
    }
    out.put("pushed", known);
    call.resolve(out);
  }

  /**
   * Mark clean ONLY the rows the client says it stored.
   *
   * The client sends back the rows from pull() it actually persisted, each with the VERSION it was
   * read at; ContactReader.taken() clears DIRTY only where that version still matches. Anything else
   * — a save that failed, a contact the user edited again while the sweep was in flight — stays
   * dirty and comes round again. Same rule as write() returning the UIDs that landed: an
   * acknowledgement is a statement about what is stored, never about what was attempted.
   */
  @PluginMethod
  public void taken(PluginCall call) {
    if (!granted()) { call.reject("contacts permission is not granted"); return; }
    JSArray list = call.getArray("rows");
    JSONArray rows = new JSONArray();
    for (int i = 0; list != null && i < list.length(); i++) {
      Object o = list.opt(i);
      if (o instanceof JSONObject) rows.put(o);
    }
    JSONArray cleared = ContactReader.taken(getContext(), rows);

    // A card whose phone version the app has now stored is IN STEP with the phone, so record the
    // hash the client computed for it — otherwise the push that follows would rewrite those rows for
    // nothing, on every sweep after every edit. The client only sends `h` when the two sides really
    // do agree; when the app's copy won a conflict it omits it, and the push puts its version back.
    JSONObject stored = readHashes();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject r = rows.optJSONObject(i);
      if (r == null) continue;
      String uid = r.optString("uid", "");
      if (uid.isEmpty()) continue;
      try {
        if (r.optBoolean("deleted", false)) stored.remove(uid);
        else if (r.has("h")) stored.put(uid, r.optString("h", ""));
      } catch (Throwable ignored) {}
    }
    writeHashes(stored);
    call.resolve(new JSObject().put("cleared", cleared.length()));
  }

  /** Start the push half of a sweep, and hand back what we believe is on the phone. */
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
    if (!ContactWriter.ensureAccount(getContext())) {
      call.reject("could not create the PosterChan contacts account");
      return;
    }
    boolean wiped = ownerGuard(call.getString("owner", ""));

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
    // Leave alone anything the phone has changed and the app has not stored yet — see
    // ContactReader.pending(). This is read fresh, not passed in, because the user can be editing a
    // contact while the sweep runs.
    Set<String> hold = ContactReader.pending(getContext());
    Set<String> landed = ContactWriter.write(getContext(), arr, have, hold);

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
   *
   * AND IT REFUSES A COLLAPSE, because a plugin must not trust its caller. This call emptied a real
   * phone book, twice: the client's own guards covered an EMPTY `uids` and nothing else, so any way
   * of producing a SHORT one — a per-book fetch that failed and was swallowed into `[]`, a relay read
   * that answered partially with a 200 on top of it — arrived here as an ordinary reconcile and was
   * obeyed. Every guard on the JS side is advisory: the JS is the thing that got it wrong.
   *
   * The rule is one sentence and it is the same one the client applies: A RECONCILE THAT WOULD DELETE
   * MORE THAN IT KEEPS IS NOT A RECONCILE, IT IS A COLLAPSE. It is refused, out loud — `refused:true`
   * with the numbers, so the client can say something rather than watch a sweep "succeed" — and the
   * stored hashes are left exactly as they were, so the next sweep is not told these cards are
   * already on a phone they were never written to.
   *
   * The escape hatch is `force:true`, for a caller that has PROVED the shrink is real (a genuine mass
   * delete, a user who asked for it). Nothing in the client passes it today; the deliberate way to
   * rebuild this phone's copy is to turn the switch off — which removes the account and every row
   * with it — and on again.
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
    // A contact CREATED on the phone is a card the app has never heard of, which is precisely what
    // this deletes. Held back until the app acknowledges it.
    Set<String> hold = ContactReader.pending(getContext());

    // ONE set, computed once: the guard must be asked about exactly the rows prune() will delete, or
    // it is guarding a different reconcile from the one that runs.
    Set<String> doomed = ContactWriter.doomed(have, keep, hold);
    boolean force = Boolean.TRUE.equals(call.getBoolean("force", false));
    if (isCollapse(doomed.size(), have.size() - doomed.size(), force)) {
      call.resolve(new JSObject().put("refused", true).put("removed", 0)
                                 .put("would", doomed.size())
                                 .put("kept", have.size() - doomed.size())
                                 .put("count", have.size()));
      return;
    }
    int removed = ContactWriter.prune(getContext(), doomed, have);

    JSONObject stored = readHashes();
    JSONObject kept = new JSONObject();
    for (Iterator<String> it = stored.keys(); it.hasNext(); ) {
      String uid = it.next();
      if (keep.contains(uid) || hold.contains(uid)) {
        try { kept.put(uid, stored.optString(uid, "")); } catch (Throwable ignored) {}
      }
    }
    writeHashes(kept);
    call.resolve(new JSObject().put("removed", removed).put("count", have.size() - removed));
  }

  /**
   * The collapse rule, on its own so it can be RUN in a test rather than read.
   *
   * `remove > keep` is deliberately crude, and crude is the point: it needs no idea of why the list
   * is short, which is what makes it hold against the next silent way of producing one. A reconcile
   * that deletes nothing is always allowed (a brand-new account keeps zero rows and removes zero —
   * refusing there would stop the hash bookkeeping ever starting), and an empty keep-set is refused
   * by the same arithmetic the moment there is anything at all to delete.
   */
  static boolean isCollapse(int remove, int keep, boolean force) {
    if (force) return false;
    if (remove <= 0) return false;
    return remove > keep;
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
