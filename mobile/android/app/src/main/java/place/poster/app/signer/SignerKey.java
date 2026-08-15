package place.poster.app.signer;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * The signing key, held where the WebView cannot reach it.
 *
 * THIS IS A SECURITY UPGRADE, NOT A TRADE. Until now the secret key lived in the WebView's own
 * storage — reachable by any script that got into the page, and copied out by anything that could
 * read the app's web data. Here it is AES-GCM sealed under an AndroidKeyStore key, hardware-backed
 * on any device with a TEE: the bytes in SharedPreferences are useless off the device, and the key
 * that unseals them cannot be exported at all.
 *
 * The same shape as `vault/VaultStore` and for the same reasons, down to
 * `setUserAuthenticationRequired(false)`: requiring device auth on the KEY means a fingerprint
 * prompt before the phone will answer any signing request at all, including the ones that arrive
 * while the screen is off. The boundary is the lock screen and being signed into the app — stated
 * out loud rather than implied, because it is a real trade: anyone past your lock screen with this
 * app installed can sign as you. Amber makes the same one.
 *
 * A SEPARATE KEYSTORE ALIAS from the vault's, so the two cannot be confused and revoking one does
 * not silently take the other with it.
 */
public final class SignerKey {

    private static final String PREFS = "pcsigner";
    private static final String K_SEC = "sec";        // the sealed 32-byte secret
    private static final String K_PUB = "pub";        // its x-only pubkey, hex — public, so plain
    private static final String ALIAS = "pcsigner_v1";
    private static final int GCM_TAG_BITS = 128;
    private static final int IV_BYTES = 12;

    private SignerKey() { }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        KeyStore.Entry e = ks.getEntry(ALIAS, null);
        if (e instanceof KeyStore.SecretKeyEntry) return ((KeyStore.SecretKeyEntry) e).getSecretKey();
        KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        kg.init(new KeyGenParameterSpec.Builder(
                ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setUserAuthenticationRequired(false)
                .build());
        return kg.generateKey();
    }

    /** Store a 32-byte secret. Returns its x-only pubkey hex, so the caller can show whose key it is. */
    public static String store(Context ctx, byte[] sec) throws Exception {
        if (sec == null || sec.length != 32) throw new IllegalArgumentException("need 32 bytes");
        String pub = Nostr.hex(Nostr.pubkey(sec));            // also validates the scalar's range
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, key());
        byte[] iv = c.getIV(), ct = c.doFinal(sec);
        byte[] blob = new byte[iv.length + ct.length];
        System.arraycopy(iv, 0, blob, 0, iv.length);
        System.arraycopy(ct, 0, blob, iv.length, ct.length);
        prefs(ctx).edit()
                .putString(K_SEC, Base64.encodeToString(blob, Base64.NO_WRAP))
                .putString(K_PUB, pub)
                .apply();
        return pub;
    }

    /** The secret, or null when this phone holds none. Callers must not log or return it. */
    public static byte[] load(Context ctx) {
        try {
            String b64 = prefs(ctx).getString(K_SEC, null);
            if (b64 == null) return null;
            byte[] blob = Base64.decode(b64, Base64.NO_WRAP);
            if (blob.length <= IV_BYTES) return null;
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.DECRYPT_MODE, key(),
                   new GCMParameterSpec(GCM_TAG_BITS, blob, 0, IV_BYTES));
            return c.doFinal(blob, IV_BYTES, blob.length - IV_BYTES);
        } catch (Throwable t) {
            return null;      // a wiped Keystore (restored backup, changed lock) reads as "no key"
        }
    }

    public static String pubkey(Context ctx) {
        return prefs(ctx).getString(K_PUB, null);
    }

    public static boolean have(Context ctx) {
        return prefs(ctx).getString(K_SEC, null) != null;
    }

    /** Forget it. The Keystore alias is left alone — it protects nothing once the blob is gone. */
    public static void clear(Context ctx) {
        prefs(ctx).edit().remove(K_SEC).remove(K_PUB).apply();
    }

    // ---- which other apps may sign without being asked again -----------------------------------
    /* Remembered per calling package, not per request: being asked to approve every single signature
     * is what makes a signer something people turn off. "Never" is remembered too — an app that was
     * refused must not get another dialog every time it retries, which is how a denial becomes a
     * prompt loop the user cannot escape except by uninstalling something. */
    /* IS THE KEY EXPOSED TO OTHER APPS ON THIS PHONE — a SEPARATE question from "is there a key".
     *
     * These were one flag, and that is why the background signer did not work. The key is loaded by
     * TWO different things: `SignerActivity`, which lets other apps on this phone sign the way they
     * would with Amber, and `SignerRelayService`, which answers YOUR OTHER DEVICES over a relay. The
     * only thing that ever stored it was the switch for the first one — "Sign for other apps on this
     * phone", in a different settings section, describing a different feature. Nobody pairing a
     * laptop by QR has any reason to turn that on, so `reload()` found no key, closed every socket
     * and returned; `connected` stayed 0 for ever, so the hand-over could never be accepted and the
     * PAGE went on signing — full speed on screen and throttled to about one request a minute behind
     * it. Reported all day as "the signer is not working in background mode".
     *
     * So the key can now be stored for the SERVICE alone (`arm`), and being reachable by other apps
     * is its own opt-in. Absent means: a key that predates this flag came from the old switch, which
     * DID mean exposed — anything else would silently turn NIP-55 off for everyone who had it. */
    private static final String K_EXPOSED = "nip55";

    public static boolean exposed(Context ctx) {
        SharedPreferences p = prefs(ctx);
        if (!p.contains(K_EXPOSED)) return have(ctx);
        return p.getBoolean(K_EXPOSED, false);
    }

    public static void setExposed(Context ctx, boolean on) {
        prefs(ctx).edit().putBoolean(K_EXPOSED, on).apply();
    }

    public static String grant(Context ctx, String pkg) {
        if (pkg == null) return null;
        return prefs(ctx).getString("grant:" + pkg, null);      // "always" | "never" | null
    }

    public static void setGrant(Context ctx, String pkg, String value) {
        if (pkg == null) return;
        prefs(ctx).edit().putString("grant:" + pkg, value).apply();
    }
}
