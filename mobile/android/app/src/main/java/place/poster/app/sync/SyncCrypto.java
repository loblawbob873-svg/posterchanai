package place.poster.app.sync;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

import place.poster.app.signer.Crypt;
import place.poster.app.signer.Nostr;

/**
 * The encrypted drive's blob format, in Java — the half of folder sync that has to be byte-identical
 * to the browser's or nothing works.
 *
 * A blob is {@code iv(12) || AES-256-GCM(plaintext)} under the account's drive master key, and THE IV
 * IS DERIVED FROM THE CONTENT: {@code sha256(plaintext)[0:12]}. That is not a shortcut, it is the
 * dedup: the ciphertext — and therefore its sha256, which is its address — is a pure function of the
 * file, so a device can ask "do you already have this?" before sending anything, and two devices that
 * hold the same file compute the same address without ever comparing notes. Get the IV wrong and
 * nothing breaks visibly; every device simply re-uploads its entire folder, for ever.
 *
 * The master key itself is never stored natively. It is generated once in the browser, NIP-44
 * self-wrapped, and kept in the drive index; the phone is handed that WRAPPED value and unwraps it
 * with the Nostr secret the native signer already holds. So this adds no new secret at rest — the
 * thing on disk is unreadable without a key that was already on disk.
 *
 * Android-free on purpose (like {@link Crypt}), so the vectors here are checked against the shipped
 * JavaScript in `tests/test_android_native_sync.py` rather than on a device.
 */
public final class SyncCrypto {

    private SyncCrypto() { }

    public static final int IV_LEN = 12;
    private static final int TAG_BITS = 128;

    public static byte[] sha256(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(data);
        } catch (Exception e) {
            throw new IllegalStateException("no SHA-256", e);
        }
    }

    public static String sha256hex(byte[] data) {
        return Nostr.hex(sha256(data));
    }

    /** sha256(plain)[0:12] — see the class comment for why this must never become a random IV. */
    public static byte[] contentIV(byte[] plain) {
        byte[] h = sha256(plain);
        byte[] iv = new byte[IV_LEN];
        System.arraycopy(h, 0, iv, 0, IV_LEN);
        return iv;
    }

    /** The stored blob for these bytes: deterministic, so its address is knowable without asking. */
    public static byte[] encrypt(byte[] mk, byte[] plain) throws Exception {
        return encrypt(mk, plain, contentIV(plain));
    }

    public static byte[] encrypt(byte[] mk, byte[] plain, byte[] iv) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(mk, "AES"), new GCMParameterSpec(TAG_BITS, iv));
        byte[] ct = c.doFinal(plain);
        byte[] out = new byte[iv.length + ct.length];
        System.arraycopy(iv, 0, out, 0, iv.length);
        System.arraycopy(ct, 0, out, iv.length, ct.length);
        return out;
    }

    public static byte[] decrypt(byte[] mk, byte[] blob) throws Exception {
        if (blob == null || blob.length <= IV_LEN) throw new IllegalArgumentException("not a blob");
        byte[] iv = new byte[IV_LEN];
        System.arraycopy(blob, 0, iv, 0, IV_LEN);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(mk, "AES"), new GCMParameterSpec(TAG_BITS, iv));
        return c.doFinal(blob, IV_LEN, blob.length - IV_LEN);
    }

    /** Where these bytes WOULD be stored, computed without storing them. */
    public static String blobSha(byte[] mk, byte[] plain) throws Exception {
        return sha256hex(encrypt(mk, plain));
    }

    /**
     * The account's drive key out of its NIP-44 self-wrapped form: {@code {"k": "<base64 of 32 bytes>"}}.
     *
     * A WRONG SIZE IS A DIFFERENT FAILURE FROM A REFUSAL, and the browser already draws that line: a
     * value that can never become a key is safe to discard and re-fetch, while a signer that merely
     * did not answer must not cost anyone their key. Here the signer is local and always answers, so
     * what is left is the structural case — and it throws rather than returning a short array that
     * would decrypt nothing and be blamed on the network.
     */
    public static byte[] unwrapMasterKey(byte[] sec, String wrapped) throws Exception {
        if (sec == null || sec.length != 32) throw new IllegalArgumentException("no local key");
        if (wrapped == null || wrapped.isEmpty()) throw new IllegalArgumentException("no wrapped drive key");
        byte[] conv = Crypt.conversationKey(sec, Nostr.pubkey(sec));       // self-wrapped: peer == me
        String raw = Crypt.nip44Decrypt(conv, wrapped);
        Map<String, Object> j = Json.obj(Json.parse(raw));
        byte[] mk = Crypt.unb64(Json.str(j.get("k"), ""));
        if (mk.length != 32) {
            throw new IllegalArgumentException("the drive key unwrapped to " + mk.length
                                               + " bytes, expected 32 — this device cannot read the folder");
        }
        return mk;
    }

    /** NIP-44 to and from yourself — how the manifest's paths travel. */
    public static String sealToSelf(byte[] sec, String plaintext) throws Exception {
        return Crypt.nip44Encrypt(Crypt.conversationKey(sec, Nostr.pubkey(sec)), plaintext, null);
    }

    public static String openFromSelf(byte[] sec, String payload) throws Exception {
        return Crypt.nip44Decrypt(Crypt.conversationKey(sec, Nostr.pubkey(sec)), payload);
    }

    public static byte[] utf8(String s) { return s.getBytes(StandardCharsets.UTF_8); }

    public static String fromUtf8(byte[] b) { return new String(b, StandardCharsets.UTF_8); }
}
