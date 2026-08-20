package place.poster.app.sms;

import android.net.Uri;

import java.util.ArrayList;
import java.util.List;

/**
 * READING A `sms:` / `smsto:` / `mms:` / `mmsto:` URI. Pure, so the shapes are RUN rather than
 * assumed.
 *
 * They come from other apps — a browser, a contacts app, a dialer's reject-with-text — and there is
 * no single spec they all follow. Observed in the wild, all of which must work:
 *
 *     sms:+15550100                    the ordinary one
 *     smsto:5550100                    the other ordinary one
 *     sms://+15550100                  an authority instead of an opaque part
 *     sms:+15550100?body=hi%20there    RFC 5724's body parameter
 *     sms:+1555,+1666                  several recipients
 *     smsto:%2B15550100                percent-encoded
 *
 * Getting this wrong means a `Text` link in a web page opens an empty compose screen, which reads as
 * the app being broken by whoever pressed it.
 */
public final class SendTo {

    private SendTo() { }

    /** The first recipient, or "" — what a one-to-one compose screen needs. */
    public static String numberFrom(Uri uri) {
        List<String> all = numbersFrom(uri);
        return all.isEmpty() ? "" : all.get(0);
    }

    public static List<String> numbersFrom(Uri uri) {
        List<String> out = new ArrayList<String>();
        if (uri == null) return out;
        String part = uri.getSchemeSpecificPart();
        if (part == null || part.isEmpty()) part = uri.getAuthority();
        if (part == null) return out;
        // `sms://number` leaves the authority holding the number and the ssp holding "//number".
        while (part.startsWith("/")) part = part.substring(1);
        int q = part.indexOf('?');
        if (q >= 0) part = part.substring(0, q);
        for (String one : part.split("[,;]")) {
            String n = Uri.decode(one).trim();
            if (!n.isEmpty()) out.add(n);
        }
        return out;
    }

    /**
     * The prefilled text, from RFC 5724's `?body=`. Read from the URI's QUERY, and — because a `sms:`
     * URI is OPAQUE, so getQueryParameter() answers null on it — parsed by hand from the
     * scheme-specific part when it has to be.
     */
    public static String bodyFrom(Uri uri) {
        if (uri == null) return "";
        try {
            if (!uri.isOpaque()) {
                String v = uri.getQueryParameter("body");
                if (v != null) return v;
            }
        } catch (Throwable ignored) { }
        String part = uri.getSchemeSpecificPart();
        if (part == null) return "";
        int q = part.indexOf('?');
        if (q < 0) return "";
        for (String pair : part.substring(q + 1).split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && "body".equalsIgnoreCase(pair.substring(0, eq))) {
                return Uri.decode(pair.substring(eq + 1));
            }
        }
        return "";
    }

    /** Is this one of the four schemes a messages app is expected to answer? */
    public static boolean isMessageUri(Uri uri) {
        if (uri == null || uri.getScheme() == null) return false;
        String s = uri.getScheme().toLowerCase(java.util.Locale.ROOT);
        return "sms".equals(s) || "smsto".equals(s) || "mms".equals(s) || "mmsto".equals(s);
    }
}
