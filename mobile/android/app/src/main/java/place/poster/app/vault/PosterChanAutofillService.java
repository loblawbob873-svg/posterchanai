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
import java.util.Locale;

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
     * a browser is the only caller for which it is the address bar. An allowlist is coarse — a
     * browser not listed here simply gets no autofill, which is a missing convenience — and the
     * alternative is handing bank credentials to whatever app asks for them. */
    private static final java.util.Set<String> BROWSERS = new java.util.HashSet<>(java.util.Arrays.asList(
            "com.android.chrome", "com.chrome.beta", "com.chrome.dev", "com.chrome.canary",
            "org.mozilla.firefox", "org.mozilla.firefox_beta", "org.mozilla.fenix",
            "org.mozilla.focus", "com.microsoft.emmx", "com.brave.browser",
            "com.opera.browser", "com.opera.mini.native", "com.duckduckgo.mobile.android",
            "com.vivaldi.browser", "com.sec.android.app.sbrowser", "com.kiwibrowser.browser",
            "org.chromium.chrome", "com.android.browser"));

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
            if (parsed.password == null && parsed.username == null) { callback.onSuccess(null); return; }

            /* ONLY A BROWSER MAY CLAIM TO BE A WEBSITE. ViewStructure.setWebDomain() is public API,
             * so any installed app can describe a virtual node as "chase.com" and be handed the
             * real Chase credential, rendered exactly like a legitimate match. Removing the
             * reversed-package guess closed the long way round to that; this is the short one.
             * Without a verified browser on the other end there is nothing here we can trust, and
             * offering nothing is the correct answer. */
            String pkg = packageOf(request);
            if (!(parsed.fieldDomain + parsed.pageDomain).isEmpty() && !BROWSERS.contains(pkg)) {
                // An app claiming a web domain it cannot be trusted for gets NOTHING, still — the
                // package path below is for apps that make no such claim. Falling through to it here
                // would reward the lie with a shortlist.

                Log.i(TAG, "ignoring a webDomain claimed by " + pkg);
                callback.onSuccess(null);
                return;
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
                matches = matchApp(snapshot, pkg);
            }
            if (matches.isEmpty()) {
                // Named, because "no suggestions here" is otherwise indistinguishable from a broken
                // service, an unsaved login and a field we never found.
                Log.i(TAG, "no match for " + (fromPackage ? pkg : candidates.toString()));
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
            if (added == 0) { callback.onSuccess(null); return; }

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
    private List<JSONObject> matchApp(String json, String pkg) {
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
        // Rank 0 is everything else — the "search my logins" row every other manager shows. Offered
        // only when nothing scored, so a screen with a real candidate is not buried under a hundred
        // unrelated ones.
        if (out.isEmpty()) out.addAll(ranked.get(0));
        return out;
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
        /** The domain of the document the field we are filling lives in — an SSO iframe, usually. */
        String fieldDomain = "";
        /** The outermost document's domain: the one that is actually in the address bar. */
        String pageDomain = "";
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

        String[] hints = node.getAutofillHints();
        int type = node.getAutofillType();
        if (type == View.AUTOFILL_TYPE_TEXT) {
            if (hints != null) {
                for (String h : hints) {
                    if (h == null) continue;
                    String hh = h.toLowerCase(Locale.ROOT);
                    if (out.password == null && hh.contains("password")) out.password = node.getAutofillId();
                    else if (out.username == null && (hh.contains("username") || hh.contains("email")))
                        out.username = node.getAutofillId();
                }
            }
            if (out.password == null && isPasswordInput(node)) out.password = node.getAutofillId();
            if (out.username == null && looksLikeUsername(node)) out.username = node.getAutofillId();
            // Whichever field we settled on, remember the document it was in.
            if (out.fieldDomain.isEmpty() && (out.password != null || out.username != null))
                out.fieldDomain = inherited;
        }
        for (int i = 0; i < node.getChildCount(); i++) parse(node.getChildAt(i), out, inherited, depth + 1);
    }

    private static boolean isPasswordInput(AssistStructure.ViewNode n) {
        int t = n.getInputType();
        int cls = t & InputType.TYPE_MASK_CLASS;
        int var = t & InputType.TYPE_MASK_VARIATION;
        if (cls == InputType.TYPE_CLASS_TEXT &&
                (var == InputType.TYPE_TEXT_VARIATION_PASSWORD
                        || var == InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
                        || var == InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD)) return true;
        return cls == InputType.TYPE_CLASS_NUMBER && var == InputType.TYPE_NUMBER_VARIATION_PASSWORD;
    }

    private static boolean looksLikeUsername(AssistStructure.ViewNode n) {
        String hay = String.valueOf(n.getIdEntry()) + ' ' + n.getHint() + ' ' + n.getContentDescription();
        hay = hay.toLowerCase(Locale.ROOT);
        return hay.contains("user") || hay.contains("email") || hay.contains("login")
                || hay.contains("account") || hay.contains("identifier");
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
