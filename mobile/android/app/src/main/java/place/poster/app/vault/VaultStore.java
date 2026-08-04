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

    /** The Keystore is API 23+, which is this app's minSdk, so there is no fallback path to get wrong. */
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
        prefs(ctx).edit().remove(KEY_BLOB).apply();
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
