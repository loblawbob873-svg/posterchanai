package place.poster.app.contacts;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.provider.ContactsContract;
import android.provider.ContactsContract.CommonDataKinds.Email;
import android.provider.ContactsContract.CommonDataKinds.Event;
import android.provider.ContactsContract.CommonDataKinds.Note;
import android.provider.ContactsContract.CommonDataKinds.Organization;
import android.provider.ContactsContract.CommonDataKinds.Phone;
import android.provider.ContactsContract.CommonDataKinds.StructuredName;
import android.provider.ContactsContract.CommonDataKinds.StructuredPostal;
import android.provider.ContactsContract.Data;
import android.provider.ContactsContract.RawContacts;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/**
 * The OTHER direction: what the phone changed, read back out of ContactsContract.
 *
 * WHY `DIRTY` IS THE WHOLE MECHANISM. ContactsProvider2 stamps `RawContacts.DIRTY=1` on any change
 * made through an ordinary (user) write, and does NOT stamp it on a write carrying
 * CALLER_IS_SYNCADAPTER — which is exactly what ContactWriter uses for every operation it performs.
 * So "dirty" means, precisely and with no bookkeeping of our own: somebody edited this card on the
 * phone since we last wrote it. That is why the sync-adapter parameter matters twice over — take it
 * off and every push marks the whole address book as user-edited, and the next pull uploads our own
 * writes back to the server as if they were the user's.
 *
 * A DELETE IS NOT A DISAPPEARANCE. Deleting one of our contacts in the phone's Contacts app leaves
 * the row in place with `DELETED=1` (a tombstone) until a sync adapter acknowledges it. That is a
 * feature here: it is the only record that the deletion happened, and it survives the app being
 * killed before it could tell the server.
 *
 * NOTHING IS FORGOTTEN UNTIL THE APP SAYS IT STORED IT. `changes()` only reads; `taken()` is a
 * separate call the client makes with the rows it actually persisted, and it clears DIRTY only
 * `WHERE _ID = ? AND VERSION = ?`. If the user edited the same contact again while the sweep was in
 * flight, the version has moved, the clear matches nothing, and the row stays dirty for the next
 * sweep — instead of that edit being marked as uploaded and silently lost. Same discipline as
 * ContactWriter.write() returning the UIDs that actually landed.
 */
public final class ContactReader {

  private static final String TAG = "PosterChan";

  /** Only ever OUR account: this reads a database full of other people's contacts. ONE definition,
   *  ContactWriter's, so the reads here and the deletes there cannot come to mean different rows. */
  private static final String OURS = ContactWriter.OURS;

  private static String[] ours() {
    return ContactWriter.oursArgs();
  }

  /** …and the same clause on a statement that names a single row. See ContactWriter.deleteRaw: the
   *  account scoping on these otherwise rests entirely on what the provider does with syncUri()'s
   *  query parameters, which is undocumented, on the two calls that clear a phone-side change. */
  private static String[] oneOfOurs(long rawId, String... extra) {
    String[] args = new String[3 + extra.length];      // the row, the account's two, then the rest
    args[0] = String.valueOf(rawId);
    args[1] = ContactWriter.ACCOUNT_TYPE;
    args[2] = ContactWriter.ACCOUNT_NAME;
    System.arraycopy(extra, 0, args, 3, extra.length);
    return args;
  }

  private ContactReader() {}

  /**
   * Give every phone-CREATED contact of ours a UID, and write it into the row before anybody sees it.
   *
   * A contact added in the phone's Contacts app under our account has no SOURCE_ID — it is not one of
   * ours yet. The UID is minted HERE, and stored on the row in the same pass, because the alternative
   * duplicates people: if the client minted one and the app died before the card reached the server,
   * the next sweep would mint a SECOND uid for the same row and the person would exist twice. With
   * the uid on the row, a sweep that fails is simply repeated — same uid, same card, one contact.
   *
   * Runs BEFORE changes() reads versions, because writing SOURCE_ID bumps RawContacts.VERSION and a
   * version read before the write would never match at acknowledge time.
   */
  public static int mintSourceIds(Context ctx) {
    List<Long> fresh = new ArrayList<>();
    Cursor c = null;
    try {
      c = ctx.getContentResolver().query(
          RawContacts.CONTENT_URI, new String[]{RawContacts._ID},
          OURS + " AND " + RawContacts.DELETED + "=0 AND (" + RawContacts.SOURCE_ID
              + " IS NULL OR " + RawContacts.SOURCE_ID + "='')",
          ours(), null);
      while (c != null && c.moveToNext()) fresh.add(c.getLong(0));
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not look for phone-created contacts", t);
    } finally {
      if (c != null) try { c.close(); } catch (Throwable ignored) {}
    }
    int done = 0;
    for (Long id : fresh) {
      ContentValues v = new ContentValues();
      v.put(RawContacts.SOURCE_ID, "pc-" + UUID.randomUUID().toString());
      try {
        // Guarded by "still has no source id" so two sweeps racing cannot overwrite each other's uid.
        done += ctx.getContentResolver().update(ContactWriter.syncUri(RawContacts.CONTENT_URI), v,
            RawContacts._ID + "=? AND " + OURS + " AND (" + RawContacts.SOURCE_ID
                + " IS NULL OR " + RawContacts.SOURCE_ID + "='')",
            oneOfOurs(id));
      } catch (Throwable t) {
        Log.w(TAG, "contacts: could not stamp a uid on a phone-created contact", t);
      }
    }
    return done;
  }

  /**
   * Every raw contact of ours the phone has touched since our last write: edits, creations and
   * deletions, as JSON the client can merge.
   */
  public static JSONArray changes(Context ctx) {
    JSONArray out = new JSONArray();
    Cursor c = null;
    try {
      c = ctx.getContentResolver().query(
          ContactWriter.syncUri(RawContacts.CONTENT_URI),
          new String[]{RawContacts._ID, RawContacts.SOURCE_ID, RawContacts.VERSION,
                       RawContacts.DELETED, RawContacts.CONTACT_ID},
          OURS + " AND (" + RawContacts.DIRTY + "=1 OR " + RawContacts.DELETED + "=1)",
          ours(), null);
      while (c != null && c.moveToNext()) {
        long rawId = c.getLong(0);
        String uid = c.getString(1);
        boolean deleted = c.getInt(3) != 0;
        if (uid == null || uid.isEmpty()) continue;   // mintSourceIds() gives it one next pass
        JSONObject row = new JSONObject();
        row.put("rawId", rawId);
        row.put("uid", uid);
        row.put("version", c.getLong(2));
        row.put("deleted", deleted);
        if (!deleted) {
          row.put("card", card(ctx, rawId, uid));
          row.put("updated", updatedAt(ctx, c.getLong(4)));
        }
        out.put(row);
      }
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not read what the phone changed", t);
    } finally {
      if (c != null) try { c.close(); } catch (Throwable ignored) {}
    }
    return out;
  }

  /**
   * UIDs with a phone-side change the client has not yet acknowledged.
   *
   * THE GUARD THAT STOPS A DELETED PERSON COMING BACK FOR EVER. A push is the app's copy of the
   * world, and the app's copy still holds somebody the phone has just deleted (that is the whole
   * point of the pull). Written blindly, the push re-inserts them, the next pull deletes them again,
   * and the two halves of the sync fight for as long as the app is open. So a card whose phone row
   * has an unacknowledged change is left ALONE by both halves of the push: not rewritten, and not
   * pruned either — a contact created on the phone and not yet stored on the server would otherwise
   * be deleted by the reconcile before it was ever uploaded.
   */
  public static Set<String> pending(Context ctx) {
    Set<String> out = new HashSet<>();
    Cursor c = null;
    try {
      c = ctx.getContentResolver().query(
          ContactWriter.syncUri(RawContacts.CONTENT_URI),
          new String[]{RawContacts.SOURCE_ID},
          OURS + " AND (" + RawContacts.DIRTY + "=1 OR " + RawContacts.DELETED + "=1)",
          ours(), null);
      while (c != null && c.moveToNext()) {
        String uid = c.getString(0);
        if (uid != null && !uid.isEmpty()) out.add(uid);
      }
    } catch (Throwable t) {
      Log.w(TAG, "contacts: could not list pending phone changes", t);
    } finally {
      if (c != null) try { c.close(); } catch (Throwable ignored) {}
    }
    return out;
  }

  /**
   * Acknowledge exactly the rows the client says it stored. Returns the raw ids actually cleared.
   *
   * Two ways this deliberately does nothing: a row whose VERSION has moved since `changes()` read it
   * (the user edited it again mid-sweep — it stays dirty and is picked up next time), and any row the
   * client did not list (a save that failed must be retried, not forgotten). Both failures are the
   * same shape as recording a hash for a batch the provider refused: the change is still on the
   * phone, nothing says so, and it is never sent again.
   */
  public static JSONArray taken(Context ctx, JSONArray rows) {
    JSONArray cleared = new JSONArray();
    if (rows == null) return cleared;
    for (int i = 0; i < rows.length(); i++) {
      JSONObject r = rows.optJSONObject(i);
      if (r == null) continue;
      long rawId = r.optLong("rawId", -1);
      if (rawId < 0) continue;
      long version = r.optLong("version", -1);
      boolean ok = false;
      try {
        if (r.optBoolean("deleted", false)) {
          // The tombstone has done its job: the server no longer has this card either.
          ok = ctx.getContentResolver().delete(ContactWriter.syncUri(RawContacts.CONTENT_URI),
              RawContacts._ID + "=? AND " + OURS + " AND " + RawContacts.DELETED + "=1",
              oneOfOurs(rawId)) > 0;
        } else {
          ContentValues v = new ContentValues();
          v.put(RawContacts.DIRTY, 0);
          ok = ctx.getContentResolver().update(ContactWriter.syncUri(RawContacts.CONTENT_URI), v,
              RawContacts._ID + "=? AND " + OURS + " AND " + RawContacts.VERSION + "=? AND "
                  + RawContacts.DELETED + "=0",
              oneOfOurs(rawId, String.valueOf(version))) > 0;
        }
      } catch (Throwable t) {
        Log.w(TAG, "contacts: could not acknowledge a phone-side change", t);
      }
      if (ok) cleared.put(rawId);
    }
    return cleared;
  }

  /** One raw contact's data rows, in the shape the client's vCard merge expects. */
  private static JSONObject card(Context ctx, long rawId, String uid) throws Exception {
    JSONObject out = new JSONObject();
    out.put("uid", uid);
    JSONArray tels = new JSONArray(), emails = new JSONArray(), adrs = new JSONArray();
    String fn = "", given = "", family = "", middle = "", prefix = "", suffix = "";
    String org = "", title = "", note = "", bday = "";
    Cursor c = null;
    try {
      c = ctx.getContentResolver().query(Data.CONTENT_URI,
          new String[]{Data.MIMETYPE, Data.DATA1, Data.DATA2, Data.DATA3, Data.DATA4,
                       Data.DATA5, Data.DATA6, Data.DATA7, Data.DATA8, Data.DATA9, Data.DATA10},
          Data.RAW_CONTACT_ID + "=?", new String[]{String.valueOf(rawId)}, null);
      while (c != null && c.moveToNext()) {
        String mime = c.getString(0);
        if (mime == null) continue;
        String d1 = s(c, 1);
        if (StructuredName.CONTENT_ITEM_TYPE.equals(mime)) {
          fn = d1;                       // DISPLAY_NAME
          given = s(c, 2); family = s(c, 3); prefix = s(c, 4); middle = s(c, 5); suffix = s(c, 6);
        } else if (Phone.CONTENT_ITEM_TYPE.equals(mime)) {
          if (!d1.isEmpty()) tels.put(kv(phoneLabel(c.getInt(2), s(c, 3)), d1));
        } else if (Email.CONTENT_ITEM_TYPE.equals(mime)) {
          if (!d1.isEmpty()) emails.put(kv(emailLabel(c.getInt(2), s(c, 3)), d1));
        } else if (Organization.CONTENT_ITEM_TYPE.equals(mime)) {
          if (org.isEmpty()) org = d1;
          if (title.isEmpty()) title = s(c, 4);          // Organization.TITLE == DATA4
        } else if (Note.CONTENT_ITEM_TYPE.equals(mime)) {
          if (note.isEmpty()) note = d1;
        } else if (Event.CONTENT_ITEM_TYPE.equals(mime)) {
          if (c.getInt(2) == Event.TYPE_BIRTHDAY && bday.isEmpty()) bday = d1;
        } else if (StructuredPostal.CONTENT_ITEM_TYPE.equals(mime)) {
          JSONObject a = new JSONObject();
          a.put("street", s(c, 4));      // DATA4
          a.put("city", s(c, 7));        // DATA7
          a.put("region", s(c, 8));      // DATA8
          a.put("code", s(c, 9));        // DATA9
          a.put("country", s(c, 10));    // DATA10
          adrs.put(a);
        }
      }
    } finally {
      if (c != null) try { c.close(); } catch (Throwable ignored) {}
    }
    if (fn.isEmpty()) fn = (given + " " + family).trim();
    out.put("fn", fn);
    out.put("given", given); out.put("family", family);
    out.put("middle", middle); out.put("prefix", prefix); out.put("suffix", suffix);
    out.put("org", org); out.put("title", title); out.put("note", note); out.put("bday", bday);
    out.put("tels", tels); out.put("emails", emails); out.put("adrs", adrs);
    return out;
  }

  private static JSONObject kv(String type, String value) throws Exception {
    JSONObject o = new JSONObject();
    o.put("type", type == null ? "" : type);
    o.put("value", value);
    return o;
  }

  private static String s(Cursor c, int col) {
    String v = c.getString(col);
    return v == null ? "" : v;
  }

  /**
   * When the aggregate this raw contact belongs to last changed, in millis.
   *
   * Only used to order two edits that BOTH happened since the last sync, which is the one case the
   * client cannot decide on its own. It is the aggregate's timestamp, not this row's — a contact
   * linked to a Google one moves when either side does — which is precisely why the client keeps the
   * losing version as a copy instead of trusting this to be exact.
   */
  private static long updatedAt(Context ctx, long contactId) {
    if (contactId <= 0) return 0;
    Cursor c = null;
    try {
      c = ctx.getContentResolver().query(ContactsContract.Contacts.CONTENT_URI,
          new String[]{ContactsContract.Contacts.CONTACT_LAST_UPDATED_TIMESTAMP},
          ContactsContract.Contacts._ID + "=?", new String[]{String.valueOf(contactId)}, null);
      if (c != null && c.moveToFirst()) return c.getLong(0);
    } catch (Throwable t) {
      Log.w(TAG, "contacts: no update timestamp for a contact", t);
    } finally {
      if (c != null) try { c.close(); } catch (Throwable ignored) {}
    }
    return 0;
  }

  /** The reverse of ContactWriter.phoneType — a vCard TYPE the client will recognise. */
  private static String phoneLabel(int type, String label) {
    switch (type) {
      case Phone.TYPE_MOBILE:   return "cell";
      case Phone.TYPE_WORK:     return "work";
      case Phone.TYPE_HOME:     return "home";
      case Phone.TYPE_FAX_WORK: return "fax work";
      case Phone.TYPE_FAX_HOME: return "fax home";
      case Phone.TYPE_PAGER:    return "pager";
      case Phone.TYPE_OTHER:    return "other";
      default:                  return label == null ? "" : label;
    }
  }

  private static String emailLabel(int type, String label) {
    switch (type) {
      case Email.TYPE_WORK:   return "work";
      case Email.TYPE_HOME:   return "home";
      case Email.TYPE_MOBILE: return "mobile";
      case Email.TYPE_OTHER:  return "other";
      default:                return label == null ? "" : label;
    }
  }
}
