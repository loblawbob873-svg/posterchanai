package place.poster.app.contacts;

import android.accounts.Account;
import android.accounts.AccountManager;
import android.content.ContentProviderOperation;
import android.content.ContentProviderResult;
import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.provider.ContactsContract;
import android.provider.ContactsContract.CommonDataKinds.Email;
import android.provider.ContactsContract.CommonDataKinds.Event;
import android.provider.ContactsContract.CommonDataKinds.Note;
import android.provider.ContactsContract.CommonDataKinds.Organization;
import android.provider.ContactsContract.CommonDataKinds.Phone;
import android.provider.ContactsContract.CommonDataKinds.Photo;
import android.provider.ContactsContract.CommonDataKinds.StructuredName;
import android.provider.ContactsContract.CommonDataKinds.StructuredPostal;
import android.provider.ContactsContract.Data;
import android.provider.ContactsContract.RawContacts;
import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The half that touches ContactsContract. No Capacitor, no JS, no decryption — it is handed cards
 * that are already plain and writes them into the phone's own contacts database.
 *
 * WHY THERE IS AN ACCOUNT AT ALL. A RawContact must belong to an account, and the account is what
 * makes this feature reversible: the phone's Contacts app groups our cards under "PosterChan", the
 * user can hide the group ("Contacts to display"), and REMOVING the account makes ContactsProvider2
 * delete every row we ever wrote — which is how sign-out gets to be one call instead of a sweep that
 * can half-fail. The authenticator behind that account type is a stub (see PosterChanAuthenticator):
 * we are not using AccountManager for authentication, only for ownership.
 *
 * WHY NOT A REAL SyncAdapter. The system runs a sync adapter on ITS schedule, in a process with no
 * WebView — and every card here is an encrypted Nostr event whose plaintext only the client ever
 * holds. A sync adapter would therefore be a "Sync now" button that can never do anything, which
 * reads as broken rather than as absent. So the account is created with
 * `setIsSyncable(…, ContactsContract.AUTHORITY, 0)`: no sync toggle, no periodic job, nothing the OS
 * can start. The app pushes when it is open, which is the only moment the data can be read.
 *
 * CALLER_IS_SYNCADAPTER IS NOT OPTIONAL. It is a URI parameter any app may set, and without it the
 * provider treats our writes as USER edits: it stamps DIRTY on every row, and — the one that breaks
 * the reconcile — a delete only sets DELETED=1 and leaves a tombstone behind, so a contact removed in
 * the web UI would stay visible on the phone. With it, a delete is a delete. That is exactly the trap
 * docs/CONTACTS.md already records for the CardDAV path ("a contact deleted in the web UI stayed on
 * the phone and could be edited back into existence"); this is the same bug wearing a different hat.
 */
public final class ContactWriter {

  private static final String TAG = "PosterChan";

  /** Must match res/xml/contacts_authenticator.xml — the account type IS the join between them. */
  public static final String ACCOUNT_TYPE = "place.poster.app.contacts";
  /** What the Contacts app shows as the account. Deliberately NOT the user's npub: the account list
   *  is visible to anyone holding the phone, and the identity is recorded in our own prefs instead. */
  public static final String ACCOUNT_NAME = "PosterChan";
  /** The clause that scopes a statement to our account, and its arguments. ONE definition, used by
   *  every read and every delete on both sides — this reads and writes a database full of other
   *  people's contacts, so "which rows are ours" is not a thing to spell out per query. */
  static final String OURS = RawContacts.ACCOUNT_TYPE + "=? AND " + RawContacts.ACCOUNT_NAME + "=?";

  static String[] oursArgs() {
    return new String[]{ACCOUNT_TYPE, ACCOUNT_NAME};
  }

  /**
   * The data rows this feature OWNS, and the exact set the account's edit schema
   * (res/xml/contacts_structure.xml) offers. A push rewrites these and leaves everything else on the
   * raw contact alone: another app (or an OEM Contacts app that ignores the schema) may have put a
   * nickname or a website on one of our cards, and deleting rows we have no field for would quietly
   * destroy them on the next push of an unrelated phone number.
   */
  static final String[] MANAGED_MIMES = {
      StructuredName.CONTENT_ITEM_TYPE, Phone.CONTENT_ITEM_TYPE, Email.CONTENT_ITEM_TYPE,
      Organization.CONTENT_ITEM_TYPE, StructuredPostal.CONTENT_ITEM_TYPE, Note.CONTENT_ITEM_TYPE,
      Event.CONTENT_ITEM_TYPE, Photo.CONTENT_ITEM_TYPE };

  /** ContentProviderOperations per applyBatch. Flushed only at CARD boundaries — see write(). */
  private static final int BATCH_OPS = 300;
  /** Contact photos are stored as a thumbnail anyway; anything bigger is bytes across the bridge and
   *  through the provider for a picture nothing will ever display at that size. */
  private static final int PHOTO_MAX_PX = 512;

  private ContactWriter() {}

  public static Account account() {
    return new Account(ACCOUNT_NAME, ACCOUNT_TYPE);
  }

  public static boolean hasAccount(Context ctx) {
    try {
      // No GET_ACCOUNTS permission needed: since API 23 an app may always see accounts whose
      // authenticator it owns, and we own this one.
      for (Account a : AccountManager.get(ctx).getAccountsByType(ACCOUNT_TYPE)) {
        if (ACCOUNT_NAME.equals(a.name)) return true;
      }
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not list accounts", t);
    }
    return false;
  }

  /**
   * Make sure the account exists, and answer the ONE question everything above depends on: is it
   * there NOW.
   *
   * THE ANSWER IS ALWAYS hasAccount(), NEVER THE RETURN VALUE OF SOMETHING WE ATTEMPTED. That
   * distinction is not pedantry — it is this method's entire history, and it cost the feature every
   * contact it was ever asked to write:
   *
   *  · `addAccountExplicitly` returns FALSE for an account that ALREADY EXISTS. That is "nothing to
   *    do", not "it failed", and read as failure it reports "no account" on every run after the
   *    first — while getAccountsByType, one line away, correctly says it is there.
   *  · `setIsSyncable`/`setSyncAutomatically` require WRITE_SYNC_SETTINGS, which this app did not
   *    declare. They therefore threw SecurityException on EVERY call, the catch below turned that
   *    into `false`, and begin() answered `account:false` to a phone whose account plainly existed —
   *    printed on screen directly above the probe's `account=yes`, which is what found this. Not one
   *    card was ever written on that device. The permission is declared now, but the shape of the
   *    code is the real fix: TUNING THE SYNC FRAMEWORK IS NOT WHAT MAKES AN ACCOUNT EXIST, so it
   *    cannot be allowed to say the account does not.
   *
   * So: try to create it, try to take it off the sync framework, then MEASURE. Nothing an attempt
   * returns or throws is the verdict — the account list is.
   */
  public static boolean ensureAccount(Context ctx) {
    Account a = account();
    if (!hasAccount(ctx)) {
      try {
        // The return value is deliberately ignored: `false` here means "already there", which is
        // indistinguishable from success for our purposes and is settled by hasAccount() below.
        AccountManager.get(ctx).addAccountExplicitly(a, null, null);
      } catch (Throwable t) {
        Log.w(TAG, "contacts: could not create the account", t);
      }
    }
    // Off the sync framework entirely — see the class comment. BEST EFFORT, in its own guard: an OEM
    // that refuses this leaves a sync toggle we did not want, which is a cosmetic wart. Letting it
    // decide whether the account exists is a phone book that never receives anybody.
    try {
      ContentResolver.setIsSyncable(a, ContactsContract.AUTHORITY, 0);
      ContentResolver.setSyncAutomatically(a, ContactsContract.AUTHORITY, false);
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not take the account off the sync framework", t);
    }
    return hasAccount(ctx);
  }

  /**
   * Take the account away, and with it every contact we wrote.
   *
   * Belt AND braces: removeAccountExplicitly is what deletes the rows, but it can return false (or
   * be absent on an OEM that has done something strange), and a lingering copy of somebody else's
   * address book on a handed-down phone is the worst outcome this feature has. So the rows are swept
   * directly as well, and that sweep is safe on its own — it is scoped to our account type.
   */
  public static void removeAccount(Context ctx) {
    try {
      wipe(ctx);
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not wipe rows", t);
    }
    try {
      AccountManager.get(ctx).removeAccountExplicitly(account());
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not remove the account", t);
    }
  }

  /**
   * Every LIVE raw contact under our account, keyed by the card UID we stamped into SOURCE_ID.
   *
   * DELETED=0 is load-bearing now that the phone can delete one of ours. A contact removed in the
   * phone's Contacts app leaves a tombstone (DELETED=1) until the sync acknowledges it, and a
   * tombstone is not a row to update: treated as live, the next push would write the card's data
   * rows onto a deleted contact and the reconcile would count somebody as present who is not.
   */
  public static Map<String, Long> existing(Context ctx) {
    Map<String, Long> out = new HashMap<>();
    List<Long> dupes = new ArrayList<>();
    Cursor c = null;
    try {
      c = ctx.getContentResolver().query(
          RawContacts.CONTENT_URI,
          new String[]{RawContacts._ID, RawContacts.SOURCE_ID},
          OURS + " AND " + RawContacts.DELETED + "=0", oursArgs(), null);
      while (c != null && c.moveToNext()) {
        String uid = c.getString(1);
        if (uid == null || uid.isEmpty()) continue;
        long id = c.getLong(0);
        Long prev = out.put(uid, id);
        // Two raw contacts claiming the same card — a leftover from a sweep that was interrupted
        // between the insert and the batch that would have owned it. Keep the newer one and drop the
        // other, or the person shows up twice for ever.
        if (prev != null) {
          out.put(uid, Math.max(prev, id));
          dupes.add(Math.min(prev, id));
        }
      }
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not read what we already wrote", t);
    } finally {
      if (c != null) try { c.close(); } catch (Throwable ignored) {}
    }
    for (Long id : dupes) deleteRaw(ctx, id);   // after the cursor, never during it
    return out;
  }

  public static int count(Context ctx) {
    return existing(ctx).size();
  }

  /**
   * Upsert a batch of cards. Returns the UIDs that actually LANDED.
   *
   * Update-in-place by UID, never delete-and-recreate: the aggregate contact id survives, and with it
   * the favourite star, the shortcut on somebody's home screen and any ringtone they set.
   *
   * The return value is not decoration. A batch the provider refuses is applied as a transaction, so
   * the rows it carried keep their OLD contents — and the caller must NOT then record the new hash
   * for them, or that person is "already up to date" for ever and their new number never reaches the
   * phone. Reporting a count instead of the names is exactly how that becomes invisible.
   *
   * `skip` is the two-way half: UIDs whose phone row has a change the app has not stored yet. The
   * app's copy is NOT authoritative for those — writing it would undo an edit made here seconds ago,
   * or resurrect somebody the phone has just deleted. See ContactReader.pending().
   */
  public static Set<String> write(Context ctx, JSONArray cards, Map<String, Long> existing,
                                  Set<String> skip) {
    return write(ctx, cards, existing, skip, new Report());
  }

  /**
   * WHAT A WRITE ACTUALLY DID, as opposed to what it attempted.
   *
   * This feature was debugged blind across four APK builds because the failure REPORTS SUCCESS: a
   * batch the provider quietly no-ops does not throw, `write()` hands back the uids it sent, and
   * every layer above says it worked while the phone's Contacts app stays empty. `applyBatch`
   * returns a ContentProviderResult per operation — an insert that landed carries a `uri` — so the
   * one question that matters ("did rows appear?") is answerable here and nowhere else.
   */
  public static final class Report {
    public int cards, built, held, ops, batches, failedBatches, applied, noop;
    public String error = "";

    /** One line, safe to put on a screen and read back over a phone call. */
    public String line() {
      return "ops=" + ops + " applied=" + applied + " noop=" + noop
           + " batches=" + batches + (failedBatches > 0 ? "/" + failedBatches + " failed" : "")
           + (error.isEmpty() ? "" : " err=" + error);
    }
  }

  public static Set<String> write(Context ctx, JSONArray cards, Map<String, Long> existing,
                                  Set<String> skip, Report rep) {
    Set<String> ok = new HashSet<>();
    if (rep == null) rep = new Report();
    if (cards == null || cards.length() == 0) return ok;
    rep.cards = cards.length();
    ArrayList<ContentProviderOperation> ops = new ArrayList<>();
    List<String> pending = new ArrayList<>();
    for (int i = 0; i < cards.length(); i++) {
      JSONObject card = cards.optJSONObject(i);
      if (card == null) continue;
      String uid = card.optString("uid", "");
      if (uid.isEmpty()) continue;
      if (skip != null && skip.contains(uid)) { rep.held++; continue; }
      Long rawId = existing.get(uid);
      try {
        buildCard(ops, uid, rawId, card);
        pending.add(uid);
        rep.built++;
      } catch (Throwable t) {
        Log.w(TAG, "contacts: skipping a card we could not build", t);
      }
      // FLUSH ONLY BETWEEN CARDS. withValueBackReference indexes into the batch being applied, so a
      // chunk that splits a card would point its data rows at whatever happened to sit at that index
      // in the next batch — silently attaching one person's phone number to another.
      if (ops.size() >= BATCH_OPS) {
        if (apply(ctx, ops, rep)) ok.addAll(pending);
        ops = new ArrayList<>(); pending = new ArrayList<>();
      }
    }
    if (apply(ctx, ops, rep)) ok.addAll(pending);
    return ok;
  }

  private static void buildCard(List<ContentProviderOperation> ops, String uid, Long rawId,
                                JSONObject card) {
    int backRef = -1;
    if (rawId == null) {
      backRef = ops.size();
      ops.add(ContentProviderOperation.newInsert(syncUri(RawContacts.CONTENT_URI))
          .withValue(RawContacts.ACCOUNT_NAME, ACCOUNT_NAME)
          .withValue(RawContacts.ACCOUNT_TYPE, ACCOUNT_TYPE)
          .withValue(RawContacts.SOURCE_ID, uid)
          .build());
    } else {
      // Rewrite the rows this feature OWNS. Diffing them property by property would be a second
      // merge algorithm on top of the one the client already ran — the client has merged the phone's
      // side in by now, so its copy is the answer. Anything outside MANAGED_MIMES belongs to the
      // phone and is left where it is.
      ops.add(ContentProviderOperation.newDelete(syncUri(Data.CONTENT_URI))
          .withSelection(managedSelection(), managedArgs(rawId))
          .build());
    }

    String given = card.optString("given", "");
    String family = card.optString("family", "");
    String fn = card.optString("fn", "");
    ContentProviderOperation.Builder name = row(rawId, backRef, StructuredName.CONTENT_ITEM_TYPE)
        .withValue(StructuredName.DISPLAY_NAME, fn.isEmpty() ? (given + " " + family).trim() : fn);
    if (!given.isEmpty()) name.withValue(StructuredName.GIVEN_NAME, given);
    if (!family.isEmpty()) name.withValue(StructuredName.FAMILY_NAME, family);
    // The parts the editor has no field for but a vCard N: does. They are WRITTEN because they are
    // READ back (ContactReader) — a name part offered on the phone and dropped here would be lost
    // the first time somebody corrected a phone number.
    String middle = card.optString("middle", ""), prefix = card.optString("prefix", ""),
           suffix = card.optString("suffix", "");
    if (!middle.isEmpty()) name.withValue(StructuredName.MIDDLE_NAME, middle);
    if (!prefix.isEmpty()) name.withValue(StructuredName.PREFIX, prefix);
    if (!suffix.isEmpty()) name.withValue(StructuredName.SUFFIX, suffix);
    ops.add(name.build());

    JSONArray tels = card.optJSONArray("tels");
    for (int i = 0; tels != null && i < tels.length(); i++) {
      JSONObject t = tels.optJSONObject(i);
      String v = t == null ? "" : t.optString("value", "");
      if (v.isEmpty()) continue;
      String label = t.optString("type", "");
      int type = phoneType(label);
      ContentProviderOperation.Builder b = row(rawId, backRef, Phone.CONTENT_ITEM_TYPE)
          .withValue(Phone.NUMBER, v).withValue(Phone.TYPE, type);
      if (type == Phone.TYPE_CUSTOM) b.withValue(Phone.LABEL, label);
      ops.add(b.build());
    }

    JSONArray emails = card.optJSONArray("emails");
    for (int i = 0; emails != null && i < emails.length(); i++) {
      JSONObject e = emails.optJSONObject(i);
      String v = e == null ? "" : e.optString("value", "");
      if (v.isEmpty()) continue;
      String label = e.optString("type", "");
      int type = emailType(label);
      ContentProviderOperation.Builder b = row(rawId, backRef, Email.CONTENT_ITEM_TYPE)
          .withValue(Email.ADDRESS, v).withValue(Email.TYPE, type);
      if (type == Email.TYPE_CUSTOM) b.withValue(Email.LABEL, label);
      ops.add(b.build());
    }

    String org = card.optString("org", "");
    String title = card.optString("title", "");
    if (!org.isEmpty() || !title.isEmpty()) {
      ops.add(row(rawId, backRef, Organization.CONTENT_ITEM_TYPE)
          .withValue(Organization.COMPANY, org)
          .withValue(Organization.TITLE, title)
          .withValue(Organization.TYPE, Organization.TYPE_WORK)
          .build());
    }

    // EVERY address, not just the first. The phone can now edit these, and a card whose second
    // address was never sent would lose it the moment the merge wrote the phone's list back.
    // `adr` (singular) is still read for an APK older than two-way sync talking to this client.
    JSONArray adrs = card.optJSONArray("adrs");
    if (adrs == null || adrs.length() == 0) {
      adrs = new JSONArray();
      JSONObject one = card.optJSONObject("adr");
      if (one != null) adrs.put(one);
    }
    for (int i = 0; i < adrs.length(); i++) {
      JSONObject adr = adrs.optJSONObject(i);
      if (adr == null) continue;
      String street = adr.optString("street", ""), city = adr.optString("city", ""),
             region = adr.optString("region", ""), code = adr.optString("code", ""),
             country = adr.optString("country", "");
      if ((street + city + region + code + country).trim().isEmpty()) continue;
      ops.add(row(rawId, backRef, StructuredPostal.CONTENT_ITEM_TYPE)
          .withValue(StructuredPostal.STREET, street)
          .withValue(StructuredPostal.CITY, city)
          .withValue(StructuredPostal.REGION, region)
          .withValue(StructuredPostal.POSTCODE, code)
          .withValue(StructuredPostal.COUNTRY, country)
          .withValue(StructuredPostal.TYPE, StructuredPostal.TYPE_HOME)
          .build());
    }

    String note = card.optString("note", "");
    if (!note.isEmpty()) {
      ops.add(row(rawId, backRef, Note.CONTENT_ITEM_TYPE).withValue(Note.NOTE, note).build());
    }

    String bday = card.optString("bday", "");
    if (!bday.isEmpty()) {
      ops.add(row(rawId, backRef, Event.CONTENT_ITEM_TYPE)
          .withValue(Event.START_DATE, bday)
          .withValue(Event.TYPE, Event.TYPE_BIRTHDAY)
          .build());
    }

    byte[] photo = photoBytes(card.optString("photo", ""));
    if (photo != null) {
      ops.add(row(rawId, backRef, Photo.CONTENT_ITEM_TYPE).withValue(Photo.PHOTO, photo).build());
    }
  }

  /** A Data row that attaches either to a raw contact we already have, or to one being inserted. */
  private static ContentProviderOperation.Builder row(Long rawId, int backRef, String mime) {
    ContentProviderOperation.Builder b =
        ContentProviderOperation.newInsert(syncUri(Data.CONTENT_URI));
    if (rawId != null) b.withValue(Data.RAW_CONTACT_ID, rawId);
    else b.withValueBackReference(Data.RAW_CONTACT_ID, backRef);
    return b.withValue(Data.MIMETYPE, mime);
  }

  /**
   * WHICH of our raw contacts a reconcile would delete: everything we hold that is not in `keep`.
   *
   * Pure, and separate from prune() for one reason: ContactSyncPlugin.commit() has to REFUSE a
   * reconcile that would delete more than it keeps, and a guard that counts a different set from the
   * one the delete walks is not a guard. One list, asked once, used by both.
   *
   * `hold` is what stops the reconcile eating the OTHER direction. A contact created in the phone's
   * Contacts app is, for the moment between being created and being stored on the server, a card the
   * app has never heard of — i.e. exactly what this would delete. Held back until the app has
   * acknowledged it, a sweep that fails costs a retry instead of the contact.
   */
  public static Set<String> doomed(Map<String, Long> existing, Set<String> keep, Set<String> hold) {
    Set<String> out = new HashSet<>();
    if (existing == null) return out;
    for (Map.Entry<String, Long> e : existing.entrySet()) {
      if (keep != null && keep.contains(e.getKey())) continue;
      if (hold != null && hold.contains(e.getKey())) continue;
      out.add(e.getKey());
    }
    return out;
  }

  /**
   * Delete exactly the raw contacts named by doomed(). THE HALF THAT IS EASY TO FORGET: without it
   * the phone book only ever grows, and a contact deleted in the web UI lives on in the dialer for
   * ever.
   *
   * It takes the SET rather than deciding again from `keep`: the decision is made once, above the
   * guard that can refuse it, and this only carries it out.
   */
  public static int prune(Context ctx, Set<String> doomed, Map<String, Long> existing) {
    int gone = 0;
    if (doomed == null || existing == null) return 0;
    for (String uid : doomed) {
      Long id = existing.get(uid);
      if (id == null) continue;
      if (deleteRaw(ctx, id)) gone++;
    }
    return gone;
  }

  /** Everything we ever wrote, with the account left in place. */
  public static int wipe(Context ctx) {
    try {
      return ctx.getContentResolver().delete(syncUri(RawContacts.CONTENT_URI), OURS, oursArgs());
    } catch (Throwable t) {
      Log.w(TAG, "contacts: wipe failed", t);
      return 0;
    }
  }

  /**
   * SAY THE ACCOUNT OUT LOUD. syncUri() carries ACCOUNT_NAME/ACCOUNT_TYPE as query parameters and
   * ContactsProvider2 does fold them into the selection (appendAccountIdToSelection) — so an id from
   * somewhere else would not match today. That is undocumented behaviour propping up the one call in
   * this app that can delete a row out of somebody's phone book, and the row id it is handed comes
   * from a map built elsewhere. One extra clause costs nothing and means the delete is scoped by the
   * statement itself, whatever the provider does with the URI.
   */
  private static boolean deleteRaw(Context ctx, long rawId) {
    try {
      return ctx.getContentResolver().delete(syncUri(RawContacts.CONTENT_URI),
          RawContacts._ID + "=? AND " + OURS,
          new String[]{String.valueOf(rawId), ACCOUNT_TYPE, ACCOUNT_NAME}) > 0;
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not delete a raw contact", t);
      return false;
    }
  }

  /**
   * DID THIS OPERATION CHANGE ANYTHING? On its own so it can be RUN in a test rather than read.
   *
   * An insert that landed carries the new row's `uri`; an update or delete carries a `count`. A
   * provider that accepted the operation and did nothing carries neither — and applyBatch does NOT
   * throw for it, which is the entire reason a sweep can hand over ninety cards, report success, and
   * leave the phone's Contacts app empty. Getting this backwards would make the diagnostic lie in
   * the one direction that matters.
   */
  static boolean landed(ContentProviderResult r) {
    if (r == null) return false;
    if (r.uri != null) return true;
    return r.count != null && r.count.intValue() > 0;
  }

  private static boolean apply(Context ctx, ArrayList<ContentProviderOperation> ops, Report rep) {
    if (ops.isEmpty()) return true;
    rep.batches++;
    rep.ops += ops.size();
    try {
      ContentProviderResult[] res =
          ctx.getContentResolver().applyBatch(ContactsContract.AUTHORITY, ops);
      // COUNT WHAT CAME BACK. An operation that changed nothing returns a result with no uri and a
      // count of zero, and applyBatch does NOT throw for it — which is the whole reason a sweep can
      // report success and leave a phone book empty.
      for (int i = 0; res != null && i < res.length; i++) {
        if (landed(res[i])) rep.applied++; else rep.noop++;
      }
      return true;
    } catch (Throwable t) {
      // One malformed card must not abandon the rest of the address book — the same rule the client
      // follows when a vCard will not parse. It IS reported, though: see write() and Report.
      Log.w(TAG, "contacts: a batch of " + ops.size() + " operations failed", t);
      rep.failedBatches++;
      if (rep.error.isEmpty()) rep.error = String.valueOf(t);
      return false;
    }
  }

  /** A selection over the rows we own — see MANAGED_MIMES. */
  private static String managedSelection() {
    StringBuilder sb = new StringBuilder(Data.RAW_CONTACT_ID + "=? AND " + Data.MIMETYPE + " IN (");
    for (int i = 0; i < MANAGED_MIMES.length; i++) sb.append(i == 0 ? "?" : ",?");
    return sb.append(")").toString();
  }

  private static String[] managedArgs(long rawId) {
    String[] args = new String[MANAGED_MIMES.length + 1];
    args[0] = String.valueOf(rawId);
    System.arraycopy(MANAGED_MIMES, 0, args, 1, MANAGED_MIMES.length);
    return args;
  }

  /** See the class comment: without this a delete is a tombstone, not a delete — and, now that the
   *  phone can edit our cards, without it every push would also stamp DIRTY on the whole address
   *  book and the next pull would upload our own writes back as if they were the user's. */
  static Uri syncUri(Uri uri) {
    return uri.buildUpon()
        .appendQueryParameter(ContactsContract.CALLER_IS_SYNCADAPTER, "true")
        .appendQueryParameter(RawContacts.ACCOUNT_NAME, ACCOUNT_NAME)
        .appendQueryParameter(RawContacts.ACCOUNT_TYPE, ACCOUNT_TYPE)
        .build();
  }

  /** Base64 (no data: prefix — the client strips it) → JPEG bytes at a sane size, or null. */
  private static byte[] photoBytes(String b64) {
    if (b64 == null || b64.isEmpty()) return null;
    try {
      byte[] raw = Base64.decode(b64, Base64.DEFAULT);
      if (raw == null || raw.length == 0) return null;
      BitmapFactory.Options probe = new BitmapFactory.Options();
      probe.inJustDecodeBounds = true;
      BitmapFactory.decodeByteArray(raw, 0, raw.length, probe);
      int big = Math.max(probe.outWidth, probe.outHeight);
      if (big <= 0) return null;                       // not an image we can read — better none
      if (big <= PHOTO_MAX_PX && raw.length <= 200 * 1024) return raw;
      BitmapFactory.Options opts = new BitmapFactory.Options();
      int sample = 1;
      while (big / sample > PHOTO_MAX_PX * 2) sample *= 2;
      opts.inSampleSize = sample;
      Bitmap bmp = BitmapFactory.decodeByteArray(raw, 0, raw.length, opts);
      if (bmp == null) return null;
      ByteArrayOutputStream out = new ByteArrayOutputStream();
      bmp.compress(Bitmap.CompressFormat.JPEG, 88, out);
      bmp.recycle();
      return out.toByteArray();
    } catch (Throwable t) {
      Log.w(TAG, "contacts: unreadable photo", t);
      return null;
    }
  }

  private static int phoneType(String t) {
    String s = t == null ? "" : t.toLowerCase();
    if (s.isEmpty()) return Phone.TYPE_OTHER;
    if (s.contains("fax")) return s.contains("work") ? Phone.TYPE_FAX_WORK : Phone.TYPE_FAX_HOME;
    if (s.contains("cell") || s.contains("mobile")) return Phone.TYPE_MOBILE;
    if (s.contains("work")) return Phone.TYPE_WORK;
    if (s.contains("home")) return Phone.TYPE_HOME;
    if (s.contains("pager")) return Phone.TYPE_PAGER;
    if (s.contains("other")) return Phone.TYPE_OTHER;
    return Phone.TYPE_CUSTOM;
  }

  private static int emailType(String t) {
    String s = t == null ? "" : t.toLowerCase();
    if (s.isEmpty()) return Email.TYPE_OTHER;
    if (s.contains("work")) return Email.TYPE_WORK;
    if (s.contains("home")) return Email.TYPE_HOME;
    if (s.contains("mobile")) return Email.TYPE_MOBILE;
    if (s.contains("other")) return Email.TYPE_OTHER;
    return Email.TYPE_CUSTOM;
  }
}
