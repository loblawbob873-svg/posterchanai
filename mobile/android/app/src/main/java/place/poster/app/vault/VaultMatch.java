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

    // ---------------------------------------------------------------- which field is which

    /** One editable text field, as much as this decision needs to know about it. */
    public static final class FieldInfo {
        /** Joined autofillHints, lowercased. The app telling us what it is — believed over anything. */
        public String hints = "";
        /** A real password inputType: PASSWORD, WEB_PASSWORD or NUMBER_PASSWORD. */
        public boolean realPassword;
        /** textVisiblePassword. NOT the same thing — see pickFields. */
        public boolean visiblePassword;
        /** idEntry + hint + contentDescription, lowercased. */
        public String text = "";

        public FieldInfo(String hints, boolean realPassword, boolean visiblePassword, String text) {
            this.hints = hints == null ? "" : hints.toLowerCase(Locale.ROOT);
            this.realPassword = realPassword;
            this.visiblePassword = visiblePassword;
            this.text = text == null ? "" : text.toLowerCase(Locale.ROOT);
        }
    }

    private static final java.util.regex.Pattern _USER_RE = java.util.regex.Pattern.compile(
            "user|e-?mail|login|account|identifier|\\bid\\b|customer|member|signon|userid");
    private static final java.util.regex.Pattern _PASS_RE = java.util.regex.Pattern.compile(
            "password|passcode|passphrase|\\bpin\\b|\\bpwd\\b");
    /** Fields that are plainly something else, for the positional fallback only. */
    private static final java.util.regex.Pattern _NOT_USER_RE = java.util.regex.Pattern.compile(
            "search|query|amount|zip|postal|address|city|state|phone|card|cvv|expiry|otp|code|"
            + "\\bdob\\b|birth|comment|message|note");

    /**
     * Decide which field takes the username and which takes the password, over the WHOLE screen.
     *
     * THE BUG THIS EXISTS FOR: the walk took the first field that looked like a password and the
     * first that looked like a username, independently, in tree order — and it counted
     * `textVisiblePassword` as a password. Banks routinely put `textVisiblePassword` on the
     * USERNAME field, because it is how you stop the keyboard offering suggestions and learning what
     * someone types. So on Wells Fargo the username box was picked as the password box, the real
     * password box was then skipped (something was already chosen), and the password was typed into
     * the username field in front of the user.
     *
     * The rules that follow are ordered by how much the app is actually TELLING us versus how much
     * we are inferring, and the whole screen is scored before anything is chosen — first-wins over a
     * tree is what made one misread field poison the other slot.
     */
    public static int[] pickFields(List<FieldInfo> fields) {
        int user = -1, pass = -1, userScore = 0, passScore = 0;
        if (fields == null) return new int[]{-1, -1};
        for (int i = 0; i < fields.size(); i++) {
            FieldInfo f = fields.get(i);
            if (f == null) continue;
            int ps = 0, us = 0;
            // 4: the app declared it. An autofillHint is a statement, not a guess.
            if (f.hints.contains("password")) ps = 4;
            // NOT "phone". A shipping-address or checkout screen declares phone hints and has no
            // login on it at all — scoring it made the vault drop down over a phone-number box,
            // fill an email into it, and arm the "save this login?" prompt on an address form.
            else if (f.hints.contains("username") || f.hints.contains("emailaddress")
                     || f.hints.contains("email")) us = 4;
            // 3: a real password inputType — the keyboard is masking it, so it is a secret.
            if (ps == 0 && us == 0 && f.realPassword) ps = 3;
            // 2: it says so on the label.
            if (ps == 0 && us == 0 && _PASS_RE.matcher(f.text).find()) ps = 2;
            if (ps == 0 && us == 0 && _USER_RE.matcher(f.text).find()) us = 2;
            /* 1: visible-password, and ONLY if nothing above fired. This is the trap: it means "no
             * suggestions, no learning", which is what a bank wants on an account number as much as
             * on a password. Scored below every real signal so a screen with a genuine password
             * field always prefers that one, and never taken when the label says username. */
            if (ps == 0 && us == 0 && f.visiblePassword) ps = 1;
            if (ps > passScore) { passScore = ps; pass = i; }
            if (us > userScore) { userScore = us; user = i; }
        }
        /* One field cannot be both. Unreachable as the rules stand — `ps` and `us` are mutually
         * exclusive by construction (the hint rule is if/else and every later rule is gated on both
         * being zero) — and kept because it is the invariant the whole thing rests on: handing one
         * AutofillId both values writes the password into the box the user can read. */
        if (user >= 0 && user == pass) {
            if (passScore >= userScore) user = -1; else pass = -1;
        }
        /* A screen with a password and no named username field: take the editable field just BEFORE
         * it. That is where a username lives on a two-box login, and it is why a form whose boxes
         * carry no hints at all still fills. Only when the password was a strong signal — inferring
         * a username next to a field we ourselves only guessed is two guesses stacked. */
        if (pass > 0 && user < 0 && passScore >= 3) {
            FieldInfo prev = fields.get(pass - 1);
            /* A visible-password box IS allowed to be the username here — that is the exact bank
             * pattern (no suggestions on the customer ID), and excluding it would leave the
             * unlabelled version of the Wells Fargo screen with no username slot at all.
             *
             * But "the previous editable field" is only a guess about LAYOUT, and a screen has other
             * boxes: a toolbar search input sits ahead of the login card in tree order on most
             * WebView apps, and a checkout page has amounts. Disqualify anything that says what it
             * is and is not a username — otherwise this types someone's email into a search box. */
            if (prev != null && !prev.realPassword
                    && !_PASS_RE.matcher(prev.text).find()
                    && !_NOT_USER_RE.matcher(prev.text).find()) user = pass - 1;
        }
        return new int[]{user, pass};
    }

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
