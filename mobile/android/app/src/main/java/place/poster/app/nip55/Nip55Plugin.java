package place.poster.app.nip55;

import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.database.Cursor;
import android.net.Uri;

import androidx.activity.result.ActivityResult;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.List;

/**
 * NIP-55 — signing via an Android signer app (Amber et al), so the user's key never enters this app.
 *
 * Two transports, per the NIP:
 *
 *  1. INTENT (always works). ACTION_VIEW on a "nostrsigner:<content>" URI with a "type" extra; the signer
 *     opens, the user approves, and the result comes back through onActivityResult. This is the path the
 *     spec guarantees, so it is the fallback for everything.
 *
 *  2. CONTENT RESOLVER (only after the user grants the permission, and only in newer signers). Same
 *     operations exposed at content://<signer package>.<TYPE>, answered WITHOUT showing any UI. This is
 *     what makes the app usable: decrypting a screen of DMs over the intent path would foreground the
 *     signer once per message. We attempt it first and fall back the moment anything is off — a null
 *     cursor, a "rejected" column, or a missing/empty "result" column — so a signer that implements it
 *     differently (or not at all) simply takes the intent path instead of failing.
 *
 * queryIntentActivities() needs the <queries> element in AndroidManifest.xml on API 30+, or the signer is
 * invisible to us and isAvailable() reports false on a device that has one installed.
 */
@CapacitorPlugin(name = "Nip55")
public class Nip55Plugin extends Plugin {

    private static final String SCHEME = "nostrsigner:";

    /** Which signer apps can handle nostrsigner: URIs? */
    @PluginMethod
    public void isAvailable(PluginCall call) {
        JSObject ret = new JSObject();
        JSArray signers = new JSArray();
        try {
            PackageManager pm = getContext().getPackageManager();
            Intent probe = new Intent(Intent.ACTION_VIEW, Uri.parse(SCHEME));
            List<ResolveInfo> infos = pm.queryIntentActivities(probe, 0);
            for (ResolveInfo ri : infos) {
                if (ri.activityInfo == null) continue;
                JSObject s = new JSObject();
                s.put("package", ri.activityInfo.packageName);
                s.put("label", String.valueOf(ri.loadLabel(pm)));
                // OURSELVES, since this app is a NIP-55 signer too. Marked rather than filtered out:
                // signing with our own on-device signer is the point — the key sits in the Keystore
                // instead of the WebView — and the login screen wants to say so by name.
                s.put("self", getContext().getPackageName().equals(ri.activityInfo.packageName));
                signers.put(s);
            }
        } catch (Exception ignored) {
        }
        ret.put("available", signers.length() > 0);
        ret.put("signers", signers);
        call.resolve(ret);
    }

    /**
     * One NIP-55 operation. `type` is the NIP's verb (get_public_key, sign_event, nip04_encrypt,
     * nip04_decrypt, nip44_encrypt, nip44_decrypt, …); `content` is the event JSON / plaintext /
     * ciphertext that rides in the URI; `pubkey` is the counterparty for the encrypt/decrypt verbs;
     * `currentUser` is the npub we are acting as, which lets a multi-account signer pick the right key.
     */
    @PluginMethod
    public void request(PluginCall call) {
        String type = call.getString("type", "");
        if (type == null || type.isEmpty()) {
            call.reject("missing type");
            return;
        }
        JSObject viaResolver = tryResolver(call, type);
        if (viaResolver != null) {
            call.resolve(viaResolver);
            return;
        }

        String content = call.getString("content", "");
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(SCHEME + (content == null ? "" : content)));
        String pkg = call.getString("pkg", "");
        if (pkg != null && !pkg.isEmpty()) intent.setPackage(pkg);
        intent.putExtra("type", type);
        putIfSet(intent, "pubkey", call.getString("pubkey", ""));
        putIfSet(intent, "current_user", call.getString("currentUser", ""));
        putIfSet(intent, "id", call.getString("id", ""));
        // Asking for the permissions we will actually need, up front, is what lets the signer remember the
        // grant — and a remembered grant is what enables the silent content-resolver path above.
        putIfSet(intent, "permissions", call.getString("permissions", ""));
        try {
            startActivityForResult(call, intent, "onSignerResult");
        } catch (Exception e) {
            call.reject("no signer app: " + e.getMessage());
        }
    }

    @ActivityCallback
    private void onSignerResult(PluginCall call, ActivityResult result) {
        if (call == null) return;
        Intent data = result == null ? null : result.getData();
        if (result == null || result.getResultCode() != android.app.Activity.RESULT_OK || data == null) {
            JSObject ret = new JSObject();
            ret.put("rejected", true);          // user hit deny, or backed out of the signer
            // WHY it refused, when it said. Our own signer answers "no key on this device" for a
            // phone that has not been given one yet, and that is a fixable situation the user can
            // only act on if they are told — collapsed into a bare "declined" it reads as a bug.
            if (data != null) ret.put("error", data.getStringExtra("error"));
            call.resolve(ret);
            return;
        }
        JSObject ret = new JSObject();
        // The scalar answer — npub for get_public_key, signature for sign_event, plaintext/ciphertext for
        // the nip04/nip44 verbs — comes back in "result" (NIP-55: `result.data?.getStringExtra("result")`).
        // "signature" is what OLD signers used, and Amber still sets it for get_public_key and sign_event
        // but NOT for the crypt verbs — so reading it first looked fine at login and then returned null for
        // every DM. Read the spec name first, keep the legacy one as a fallback for older signers.
        String scalar = data.getStringExtra("result");
        if (scalar == null) scalar = data.getStringExtra("signature");
        ret.put("result", scalar);
        ret.put("event", data.getStringExtra("event"));   // signed event JSON — present for sign_event
        ret.put("id", data.getStringExtra("id"));
        ret.put("package", data.getStringExtra("package"));
        ret.put("rejected", false);
        ret.put("viaResolver", false);
        call.resolve(ret);
    }

    private static void putIfSet(Intent intent, String key, String value) {
        if (value != null && !value.isEmpty()) intent.putExtra(key, value);
    }

    /**
     * Silent path. Returns null whenever we cannot be SURE we got a real answer, which sends the caller to
     * the intent flow — never a guess. Requires a package (we cannot address a content provider without
     * one), so the very first call of a session, get_public_key, always goes through the intent and is what
     * tells us the package name.
     */
    private JSObject tryResolver(PluginCall call, String type) {
        String pkg = call.getString("pkg", "");
        if (pkg == null || pkg.isEmpty()) return null;
        Cursor c = null;
        try {
            Uri uri = Uri.parse("content://" + pkg + "." + type.toUpperCase());
            String content = call.getString("content", "");
            String pubkey = call.getString("pubkey", "");
            String currentUser = call.getString("currentUser", "");
            // NIP-55 passes the arguments in the PROJECTION array: [content, counterparty pubkey, acting npub].
            String[] projection = new String[]{
                content == null ? "" : content,
                pubkey == null ? "" : pubkey,
                currentUser == null ? "" : currentUser
            };
            c = getContext().getContentResolver().query(uri, projection, null, null, null);
            if (c == null || !c.moveToFirst()) return null;      // not implemented / no permission granted
            if (c.getColumnIndex("rejected") > -1) return null;  // explicit refusal → let the intent ask
            int ri = c.getColumnIndex("result");
            if (ri < 0) return null;                             // unknown column layout → do not guess
            String res = c.getString(ri);
            if (res == null || res.isEmpty()) return null;
            JSObject ret = new JSObject();
            ret.put("result", res);
            int ei = c.getColumnIndex("event");
            ret.put("event", ei > -1 ? c.getString(ei) : null);
            ret.put("package", pkg);
            ret.put("rejected", false);
            ret.put("viaResolver", true);
            return ret;
        } catch (Exception e) {
            return null;                                         // provider missing, SecurityException, …
        } finally {
            if (c != null) try { c.close(); } catch (Exception ignored) {}
        }
    }
}
