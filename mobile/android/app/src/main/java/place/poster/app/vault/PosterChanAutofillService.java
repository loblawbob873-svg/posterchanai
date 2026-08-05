package place.poster.app.vault;

import android.app.assist.AssistStructure;
import android.os.Build;
import android.os.CancellationSignal;
import android.service.autofill.AutofillService;
import android.service.autofill.Dataset;
import android.service.autofill.FillCallback;
import android.service.autofill.FillRequest;
import android.service.autofill.FillResponse;
import android.service.autofill.SaveCallback;
import android.service.autofill.SaveInfo;
import android.service.autofill.SaveRequest;
import android.text.InputType;
import android.util.Log;
import android.view.View;
import android.view.autofill.AutofillId;
import android.view.autofill.AutofillValue;
import android.widget.RemoteViews;

import androidx.annotation.NonNull;
import androidx.annotation.RequiresApi;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Android autofill for the PosterChan vault.
 *
 * WHAT IT READS. Only VaultStore — the snapshot the app wrote after decrypting the vault. This
 * service never touches the network, never talks to a relay, and holds no key material of its own,
 * so it works on a plane and cannot be the thing that leaks a vault.
 *
 * HOW IT MATCHES. The snapshot carries `hosts` and `domains` that were computed by the SHARED
 * vaultcore.js — the same rule the app and the Firefox extension use, including the multi-label
 * public-suffix cases. This file only compares strings. Re-implementing hostOf()/baseDomain() here
 * would be a fourth copy of the one rule that decides whether a password is offered on the right
 * site, and it would be the copy with no tests.
 *
 * An EXACT host match fills. A same-registrable-domain match is OFFERED (it appears in the list with
 * its site named) but is never the silent answer — the same distinction the extension makes.
 *
 * NATIVE APPS. Android hands over a package name, never a URL, so `com.chase.sig.android` cannot be
 * PROVEN to be chase.com from inside this process. Two ways an entry reaches an app screen, and the
 * difference between them is the whole design:
 *
 *   ASSOCIATED — the entry carries `androidapp://<package>` among its URIs. That is an association
 *                the USER made, or one that came across in a Bitwarden export (which writes them),
 *                so it is as good as an exact host match and is offered first, unlabelled.
 *   SUGGESTED  — everything else, ordered by VaultMatch.appRank and labelled "suggested". A guess is
 *                never dressed up as a match: the user reads their own entry's name and picks it,
 *                which is the same act as opening PosterChan and copying it, minus the typing.
 *
 * This file used to return NOTHING for a native app, reasoning that an unverifiable guess is worse
 * than no answer. The reasoning was right and the conclusion was wrong: refusing to show someone
 * their own vault is not the safe version of guessing — it is a password manager that does not work
 * in apps, which is where most people type most of their passwords. The safe version is to show it
 * and let them choose. (What was rightly removed was a reversed-package guess presented as the ONLY
 * row, which renders identically to a verified match; a labelled shortlist does not.)
 *
 * Every app request records the asking package (VaultStore.noteApp) so PosterChan can offer to turn
 * a suggestion into a permanent association.
 */
@RequiresApi(api = Build.VERSION_CODES.O)
public class PosterChanAutofillService extends AutofillService {

    private static final String TAG = "PosterChanAutofill";
    private static final int MAX_DATASETS = 8;

    /* Packages whose getWebDomain() means what it says. A native app can set that field to anything;
     * a browser is the only caller for which it is the address bar.
     *
     * An unlisted browser — or an in-app browser / Custom Tab inside another app — does not get
     * silence any more; its claimed domain is discarded and it is treated as an app, which means
     * associations it was given plus clearly-labelled suggestions ranked on its PACKAGE. That is
     * deliberately weak: nothing that surface says about which site it is showing is believed, and
     * the whole-vault fallback is withheld from it entirely (see matchApp). Adding a browser here
     * makes it work properly; leaving one out degrades it, and no longer breaks it. */
    private static final java.util.Set<String> BROWSERS = new java.util.HashSet<>(java.util.Arrays.asList(
            "com.android.chrome", "com.chrome.beta", "com.chrome.dev", "com.chrome.canary",
            "org.mozilla.firefox", "org.mozilla.firefox_beta", "org.mozilla.fenix",
            "org.mozilla.focus", "com.microsoft.emmx", "com.brave.browser",
            "com.opera.browser", "com.opera.mini.native", "com.duckduckgo.mobile.android",
            "com.vivaldi.browser", "com.sec.android.app.sbrowser", "com.kiwibrowser.browser",
            "org.chromium.chrome", "com.android.browser",
            "com.google.android.googlequicksearchbox", "com.samsung.android.app.sbrowser",
            "com.UCMobile.intl", "com.yandex.browser", "org.mozilla.fennec_fdroid",
            "us.spotco.fennec_dos", "io.github.forkmaintainers.iceraven", "com.neeva.app"));

    @Override
    public void onFillRequest(@NonNull FillRequest request, @NonNull CancellationSignal cancellationSignal,
                              @NonNull FillCallback callback) {
        try {
            List<AssistStructure> structures = new ArrayList<>();
            // The LAST context is the current screen; earlier ones are the history of this session.
            structures.add(request.getFillContexts().get(request.getFillContexts().size() - 1).getStructure());

            Parsed parsed = new Parsed();
            for (AssistStructure s : structures) {
                for (int i = 0; i < s.getWindowNodeCount(); i++) {
                    parse(s.getWindowNodeAt(i).getRootViewNode(), parsed);
                }
            }
            resolve(parsed);
            // Recorded even here — "the service found no field at all" is an outcome the diagnostic
            // has to be able to report, and it is the one that looks identical to autofill not being
            // installed. packageOf() is re-read rather than hoisted above this branch: the tests
            // anchor the webDomain rules on the pkg declaration below being the START of that
            // region, and hoisting it drags this early return's onSuccess(null) inside it.
            if (parsed.password == null && parsed.username == null) {
                note(parsed, packageOf(request), false, null, "no username or password field on this screen");
                callback.onSuccess(null);
                return;
            }

            String pkg = packageOf(request);
            // Whether this app told us it was showing a web page. It is not trusted to say WHICH
            // page — but that it is showing one at all changes what is safe to offer (see matchApp).
            boolean claimedWeb = false;
            /* ONLY A BROWSER MAY CLAIM TO BE A WEBSITE — but a non-browser that claims one is still
             * an APP, and it gets what any app gets.
             *
             * setWebDomain() is public API, so any installed app can describe a node as "chase.com"
             * and be handed the real Chase credential looking exactly like a legitimate match. That
             * is what this refuses: the CLAIM is discarded outright, never weighed.
             *
             * Refusing the whole request was too much, though. An app that renders its own login in
             * its own WebView — Sam's Club does — reports a web domain as a matter of course, and
             * got silence: no suggestions, no associated entry, nothing, on a screen where it is
             * plainly the app asking for its own password. Dropping to the package path costs
             * nothing security-wise, because that path never reads the claimed domain: it matches on
             * `androidapp://<package>` associations the user made, and labels everything else
             * "suggested" for the user to choose. A lying app gains exactly what an honest one does.
             */
            if (!(parsed.fieldDomain + parsed.pageDomain).isEmpty() && !BROWSERS.contains(pkg)) {
                Log.i(TAG, "ignoring a webDomain claimed by " + pkg + " — treating it as an app");
                parsed.fieldDomain = "";
                parsed.pageDomain = "";
                claimedWeb = true;
            }
            List<String> candidates = VaultMatch.hostCandidates(parsed.fieldDomain, parsed.pageDomain);
            String snapshot = VaultStore.get(this);
            List<JSONObject> matches;
            boolean fromPackage = candidates.isEmpty();
            if (!fromPackage) {
                matches = match(snapshot, candidates);
            } else {
                // A native app: no web domain to compare against, so this is the package path.
                if (!BROWSERS.contains(pkg)) VaultStore.noteApp(this, pkg);
                matches = matchApp(snapshot, pkg, claimedWeb);
            }
            if (matches.isEmpty()) {
                // Named, because "no suggestions here" is otherwise indistinguishable from a broken
                // service, an unsaved login and a field we never found.
                Log.i(TAG, "no match for " + (fromPackage ? pkg : candidates.toString()));
                note(parsed, pkg, claimedWeb, candidates, "no entry matched this app or site");
                callback.onSuccess(null);
                return;
            }

            FillResponse.Builder resp = new FillResponse.Builder();
            int added = 0;
            for (JSONObject it : matches) {
                if (added >= MAX_DATASETS) break;
                Dataset ds = dataset(parsed, it, fromPackage && !it.optBoolean("_app", false));
                if (ds != null) { resp.addDataset(ds); added++; }
            }
            // Counting DATASETS ADDED, not matches considered: an entry with no username and no
            // password builds no dataset, and a response with zero datasets is rejected by the
            // framework. The earlier version incremented per match, so this guard never fired.
            if (added == 0) {
                note(parsed, pkg, claimedWeb, candidates, "matched, but no entry had anything to fill");
                callback.onSuccess(null);
                return;
            }
            note(parsed, pkg, claimedWeb, candidates, "offered " + added + " login" + (added == 1 ? "" : "s"));

            // Offer to save what the user types when it is NOT already one of ours.
            List<AutofillId> ids = new ArrayList<>();
            if (parsed.username != null) ids.add(parsed.username);
            if (parsed.password != null) ids.add(parsed.password);
            if (!ids.isEmpty()) {
                resp.setSaveInfo(new SaveInfo.Builder(
                        SaveInfo.SAVE_DATA_TYPE_USERNAME | SaveInfo.SAVE_DATA_TYPE_PASSWORD,
                        ids.toArray(new AutofillId[0])).build());
            }
            callback.onSuccess(resp.build());
        } catch (Throwable t) {
            Log.w(TAG, "fill failed", t);
            callback.onSuccess(null);      // never crash the app being filled
        }
    }

    private Dataset dataset(Parsed p, JSONObject it, boolean suggestion) {
        String user = it.optString("username", "");
        String pass = it.optString("password", "");
        String title = it.optString("title", "");
        String label = user.isEmpty() ? (title.isEmpty() ? "PosterChan" : title)
                                      : user + (title.isEmpty() ? "" : "  ·  " + title);
        /* Say when it is a guess — and say it FIRST.
         *
         * The dataset row is singleLine + ellipsize="end" in a dropdown anchored to a text field,
         * so a marker on the end is the first thing dropped: `christopher.anderson@gmail.com  ·
         * Ch…` renders identically to a user-made association, which is precisely the property this
         * whole design rests on. Leading, it survives every truncation. */
        if (suggestion) label = "suggested  ·  " + label;
        // OUR layout, not android.R.layout.simple_list_item_1: a RemoteViews may only inflate a
        // layout belonging to the package it names, and the framework one throws at fill time on a
        // real device — the sort of failure that compiles, passes CI and breaks in someone's hand.
        RemoteViews rv = new RemoteViews(getPackageName(), place.poster.app.R.layout.autofill_dataset);
        rv.setTextViewText(place.poster.app.R.id.autofill_label, label);
        Dataset.Builder b = new Dataset.Builder(rv);
        boolean any = false;
        if (p.username != null && !user.isEmpty()) { b.setValue(p.username, AutofillValue.forText(user)); any = true; }
        if (p.password != null && !pass.isEmpty()) { b.setValue(p.password, AutofillValue.forText(pass)); any = true; }
        if (!any) return null;
        try { return b.build(); } catch (Throwable t) { return null; }
    }

    /** Which app is asking. */
    private static String packageOf(FillRequest request) {
        try {
            AssistStructure s = request.getFillContexts()
                    .get(request.getFillContexts().size() - 1).getStructure();
            return String.valueOf(s.getActivityComponent().getPackageName());
        } catch (Throwable t) { return ""; }
    }

    /** The saved credentials for this screen, best first: exact host, then same registrable domain.
     *  The comparison itself is VaultMatch, which is plain Java and has tests. */
    private List<JSONObject> match(String json, List<String> candidates) {
        List<JSONObject> exact = new ArrayList<>(), domain = new ArrayList<>();
        if (json == null || json.isEmpty() || candidates.isEmpty()) return exact;
        try {
            JSONArray arr = new JSONArray(json);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject it = arr.optJSONObject(i);
                if (it == null) continue;
                int r = VaultMatch.bestRank(strings(it.optJSONArray("hosts")),
                                            strings(it.optJSONArray("domains")), candidates);
                if (r == 2) exact.add(it);
                else if (r == 1) domain.add(it);
                // NOTE: no package-name guessing. See the class comment.
            }
        } catch (Throwable t) {
            Log.w(TAG, "unreadable snapshot", t);
        }
        exact.addAll(domain);
        return exact;
    }

    /**
     * The entries to offer on a NATIVE app screen: associated first, then ranked suggestions.
     *
     * `_app` marks the ones the user (or their import) actually associated, so the dataset builder
     * can label the rest honestly. Nothing is filled on a guess — the user reads their own entry's
     * name and picks it.
     */
    private List<JSONObject> matchApp(String json, String pkg, boolean claimedWeb) {
        List<JSONObject> out = new ArrayList<>();
        List<List<JSONObject>> ranked = new ArrayList<>();
        for (int i = 0; i < 4; i++) ranked.add(new ArrayList<JSONObject>());
        if (json == null || json.isEmpty() || pkg == null || pkg.isEmpty()) return out;
        try {
            JSONArray arr = new JSONArray(json);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject it = arr.optJSONObject(i);
                if (it == null) continue;
                List<String> uris = strings(it.optJSONArray("uris"));
                if (VaultMatch.appMatches(uris, pkg)) {
                    try { it.put("_app", true); } catch (Throwable ignored) {}
                    out.add(it);
                    continue;
                }
                int r = VaultMatch.appRank(pkg, strings(it.optJSONArray("hosts")),
                                           strings(it.optJSONArray("domains")),
                                           it.optString("title", ""));
                ranked.get(r).add(it);
            }
        } catch (Throwable t) {
            Log.w(TAG, "unreadable snapshot", t);
        }
        for (int r = 3; r >= 1; r--) out.addAll(ranked.get(r));
        /* Rank 0 is everything else — the "search my logins" row every other manager shows. Offered
         * only when nothing scored, so a screen with a real candidate is not buried under a hundred
         * unrelated ones.
         *
         * NOT on a surface that told us it was showing a web page. An in-app browser or Custom Tab
         * can be pointed at anything, including a phishing page, and "here is your entire vault,
         * pick one" a tap away from a convincing fake is a worse offer than nothing. An app showing
         * its OWN login (which is why that path exists at all) ranks against its own package and
         * never needs this fallback. */
        if (out.isEmpty() && !claimedWeb) out.addAll(ranked.get(0));
        return out;
    }

    /**
     * Write down the SHAPE of this request, so the app can show it back.
     *
     * The service has no UI and runs in another process when another app wakes it, so until this
     * existed the only window into a wrong decision was `adb logcat` — a computer, a cable and
     * developer mode, for a bug that only ever happens on a phone in someone's hand. Two rounds of
     * "the password went into the username box" were diagnosed from a description alone, and the
     * second fix did not hold, because a description cannot say which fields the screen offered.
     *
     * NOTHING FROM THE VAULT GOES IN HERE, and nothing that was typed or filled: not the entry, not
     * the username, not the password, not even how many matched. Only what the DECISION was made
     * from — the asking package, and per field the declared hints, the two input-type booleans and
     * the field's own id/label text, which the app itself chose. `outcome` is why it ended where it
     * did. That is the minimum that makes a wrong pick reviewable, and it is all of it.
     */
    private void note(Parsed p, String pkg, boolean claimedWeb, List<String> candidates, String outcome) {
        try {
            JSONObject o = new JSONObject();
            o.put("at", System.currentTimeMillis());
            o.put("pkg", pkg);
            o.put("outcome", outcome);
            o.put("claimedWeb", claimedWeb);
            o.put("hosts", candidates == null ? "" : candidates.toString());
            o.put("pickUser", p.pickUser);
            o.put("pickPass", p.pickPass);
            JSONArray fs = new JSONArray();
            for (int i = 0; i < p.fields.size() && i < 16; i++) {
                VaultMatch.FieldInfo f = p.fields.get(i);
                if (f == null) continue;
                JSONObject jf = new JSONObject();
                jf.put("hints", clip(f.hints, 40));
                jf.put("realPw", f.realPassword);
                jf.put("visPw", f.visiblePassword);
                jf.put("text", clip(f.text, 60));
                fs.put(jf);
            }
            o.put("fields", fs);
            o.put("fieldCount", p.fields.size());
            VaultStore.noteFill(this, o.toString());
        } catch (Throwable ignored) {}   // a diagnostic must never be the thing that breaks a fill
    }

    private static String clip(String s, int n) {
        String v = String.valueOf(s == null ? "" : s).trim();
        return v.length() <= n ? v : v.substring(0, n) + "…";
    }

    private static List<String> strings(JSONArray a) {
        List<String> out = new ArrayList<>();
        if (a == null) return out;
        for (int i = 0; i < a.length(); i++) out.add(a.optString(i, ""));
        return out;
    }

    // ---------------------------------------------------------------- structure

    private static final class Parsed {
        AutofillId username, password;
        /** Which entries of `fields` became the username and password slots; -1 for neither. Kept
         *  only so the diagnostic can report the DECISION alongside what it was made from. */
        int pickUser = -1, pickPass = -1;
        /** The domain of the document the field we are filling lives in — an SSO iframe, usually. */
        String fieldDomain = "";
        /** The outermost document's domain: the one that is actually in the address bar. */
        String pageDomain = "";
        /* Every editable text field on the screen, in tree order, with the domain that enclosed it.
         * Collected first and judged afterwards: taking the first thing that looked like a password
         * and the first that looked like a username, independently, is what let one misread field
         * poison the other slot — see VaultMatch.pickFields. */
        final List<VaultMatch.FieldInfo> fields = new ArrayList<>();
        final List<AutofillId> ids = new ArrayList<>();
        final List<String> domains = new ArrayList<>();
    }

    /**
     * Walk the view tree for the username and password fields.
     *
     * Autofill HINTS first, because an app that declares them is telling the truth about itself.
     * Then the input type (a variation of TEXT_VARIATION_PASSWORD is unambiguous), then the field's
     * own id/hint text. A WebView reports its page through getWebDomain(), which is what makes a
     * browser on the phone match the same way the desktop extension does.
     */
    private void parse(AssistStructure.ViewNode node, Parsed out) { parse(node, out, "", 0); }

    private void parse(AssistStructure.ViewNode node, Parsed out, String inherited, int depth) {
        if (node == null) return;
        String wd = node.getWebDomain();
        /* The domain in scope for everything below this node. Passed DOWN rather than latched
         * globally: the old code kept the first webDomain it met anywhere in the tree, so a page
         * whose analytics or captcha iframe happened to sort ahead of its login form convinced the
         * phone it was on that frame's site. Nothing matched, nothing was offered, and it looked
         * like the one site autofill "just doesn't work on". */
        if (wd != null && !wd.isEmpty()) {
            inherited = wd;
            if (out.pageDomain.isEmpty()) out.pageDomain = wd;   // shallowest = the top document
        }

        int type = node.getAutofillType();
        AutofillId id = node.getAutofillId();
        if (type == View.AUTOFILL_TYPE_TEXT && id != null && node.getVisibility() == View.VISIBLE) {
            StringBuilder hb = new StringBuilder();
            String[] hints = node.getAutofillHints();
            if (hints != null) for (String h : hints) if (h != null) hb.append(h).append(' ');
            String text = String.valueOf(node.getIdEntry()) + ' ' + node.getHint() + ' '
                        + node.getContentDescription();
            boolean htmlPassword = false, skip = false;
            /* WEBVIEW CONTENT. A page inside an app's own WebView is not a stack of Android views:
             * its fields carry NO autofillHints, NO idEntry and NO Android input type — they carry
             * HTML. `<input type="password">` and `autocomplete="current-password"` are the whole
             * signal, and without reading them a WebView login screen looks like a screen with no
             * fields on it, which is "autofill does nothing at all in this app". Increasingly that
             * is most apps. */
            android.view.ViewStructure.HtmlInfo html = node.getHtmlInfo();
            if (html != null && "input".equalsIgnoreCase(String.valueOf(html.getTag()))) {
                String itype = "", extra = "";
                java.util.List<android.util.Pair<String, String>> attrs = html.getAttributes();
                if (attrs != null) {
                    for (android.util.Pair<String, String> a : attrs) {
                        if (a == null || a.first == null) continue;
                        String k = a.first.toLowerCase(java.util.Locale.ROOT);
                        String v = a.second == null ? "" : a.second;
                        if (k.equals("type")) itype = v.toLowerCase(java.util.Locale.ROOT);
                        // name/id/placeholder/aria-label/autocomplete are what a page calls its own
                        // boxes; they feed the same text scoring an Android view's id would.
                        else if (k.equals("name") || k.equals("id") || k.equals("placeholder")
                                 || k.equals("aria-label") || k.equals("autocomplete")
                                 || k.equals("label")) extra += " " + v;
                    }
                }
                if (itype.equals("password")) htmlPassword = true;
                // A non-text input is not a login field: checkboxes, buttons and hidden inputs are
                // exactly what a form has most of. SKIPPED, not returned from — a `return` here
                // would abandon the rest of the subtree as well, and the walk still has children to
                // visit even when this node is not one we want.
                if (!itype.isEmpty() && !itype.equals("text") && !itype.equals("email")
                        && !itype.equals("tel") && !itype.equals("password")
                        && !itype.equals("number")) skip = true;
                text = text + extra;
            }
            if (!skip) {
                out.fields.add(new VaultMatch.FieldInfo(hb.toString(),
                                                        isRealPassword(node) || htmlPassword,
                                                        isVisiblePassword(node), text));
                out.ids.add(id);
                out.domains.add(inherited);
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) parse(node.getChildAt(i), out, inherited, depth + 1);
    }

    /** Turn the collected fields into a username slot and a password slot. */
    private static void resolve(Parsed out) {
        int[] pick = VaultMatch.pickFields(out.fields);
        out.pickUser = pick[0];
        out.pickPass = pick[1];
        if (pick[0] >= 0) {
            out.username = out.ids.get(pick[0]);
            if (out.fieldDomain.isEmpty()) out.fieldDomain = out.domains.get(pick[0]);
        }
        if (pick[1] >= 0) {
            out.password = out.ids.get(pick[1]);
            if (out.fieldDomain.isEmpty()) out.fieldDomain = out.domains.get(pick[1]);
        }
    }

    /** A masked field: the keyboard is hiding it, so it is a secret. */
    private static boolean isRealPassword(AssistStructure.ViewNode n) {
        int t = n.getInputType();
        int cls = t & InputType.TYPE_MASK_CLASS;
        int var = t & InputType.TYPE_MASK_VARIATION;
        if (cls == InputType.TYPE_CLASS_TEXT &&
                (var == InputType.TYPE_TEXT_VARIATION_PASSWORD
                        || var == InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD)) return true;
        return cls == InputType.TYPE_CLASS_NUMBER && var == InputType.TYPE_NUMBER_VARIATION_PASSWORD;
    }

    /**
     * `textVisiblePassword` — kept SEPARATE from a real password field, which is the whole fix.
     *
     * It does not mean "this is a password". It means "no suggestions, no autocorrect, do not learn
     * what is typed here" — which is exactly what a bank wants on an account number or a customer
     * ID. Wells Fargo sets it on the USERNAME box, so counting it as a password picked that box as
     * the password slot, skipped the real password box, and typed the password into the username
     * field on screen. VaultMatch.pickFields scores it below every real signal.
     */
    private static boolean isVisiblePassword(AssistStructure.ViewNode n) {
        int t = n.getInputType();
        return (t & InputType.TYPE_MASK_CLASS) == InputType.TYPE_CLASS_TEXT
                && (t & InputType.TYPE_MASK_VARIATION) == InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD;
    }

    /**
     * The user asked to save a login from another app.
     *
     * This service cannot write to the vault: the events are signed by the user's key, which lives
     * in the app and deliberately not here. So the credential is handed to the app, which is where
     * saving has always happened. Reporting success and dropping it would be the worst of both.
     */
    @Override
    public void onSaveRequest(@NonNull SaveRequest request, @NonNull SaveCallback callback) {
        callback.onFailure("Open PosterChan to save this login to your vault.");
    }
}
