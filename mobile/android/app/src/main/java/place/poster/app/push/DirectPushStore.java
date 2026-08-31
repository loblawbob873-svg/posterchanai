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
