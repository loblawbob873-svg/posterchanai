package place.poster.app.vault;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Which site the phone is looking at, and which saved logins belong to it.
 *
 * Deliberately free of Android and of org.json so it compiles and runs under plain javac — see
 * tests/test_android_autofill_match.py. The rule that decides whether a password is offered on the
 * right site is the one piece of this feature that must not be "probably fine", and on Android it
 * was previously buried in a recursive walk over AssistStructure, which no test can build.
 *
 * The HOSTS AND DOMAINS THEMSELVES are computed by the shared vaultcore.js and travel in the
 * snapshot; nothing here re-implements hostOf() or the public-suffix table. This file only compares.
 */
public final class VaultMatch {

    private VaultMatch() {}

    /**
     * The page's host, from whatever the platform called a "web domain".
     *
     * Accepts a bare host, a full URL, host:port, and a leading www., because different browsers
     * report different ones for the same page.
     */
    public static String normHost(String s) {
        if (s == null) return "";
        String h = s.trim().toLowerCase(Locale.ROOT);
        int i = h.indexOf("://");
        if (i >= 0) h = h.substring(i + 3);
        int slash = h.indexOf('/');
        if (slash >= 0) h = h.substring(0, slash);
        int colon = h.indexOf(':');
        if (colon >= 0) h = h.substring(0, colon);
        if (h.startsWith("www.")) h = h.substring(4);
        return h;
    }

    /**
     * The hosts to try, best first.
     *
     * THE BUG THIS EXISTS FOR: the walk used to keep the FIRST webDomain it met in tree order, and a
     * page's tree order is decided by its third-party iframes. On a site whose analytics or captcha
     * frame sorts ahead of the login form, the phone decided it was looking at that frame's domain,
     * found nothing, and offered nothing — while the same saved login filled fine on desktop. It
     * presents as "autofill doesn't work on this ONE site", which reads like bad data and is
     * actually a tree-order coin flip.
     *
     * So both are kept: the domain enclosing the field we are about to fill (right for a login
     * hosted in an SSO iframe) and the outermost document's (right for everything else, and the
     * only one that is ever the address bar). Field first — it is the more specific claim — but a
     * miss there falls through to the page rather than giving up.
     */
    public static List<String> hostCandidates(String fieldDomain, String pageDomain) {
        List<String> out = new ArrayList<>(2);
        String a = normHost(fieldDomain), b = normHost(pageDomain);
        if (!a.isEmpty()) out.add(a);
        if (!b.isEmpty() && !b.equals(a)) out.add(b);
        return out;
    }

    /** An exact host match: the strongest claim, and the only one anything fills silently on. */
    public static boolean hostMatches(List<String> hosts, String host) {
        if (hosts == null || host.isEmpty()) return false;
        for (String h : hosts) if (h != null && host.equalsIgnoreCase(h.trim())) return true;
        return false;
    }

    /**
     * Same registrable domain: `login.hsbc.co.uk` against a stored `hsbc.co.uk`.
     *
     * Suffix-compared against the domain vaultcore.js already computed, so this file never has to
     * know what a public suffix is. `.` is required before it — otherwise `notpaypal.com` ends with
     * `paypal.com` and is offered PayPal's password.
     */
    public static boolean domainMatches(List<String> domains, String host) {
        if (domains == null || host.isEmpty()) return false;
        for (String d0 : domains) {
            if (d0 == null) continue;
            String d = d0.trim().toLowerCase(Locale.ROOT);
            if (d.isEmpty()) continue;
            if (host.equals(d) || host.endsWith("." + d)) return true;
        }
        return false;
    }

    // ---------------------------------------------------------------- native apps

    /**
     * The package an `androidapp://com.example` URI names, or "" for anything else.
     *
     * This is the association every other password manager stores, and Bitwarden writes it into its
     * export — so a vault imported from one already knows which app each login belongs to. Nothing
     * here has to be invented; it only has to be READ, which is what was missing.
     */
    public static String packageOfUri(String uri) {
        String s = String.valueOf(uri == null ? "" : uri).trim();
        int i = s.indexOf("://");
        if (i < 0) return "";
        String scheme = s.substring(0, i).toLowerCase(Locale.ROOT);
        if (!scheme.equals("androidapp") && !scheme.equals("android")) return "";
        String rest = s.substring(i + 3);
        int cut = rest.indexOf('/');
        if (cut >= 0) rest = rest.substring(0, cut);
        // The SCHEME is case-insensitive; the package is NOT. Android package names are
        // case-sensitive and uppercase is legal, so folding here lets a sideloaded `Com.Chase`
        // inherit the association its owner made for `com.chase` — and inherit it at
        // association grade, which fills without a warning.
        return rest.trim();
    }

    /** Does this entry name this app outright? The app equivalent of an exact host match. */
    public static boolean appMatches(List<String> uris, String pkg) {
        if (uris == null || pkg == null || pkg.isEmpty()) return false;
        for (String u : uris) if (pkg.equals(packageOfUri(u))) return true;
        return false;
    }

    /**
     * How likely this entry belongs to this app — for ORDERING A LIST THE USER PICKS FROM, and for
     * nothing else.
     *
     * The distinction matters and is the reason native apps got nothing at all before. Android hands
     * over a package name, never a URL, so `com.chase.sig.android` cannot be PROVEN to be chase.com
     * from inside this process; a manager that treats a name-guess as a match will hand a bank
     * credential to any app that names itself convincingly. But refusing to show the user their own
     * vault is not the safe version of that — it is just a password manager that does not work in
     * apps, which is where most people type most of their passwords.
     *
     * So: nothing is ever filled silently on a guess. The user sees their own entries, by name, and
     * chooses one — the same act as opening the app and copying it, minus the typing. This score
     * only decides what is near the top.
     */
    public static int appRank(String pkg, List<String> hosts, List<String> domains, String title) {
        String p = String.valueOf(pkg == null ? "" : pkg).toLowerCase(Locale.ROOT);
        if (p.isEmpty()) return 0;
        int best = 0;
        // `com.chase.sig.android` -> chase, sig, android; the vendor's own name is in there
        // somewhere, and so is a lot of noise, which is why this only sorts.
        List<String> parts = new ArrayList<>();
        for (String seg : p.split("\\.")) {
            if (seg.length() >= 3 && !_NOISE.contains(seg)) parts.add(seg);
        }
        for (List<String> src : java.util.Arrays.asList(domains, hosts)) {
            if (src == null) continue;
            for (String h0 : src) {
                String h = normHost(h0);
                if (h.isEmpty()) continue;
                String label = h.split("\\.")[0];          // chase.com -> chase
                // The SAME noise filter the package segments get. Without it a host label of "com"
                // — which arrives whenever an `androidapp://com.…` URI leaks into the host list —
                // matches every package on earth by substring: `com.comcast.xfinity` scored 2
                // against a Gmail entry and sorted above the real Comcast one.
                if (label.length() < 3 || _NOISE.contains(label)) continue;
                for (String seg : parts) {
                    if (seg.equals(label)) best = Math.max(best, 3);
                    else if (seg.contains(label) || label.contains(seg)) best = Math.max(best, 2);
                }
            }
        }
        String t = String.valueOf(title == null ? "" : title).toLowerCase(Locale.ROOT)
                         .replaceAll("[^a-z0-9]", "");
        if (best == 0 && t.length() >= 3) {
            for (String seg : parts) if (t.contains(seg) || seg.contains(t)) { best = 1; break; }
        }
        return best;
    }

    /** Package segments that identify nobody. */
    private static final java.util.Set<String> _NOISE = new java.util.HashSet<>(java.util.Arrays.asList(
            "com", "org", "net", "android", "app", "apps", "mobile", "client", "www", "inc",
            "the", "prod", "release", "google", "co", "uk", "us"));

    /** exact = 2, same-domain = 1, no = 0. Higher is a better match, and only 2 may fill silently. */
    public static int rank(List<String> hosts, List<String> domains, String host) {
        if (hostMatches(hosts, host)) return 2;
        if (domainMatches(domains, host)) return 1;
        return 0;
    }

    /**
     * The best rank across every candidate host.
     *
     * Best, not first: if the field sits in an SSO iframe we hold nothing for, but the page itself
     * is a site we do hold, the page's exact match is the right answer — taking the first candidate
     * that scored anything would have settled for the iframe's weaker one.
     */
    public static int bestRank(List<String> hosts, List<String> domains, List<String> candidates) {
        int best = 0;
        if (candidates == null) return 0;
        for (String c : candidates) {
            int r = rank(hosts, domains, c);
            if (r > best) best = r;
            if (best == 2) break;
        }
        return best;
    }
}
