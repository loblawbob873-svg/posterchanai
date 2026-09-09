package place.poster.app.push;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONObject;
import org.json.JSONArray;

import java.security.KeyStore;
import java.util.UUID;
import java.util.LinkedHashSet;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Device identity and sealed credentials for PosterChan Direct. */
final class DirectPushStore {
    private static final String PREFS = PushEventService.PREFS;
    private static final String DEVICE = "direct_device_id";
    private static final String SEALED = "direct_credentials";
    private static final String RECEIPTS = "direct_receipts";
    private static final String TYPE_PREFS = "push_type_prefs";
    private static final int MAX_RECEIPTS = 256;
    private static final String ALIAS = "posterchan_direct_push_v1";
    private static final int IV_BYTES = 12;

    static final class Credentials {
        final String socketUrl;
        final String token;
        final String deviceId;

        Credentials(String socketUrl, String token, String deviceId) {
            this.socketUrl = socketUrl;
            this.token = token;
            this.deviceId = deviceId;
        }
    }

    private DirectPushStore() { }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Public, random and stable across app upgrades. It exists before notifications are enabled. */
    /* WHICH NOTIFICATIONS THIS PHONE WANTS, held natively.
     *
     * The server filters too, and that is the primary gate — but it can only filter once the client
     * has managed to tell it, and that call can fail (offline, a refused signature, a reinstall that
     * left a stale row). The failure is silent and reads as the original complaint coming back: you
     * turn likes off and the phone keeps buzzing. This copy is written from the same place the
     * server mirror is sent, so the device enforces its own answer even when the server has the
     * wrong one. localStorage cannot do this job — the WebView is not running when a push arrives.
     */
    static void setTypePrefs(Context context, String json) {
        prefs(context).edit().putString(TYPE_PREFS, json == null ? "" : json).apply();
    }

    static boolean allowsType(Context context, String type) {
        try {
            return allowsType(prefs(context).getString(TYPE_PREFS, ""), type);
        } catch (Throwable ignored) {
            return true;
        }
    }

    /** The decision, with no Context so it can be run directly by a test.
     *
     * FAIL OPEN, exactly like app/services/push_prefs.py at the other end: unset, unparseable or
     * unknown all mean SHOW. A silenced notification is indistinguishable from a lost one, and the
     * expensive mistake here is a direct message that never appeared — not one buzz too many. A
     * call carries no toggle and must ring whatever else is switched off.
     */
    static boolean allowsType(String storedJson, String type) {
        if (type == null || type.isEmpty() || "call".equals(type)) return true;
        if (storedJson == null || storedJson.isEmpty()) return true;
        try {
            JSONObject o = new JSONObject(storedJson);
            return o.isNull(type) || o.optBoolean(type, true);
        } catch (Throwable ignored) {
            return true;
        }
    }

    static String deviceId(Context context) {
        SharedPreferences p = prefs(context);
        String id = p.getString(DEVICE, "");
        if (id != null && id.matches("[a-f0-9-]{16,64}")) return id;
        id = UUID.randomUUID().toString();
        // commit: register() may immediately send this id to the server from another thread.
        if (!p.edit().putString(DEVICE, id).commit()) throw new IllegalStateException("could not store device id");
        return id;
    }

    private static SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        KeyStore.Entry existing = ks.getEntry(ALIAS, null);
        if (existing instanceof KeyStore.SecretKeyEntry) {
            return ((KeyStore.SecretKeyEntry) existing).getSecretKey();
        }
        KeyGenerator generator = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(
                ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                // A background notification transport must reconnect while the screen is locked.
                .setUserAuthenticationRequired(false)
                .build());
        return generator.generateKey();
    }

    static void save(Context context, String socketUrl, String token, String deviceId) throws Exception {
        JSONObject clear = new JSONObject();
        clear.put("socket", socketUrl);
        clear.put("token", token);
        clear.put("device", deviceId);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] ciphertext = cipher.doFinal(clear.toString().getBytes("UTF-8"));
        byte[] iv = cipher.getIV();
        byte[] blob = new byte[iv.length + ciphertext.length];
        System.arraycopy(iv, 0, blob, 0, iv.length);
        System.arraycopy(ciphertext, 0, blob, iv.length, ciphertext.length);
        boolean ok = prefs(context).edit()
                .putString(DEVICE, deviceId)
                .putString(SEALED, Base64.encodeToString(blob, Base64.NO_WRAP))
                .commit();
        if (!ok) throw new IllegalStateException("could not store direct notification credentials");
    }

    static Credentials load(Context context) {
        try {
            String encoded = prefs(context).getString(SEALED, "");
            if (encoded == null || encoded.isEmpty()) return null;
            byte[] blob = Base64.decode(encoded, Base64.NO_WRAP);
            if (blob.length <= IV_BYTES) return null;
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(),
                    new GCMParameterSpec(128, blob, 0, IV_BYTES));
            byte[] clear = cipher.doFinal(blob, IV_BYTES, blob.length - IV_BYTES);
            JSONObject json = new JSONObject(new String(clear, "UTF-8"));
            String socket = json.optString("socket", "");
            String token = json.optString("token", "");
            String device = json.optString("device", "");
            if (socket.isEmpty() || token.isEmpty() || device.isEmpty()) return null;
            return new Credentials(socket, token, device);
        } catch (Throwable ignored) {
            // A restored backup has preferences but not the old hardware-backed Keystore key. Treat
            // that as signed out instead of retrying corrupt credentials forever.
            return null;
        }
    }

    /** Forget the secret and endpoint while preserving the stable public device id. */
    static void clear(Context context) {
        prefs(context).edit().remove(SEALED).remove(RECEIPTS).commit();
    }

    /** True only for a server delivery id successfully rendered in an earlier connection/process. */
    static synchronized boolean wasDelivered(Context context, String id) {
        if (id == null || id.isEmpty()) return false;
        return receipts(context).contains(id);
    }

    /** Persist before ACK, so a process death after ACK cannot display the replay a second time. */
    static synchronized boolean markDelivered(Context context, String id) {
        if (id == null || id.isEmpty() || id.length() > 256) return false;
        LinkedHashSet<String> ids = receipts(context);
        ids.remove(id);
        ids.add(id);
        while (ids.size() > MAX_RECEIPTS) ids.remove(ids.iterator().next());
        JSONArray encoded = new JSONArray();
        for (String value : ids) encoded.put(value);
        return prefs(context).edit().putString(RECEIPTS, encoded.toString()).commit();
    }

    private static LinkedHashSet<String> receipts(Context context) {
        LinkedHashSet<String> out = new LinkedHashSet<>();
        try {
            JSONArray encoded = new JSONArray(prefs(context).getString(RECEIPTS, "[]"));
            int start = Math.max(0, encoded.length() - MAX_RECEIPTS);
            for (int i = start; i < encoded.length(); i++) {
                String id = encoded.optString(i, "");
                if (!id.isEmpty() && id.length() <= 256) out.add(id);
            }
        } catch (Throwable ignored) { }
        return out;
    }
}
