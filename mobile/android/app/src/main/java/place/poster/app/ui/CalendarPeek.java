package place.poster.app.ui;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.ContactsContract;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.List;
import java.util.Locale;

/**
 * "YOU HAVE A MEETING WITH THEM AT 3" — the one line of calendar context a phone screen can honestly
 * show.
 *
 * The calendar is encrypted with the person's own key and lives on a relay; a broadcast receiver
 * holding an incoming call has none of that. So this reads the SAME SharedPreferences blob the
 * home-screen calendar widget already draws from — the client pushes days it has already decrypted
 * (CalendarPlugin.push) and nothing native ever parses iCalendar or expands a recurrence rule. One
 * implementation of that lives in the client, where it is tested; a second one in Java is how the
 * widget and the app would end up disagreeing about what day something is on.
 *
 * MATCHING A PERSON TO A NUMBER happens HERE, against the phone's own address book, because that is
 * where the mapping is. An event's attendee is usually an email address; the phone knows which
 * contact that is and what their numbers are. Nothing is stored, nothing is written, and a failure
 * anywhere along the way means no context line — which is the correct answer, since a WRONG one
 * ("meeting with Alice") beside the wrong caller is worse than none.
 */
public final class CalendarPeek {

    /** Written by CalendarPlugin; see CalendarWidget for the format. */
    private static final String PREFS = "pcai_calendar";
    private static final String KEY_DAYS = "days";

    private CalendarPeek() { }

    /** One line, or "" when there is nothing today or tomorrow with this person. */
    public static String nextWith(Context ctx, String number) {
        try {
            if (ctx == null || number == null || number.trim().isEmpty()) return "";
            String raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_DAYS, "");
            if (raw == null || raw.isEmpty()) return "";
            List<String> ids = identifiersFor(ctx, number);
            if (ids.isEmpty()) return "";
            JSONObject days = new JSONObject(raw);
            Calendar cal = Calendar.getInstance();
            for (int ahead = 0; ahead <= 1; ahead++) {
                String key = String.format(Locale.ROOT, "%04d-%02d-%02d",
                        cal.get(Calendar.YEAR), cal.get(Calendar.MONTH) + 1,
                        cal.get(Calendar.DAY_OF_MONTH));
                JSONArray list = days.optJSONArray(key);
                if (list != null) {
                    for (int i = 0; i < list.length(); i++) {
                        JSONObject e = list.optJSONObject(i);
                        if (e == null) continue;
                        // Already finished — the widget dims these; a context line must not show one.
                        if (e.optBoolean("p", false)) continue;
                        if (!mentions(e.optJSONArray("w"), ids)) continue;
                        String at = e.optString("t", "");
                        String what = e.optString("s", "");
                        String when = ahead == 0 ? (at.isEmpty() ? "today" : at)
                                                 : "tomorrow" + (at.isEmpty() ? "" : " " + at);
                        return what.isEmpty() ? when : (what + "  ·  " + when);
                    }
                }
                cal.add(Calendar.DAY_OF_MONTH, 1);
            }
        } catch (Throwable ignored) {
            // A malformed blob, a locked device, no contacts permission. No line.
        }
        return "";
    }

    private static boolean mentions(JSONArray who, List<String> ids) {
        if (who == null) return false;
        for (int i = 0; i < who.length(); i++) {
            String v = who.optString(i, "");
            if (v.isEmpty()) continue;
            String norm = strip(v);
            for (String id : ids) if (!id.isEmpty() && norm.equalsIgnoreCase(id)) return true;
            // A `tel:` attendee is compared as a phone number, loosely — the same rule threads use,
            // because the same person is written three ways by three apps on one phone.
            if (v.toLowerCase(Locale.ROOT).startsWith("tel:")) {
                for (String id : ids) if (id.startsWith("+") || id.matches("[0-9]{5,}")) {
                    if (looseSame(norm, id)) return true;
                }
            }
        }
        return false;
    }

    private static boolean looseSame(String a, String b) {
        String x = a.replaceAll("[^0-9]", ""), y = b.replaceAll("[^0-9]", "");
        if (x.length() < 7 || y.length() < 7) return x.equals(y);
        return x.substring(x.length() - 7).equals(y.substring(y.length() - 7));
    }

    private static String strip(String v) {
        String s = v.trim();
        int c = s.indexOf(':');
        if (c >= 0 && (s.regionMatches(true, 0, "mailto:", 0, 7)
                    || s.regionMatches(true, 0, "tel:", 0, 4))) {
            s = s.substring(c + 1);
        }
        return s.trim();
    }

    /**
     * Everything this number's contact is known by — the number itself, and every email address on
     * the same contact card. That second half is what makes the feature work at all: a calendar
     * invitation names people by email and a phone rings with a number.
     */
    private static List<String> identifiersFor(Context ctx, String number) {
        List<String> out = new ArrayList<String>();
        out.add(number.trim());
        Cursor c = null;
        try {
            Uri uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI,
                                           Uri.encode(number));
            c = ctx.getContentResolver().query(uri,
                    new String[]{ ContactsContract.PhoneLookup.CONTACT_ID }, null, null, null);
            if (c == null || !c.moveToFirst()) return out;
            long contactId = c.getLong(0);
            c.close();
            c = ctx.getContentResolver().query(
                    ContactsContract.CommonDataKinds.Email.CONTENT_URI,
                    new String[]{ ContactsContract.CommonDataKinds.Email.ADDRESS },
                    ContactsContract.CommonDataKinds.Email.CONTACT_ID + "=?",
                    new String[]{ String.valueOf(contactId) }, null);
            while (c != null && c.moveToNext()) {
                String mail = c.getString(0);
                if (mail != null && !mail.trim().isEmpty()) out.add(mail.trim());
            }
        } catch (Throwable ignored) {
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        return out;
    }
}
