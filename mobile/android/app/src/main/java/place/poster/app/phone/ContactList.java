package place.poster.app.phone;

import android.content.Context;
import android.database.Cursor;
import android.provider.ContactsContract;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * THE PHONE'S ADDRESS BOOK, for the dialer's Contacts list and its search.
 *
 * The phone's OWN book, across every account — the same rule PhoneBook follows and for the same
 * reason: the person's contacts live wherever they live, and PosterChan's synced cards are already
 * among them. Nothing here writes, and nothing here keeps a copy. A dialer with its own contact
 * store is the third one on the phone, and the one that is always out of date.
 *
 * SEARCHING IS THE PROVIDER'S JOB, not ours. `Contacts.CONTENT_FILTER_URI` matches names, and
 * `Phone.CONTENT_FILTER_URI` matches numbers AND names including the T9-style matching an OEM
 * provider adds — reimplementing that in Java would be worse at it and would differ from what the
 * rest of the phone does.
 */
public final class ContactList {

    private static final String TAG = "PosterChan";

    public static final class Person {
        public long id;
        public String name = "";
        public String number = "";
        public String photo = "";

        public String label() { return name.isEmpty() ? number : name; }
    }

    private ContactList() { }

    private static final String[] COLS = {
        ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
        ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
        ContactsContract.CommonDataKinds.Phone.NUMBER,
        ContactsContract.CommonDataKinds.Phone.PHOTO_THUMBNAIL_URI,
    };

    /** Everybody with a phone number, A to Z. One row per person, not per number. */
    public static List<Person> all(Context ctx, int limit) {
        return read(ctx, ContactsContract.CommonDataKinds.Phone.CONTENT_URI, null, null, limit);
    }

    /**
     * Name-or-number search, handed to the provider. An empty query is the whole book rather than
     * nothing — the list is what somebody sees before they have typed anything.
     */
    public static List<Person> search(Context ctx, String q, int limit) {
        if (q == null || q.trim().isEmpty()) return all(ctx, limit);
        android.net.Uri uri = android.net.Uri.withAppendedPath(
                ContactsContract.CommonDataKinds.Phone.CONTENT_FILTER_URI,
                android.net.Uri.encode(q.trim()));
        return read(ctx, uri, null, null, limit);
    }

    private static List<Person> read(Context ctx, android.net.Uri uri, String where,
                                     String[] args, int limit) {
        List<Person> out = new ArrayList<Person>();
        if (ctx == null) return out;
        Cursor c = null;
        java.util.HashSet<Long> seen = new java.util.HashSet<Long>();
        try {
            c = ctx.getContentResolver().query(uri, COLS, where, args,
                    ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME
                        + " COLLATE NOCASE ASC");
            if (c == null) return out;
            while (c.moveToNext() && out.size() < limit) {
                long id = c.getLong(0);
                // ONE ROW PER PERSON. The Phone table has a row per NUMBER, so somebody with a
                // mobile and a work line appears twice — which in a contact list reads as duplicate
                // contacts rather than as two numbers.
                if (!seen.add(id)) continue;
                Person p = new Person();
                p.id = id;
                p.name = str(c, 1);
                p.number = str(c, 2);
                p.photo = str(c, 3);
                out.add(p);
            }
        } catch (Throwable t) {
            // No READ_CONTACTS yet, or a provider that refused. An empty list, never a crash on the
            // screen somebody opened to call somebody.
            Log.w(TAG, "tel: could not read contacts", t);
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        return out;
    }

    private static String str(Cursor c, int i) {
        try { String s = c.getString(i); return s == null ? "" : s; }
        catch (Throwable t) { return ""; }
    }

    /** Open a contact's card in the phone's own Contacts app. */
    public static android.content.Intent view(long contactId) {
        return new android.content.Intent(android.content.Intent.ACTION_VIEW,
                android.content.ContentUris.withAppendedId(
                        ContactsContract.Contacts.CONTENT_URI, contactId));
    }

    /** Matches for the dialer's own filter, when the query is being typed as digits. */
    public static boolean matches(Person p, String q) {
        if (q == null || q.isEmpty()) return true;
        String s = q.toLowerCase(Locale.ROOT);
        return p.name.toLowerCase(Locale.ROOT).contains(s)
            || p.number.replaceAll("[^0-9+]", "").contains(q.replaceAll("[^0-9+]", ""));
    }
}
