package place.poster.app.vault;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.util.Log;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Where the autofill service reads the vault from.
 *
 * WHY A SECOND COPY EXISTS AT ALL. The autofill service is a separate process that Android wakes
 * when some OTHER app shows a login field — long after our WebView is gone. It cannot ask the web
 * layer for anything, and it cannot open the Nostr events itself without a NIP-44 implementation in
 * Java (secp256k1 ECDH, ChaCha20, HMAC) that would then be a second cryptographic implementation to
 * keep correct. So the app, which has already decrypted the vault, writes what autofill needs here,
 * and this is the only thing the service reads.
 *
 * ENCRYPTED WITH A KEYSTORE KEY, hardware-backed where the device has a TEE: the bytes in
 * SharedPreferences are useless off the device, and useless to another app (Android's per-app UID
 * sandbox already separates them; this is the layer that survives a backup extraction or a rooted
 * `adb pull`).
 *
 * setUserAuthenticationRequired(FALSE), deliberately. Requiring device auth on the KEY is how you
 * end up being asked for a fingerprint before the phone will even LIST which logins it has — every
 * cold start, every time the service wakes. The product decision here is that being signed into the
 * app is what unlocks the vault, exactly as it is for Notes; the phone's own lock screen is the
 * boundary. That is a real trade and it is written down rather than hidden: anyone who gets past
 * your lock screen with the app installed can autofill your passwords.
 *
 * No androidx.security dependency: this is sixty lines of AES/GCM against AndroidKeyStore, and
 * adding a library whose API has churned twice for it would be more surface, not less.
 */
public final class VaultStore {

    private static final String TAG = "PosterChanVault";
    private static final String PREFS = "pcvault";
    private static final String KEY_BLOB = "blob";
    private static final String ALIAS = "pcvault_v1";
    private static final int GCM_TAG_BITS = 128;
    private static final int IV_BYTES = 12;

    private VaultStore() {}

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** The Keystore predates this app's API-26 floor, so no insecure fallback is needed. */
    private static SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        KeyStore.Entry e = ks.getEntry(ALIAS, null);
        if (e instanceof KeyStore.SecretKeyEntry) return ((KeyStore.SecretKeyEntry) e).getSecretKey();
        KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        KeyGenParameterSpec.Builder b = new KeyGenParameterSpec.Builder(
                ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setUserAuthenticationRequired(false);
        kg.init(b.build());
        return kg.generateKey();
    }

    /** Replace the stored set. Called by the app whenever the vault changes. */
    public static synchronized boolean put(Context ctx, String json) {
        try {
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.ENCRYPT_MODE, key());
            byte[] iv = c.getIV();
            byte[] ct = c.doFinal(json.getBytes(StandardCharsets.UTF_8));
            byte[] out = new byte[iv.length + ct.length];
            System.arraycopy(iv, 0, out, 0, iv.length);
            System.arraycopy(ct, 0, out, iv.length, ct.length);
            prefs(ctx).edit().putString(KEY_BLOB, Base64.encodeToString(out, Base64.NO_WRAP)).apply();
            return true;
        } catch (Throwable t) {
            // Same reasoning as get(): a broken key must not be kept, or every future write fails.
            Log.w(TAG, "could not store the vault snapshot", t);
            reset(ctx);
            return false;
        }
    }

    /** The stored set, or "" when there is none / it cannot be read. Never throws at the caller. */
    public static synchronized String get(Context ctx) {
        String b64 = prefs(ctx).getString(KEY_BLOB, null);
        if (b64 == null) return "";
        try {
            byte[] all = Base64.decode(b64, Base64.NO_WRAP);
            if (all.length <= IV_BYTES) return "";
            byte[] iv = new byte[IV_BYTES];
            System.arraycopy(all, 0, iv, 0, IV_BYTES);
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(GCM_TAG_BITS, iv));
            byte[] pt = c.doFinal(all, IV_BYTES, all.length - IV_BYTES);
            return new String(pt, StandardCharsets.UTF_8);
        } catch (Throwable t) {
            // A Keystore key can be invalidated (device credential removed, app restored to another
            // device). Clearing only the BLOB is not enough: an invalidated alias still EXISTS, so
            // key() keeps returning it and both put() and get() throw forever — autofill silently
            // dead, put() returning false on every vault change, and no way back short of clearing
            // app data. Drop the alias too, so the next key() mints a fresh one.
            Log.i(TAG, "vault snapshot unreadable — resetting (" + t.getClass().getSimpleName() + ")");
            reset(ctx);
            return "";
        }
    }

    public static synchronized void clear(Context ctx) {
        // The apps list is NOT dropped here. clear() is reached from reset(), which runs when the
        // Keystore key is gone — the exact path this list is left unsealed to survive. Losing which
        // apps have asked would silently empty the association picker after an unrelated key
        // rotation. Signing out clears it explicitly (see forgetApps).
        prefs(ctx).edit().remove(KEY_BLOB).apply();
    }

    /** Signing out or unpairing: the association picker's history goes too. */
    public static synchronized void forgetApps(Context ctx) {
        prefs(ctx).edit().remove(KEY_APPS).remove(KEY_LASTFILL).apply();
    }

    /* ---------------------------------------------------------------- last fill, for diagnosis
     *
     * WHY THIS EXISTS. The autofill service runs in its own process, woken by another app, with no
     * UI of its own — so when it does the wrong thing on somebody's bank login there is NOTHING to
     * look at. The only way to see inside it was `adb logcat`, which means owning a computer, a
     * cable and developer mode, on a bug that by definition happens on a phone in someone's hand.
     * "The password went into the username box" was diagnosed twice from a description, and the
     * second fix did not hold, because a description cannot say which fields the screen actually
     * offered — which is the whole question.
     *
     * So the service writes down what it SAW and what it CHOSE, and the app shows it back.
     *
     * WHAT IT MAY NOT CONTAIN: nothing typed, nothing filled, and nothing out of the vault — not the
     * entry, not the username, not the password, not even how many matched. Only the shape of the
     * screen the phone was asked about: the asking package, and per field the autofill hints, the two
     * input-type booleans, and the field's own id/label text (which the APP chose, and which is what
     * the picker scores). That is app metadata, it is what the decision is made from, and it is the
     * minimum that makes the decision reviewable.
     *
     * NOT sealed under the Keystore, for the same reason the app list is not: it holds no secret, and
     * its whole value is being readable on the paths where something else has already gone wrong.
     */
    private static final String KEY_LASTFILL = "lastfill";
    private static final int MAX_LASTFILL = 8000;

    public static synchronized void noteFill(Context ctx, String json) {
        if (json == null) return;
        try {
            if (json.length() > MAX_LASTFILL) json = json.substring(0, MAX_LASTFILL);
            prefs(ctx).edit().putString(KEY_LASTFILL, json).apply();
        } catch (Throwable ignored) {}
    }

    /** The last fill request's shape as JSON, or "" if none has happened since install/sign-in. */
    public static synchronized String lastFill(Context ctx) {
        try { return prefs(ctx).getString(KEY_LASTFILL, ""); } catch (Throwable t) { return ""; }
    }

    /* ---------------------------------------------------------------- app associations
     *
     * Which apps have asked for a login, most recent first. That is all it is: package names and
     * nothing else — no entry ids, no usernames, no passwords, and no record of what was filled.
     *
     * It exists so PosterChan can say "Chase asked for a login — which entry is that?" and write
     * `androidapp://com.chase.sig.android` onto the one you pick, turning a ranked guess into a real
     * association that fills silently forever after. The alternative is asking someone to type a
     * package name, which nobody knows and nobody should have to look up.
     *
     * NOT sealed under the Keystore, unlike the vault: it holds no secret, and the whole point is
     * that it must still be readable on the paths where the Keystore is the thing that failed.
     */
    private static final String KEY_APPS = "apps";
    private static final int MAX_APPS = 40;

    public static synchronized void noteApp(Context ctx, String pkg) {
        if (pkg == null || pkg.isEmpty()) return;
        try {
            String cur = prefs(ctx).getString(KEY_APPS, "");
            java.util.List<String> out = new java.util.ArrayList<>();
            out.add(pkg);
            for (String p : cur.split("\n")) {
                p = p.trim();
                // Deduplicated by moving to the front, so "most recent" stays true and the list
                // does not fill with forty copies of the app you open every day.
                if (!p.isEmpty() && !p.equals(pkg) && out.size() < MAX_APPS) out.add(p);
            }
            // TextUtils.join keeps this persistence path independent of Java-library desugaring.
            prefs(ctx).edit().putString(KEY_APPS, android.text.TextUtils.join("\n", out)).apply();
        } catch (Throwable ignored) {}
    }

    /** Newline-separated, most recent first. "" when nothing has asked yet. */
    public static synchronized String apps(Context ctx) {
        try { return prefs(ctx).getString(KEY_APPS, ""); } catch (Throwable t) { return ""; }
    }

    /** Drop the stored snapshot AND the key that sealed it, so the next write starts clean. */
    public static synchronized void reset(Context ctx) {
        clear(ctx);
        try {
            KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
            ks.load(null);
            ks.deleteEntry(ALIAS);
        } catch (Throwable ignored) {}
    }

    /** True where the platform can host an AutofillService at all (API 26+). */
    public static boolean autofillSupported() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.O;
    }
}
