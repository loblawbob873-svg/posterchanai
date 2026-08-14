package place.poster.app.signer;

import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * NIP-04 and NIP-44 v2, in plain Java, ported from `app/services/nostr/nip44.py` and `nip04.py`.
 *
 * ANDROID-FREE ON PURPOSE, like {@link Nostr}: `tests/test_android_nip55_signer.py` compiles this
 * with `javac` and checks every operation against the Python this repo already encrypts with. That
 * matters more here than anywhere else in the app — a message encryption that is subtly wrong does
 * not crash, it produces ciphertext no other client can read, and you find out from someone else's
 * empty DM window.
 *
 * CHACHA20 IS IMPLEMENTED HERE RATHER THAN TAKEN FROM javax.crypto. Android only exposes ChaCha20 as
 * a JCE cipher from API 28, this app supports 23, and a signer that silently cannot do NIP-44 on
 * older phones is exactly the kind of partial failure that gets discovered by a user rather than by
 * us. It is forty lines of well-specified arithmetic (RFC 8439) and it is checked against the
 * reference like everything else here.
 */
public final class Crypt {

    private Crypt() { }

    // ---------------------------------------------------------------- NIP-04 (legacy, still used)
    /** AES-256-CBC with the RAW ECDH shared-X as the key — not hashed. See nip04.py. */
    public static String nip04Encrypt(byte[] sec, byte[] peer, String text) throws Exception {
        byte[] key = Nostr.sharedX(sec, peer);
        byte[] iv = new byte[16];
        new SecureRandom().nextBytes(iv);
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv));
        byte[] ct = c.doFinal(text.getBytes(StandardCharsets.UTF_8));
        return b64(ct) + "?iv=" + b64(iv);
    }

    public static String nip04Decrypt(byte[] sec, byte[] peer, String payload) throws Exception {
        int at = payload.indexOf("?iv=");
        if (at < 0) throw new IllegalArgumentException("not a NIP-04 payload");
        byte[] ct = unb64(payload.substring(0, at));
        byte[] iv = unb64(payload.substring(at + 4));
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(Nostr.sharedX(sec, peer), "AES"),
               new IvParameterSpec(iv));
        return new String(c.doFinal(ct), StandardCharsets.UTF_8);
    }

    // ---------------------------------------------------------------------------- NIP-44 v2
    public static byte[] conversationKey(byte[] sec, byte[] peer) throws Exception {
        return hmac("nip44-v2".getBytes(StandardCharsets.UTF_8), Nostr.sharedX(sec, peer));
    }

    private static byte[] hkdfExpand(byte[] prk, byte[] info, int length) throws Exception {
        byte[] out = new byte[length];
        byte[] t = new byte[0];
        int pos = 0, i = 0;
        while (pos < length) {
            i++;
            byte[] in = new byte[t.length + info.length + 1];
            System.arraycopy(t, 0, in, 0, t.length);
            System.arraycopy(info, 0, in, t.length, info.length);
            in[in.length - 1] = (byte) i;
            t = hmac(prk, in);
            int n = Math.min(t.length, length - pos);
            System.arraycopy(t, 0, out, pos, n);
            pos += n;
        }
        return out;
    }

    /** {chachaKey(32), chachaNonce(12), hmacKey(32)} */
    private static byte[][] messageKeys(byte[] convKey, byte[] nonce) throws Exception {
        byte[] k = hkdfExpand(convKey, nonce, 76);
        return new byte[][] { Arrays.copyOfRange(k, 0, 32), Arrays.copyOfRange(k, 32, 44),
                              Arrays.copyOfRange(k, 44, 76) };
    }

    static int paddedLen(int unpadded) {
        if (unpadded <= 32) return 32;
        int nextPower = Integer.highestOneBit(unpadded - 1) << 1;
        int chunk = nextPower <= 256 ? 32 : nextPower / 8;
        return chunk * ((unpadded - 1) / chunk + 1);
    }

    public static String nip44Encrypt(byte[] convKey, String text, byte[] nonce) throws Exception {
        if (nonce == null) { nonce = new byte[32]; new SecureRandom().nextBytes(nonce); }
        byte[] pt = text.getBytes(StandardCharsets.UTF_8);
        if (pt.length < 1 || pt.length > 65535) throw new IllegalArgumentException("bad length");
        byte[] padded = new byte[2 + paddedLen(pt.length)];
        padded[0] = (byte) ((pt.length >> 8) & 0xff);
        padded[1] = (byte) (pt.length & 0xff);
        System.arraycopy(pt, 0, padded, 2, pt.length);
        byte[][] mk = messageKeys(convKey, nonce);
        byte[] ct = chacha20(mk[0], mk[1], padded);
        byte[] aad = new byte[nonce.length + ct.length];
        System.arraycopy(nonce, 0, aad, 0, nonce.length);
        System.arraycopy(ct, 0, aad, nonce.length, ct.length);
        byte[] mac = hmac(mk[2], aad);
        byte[] out = new byte[1 + 32 + ct.length + 32];
        out[0] = 2;
        System.arraycopy(nonce, 0, out, 1, 32);
        System.arraycopy(ct, 0, out, 33, ct.length);
        System.arraycopy(mac, 0, out, 33 + ct.length, 32);
        return b64(out);
    }

    public static String nip44Decrypt(byte[] convKey, String payloadB64) throws Exception {
        byte[] raw = unb64(payloadB64);
        if (raw.length < 99 || raw[0] != 2) throw new IllegalArgumentException("unsupported version");
        byte[] nonce = Arrays.copyOfRange(raw, 1, 33);
        byte[] ct = Arrays.copyOfRange(raw, 33, raw.length - 32);
        byte[] mac = Arrays.copyOfRange(raw, raw.length - 32, raw.length);
        byte[][] mk = messageKeys(convKey, nonce);
        byte[] aad = new byte[nonce.length + ct.length];
        System.arraycopy(nonce, 0, aad, 0, nonce.length);
        System.arraycopy(ct, 0, aad, nonce.length, ct.length);
        // Constant-time: a MAC compared with early exit leaks where it first differed, which is the
        // classic way a forgery becomes reachable by measuring.
        if (!MessageDigestEquals(hmac(mk[2], aad), mac)) throw new IllegalArgumentException("bad MAC");
        byte[] padded = chacha20(mk[0], mk[1], ct);
        int n = ((padded[0] & 0xff) << 8) | (padded[1] & 0xff);
        if (n < 1 || padded.length < 2 + n) throw new IllegalArgumentException("invalid padding");
        return new String(Arrays.copyOfRange(padded, 2, 2 + n), StandardCharsets.UTF_8);
    }

    private static boolean MessageDigestEquals(byte[] a, byte[] b) {
        if (a.length != b.length) return false;
        int d = 0;
        for (int i = 0; i < a.length; i++) d |= a[i] ^ b[i];
        return d == 0;
    }

    // ------------------------------------------------------------------- ChaCha20 (RFC 8439)
    private static int rotl(int v, int c) { return (v << c) | (v >>> (32 - c)); }

    private static void quarter(int[] s, int a, int b, int c, int d) {
        s[a] += s[b]; s[d] = rotl(s[d] ^ s[a], 16);
        s[c] += s[d]; s[b] = rotl(s[b] ^ s[c], 12);
        s[a] += s[b]; s[d] = rotl(s[d] ^ s[a], 8);
        s[c] += s[d]; s[b] = rotl(s[b] ^ s[c], 7);
    }

    private static int le(byte[] b, int i) {
        return (b[i] & 0xff) | ((b[i + 1] & 0xff) << 8) | ((b[i + 2] & 0xff) << 16)
                | ((b[i + 3] & 0xff) << 24);
    }

    /** Counter starts at 0, matching the reference's 4-byte LE counter || 96-bit nonce. */
    static byte[] chacha20(byte[] key, byte[] nonce12, byte[] data) {
        byte[] out = new byte[data.length];
        int[] st = new int[16];
        st[0] = 0x61707865; st[1] = 0x3320646e; st[2] = 0x79622d32; st[3] = 0x6b206574;
        for (int i = 0; i < 8; i++) st[4 + i] = le(key, i * 4);
        for (int i = 0; i < 3; i++) st[13 + i] = le(nonce12, i * 4);
        int counter = 0;
        for (int off = 0; off < data.length; off += 64) {
            st[12] = counter++;
            int[] w = st.clone();
            for (int r = 0; r < 10; r++) {
                quarter(w, 0, 4, 8, 12); quarter(w, 1, 5, 9, 13);
                quarter(w, 2, 6, 10, 14); quarter(w, 3, 7, 11, 15);
                quarter(w, 0, 5, 10, 15); quarter(w, 1, 6, 11, 12);
                quarter(w, 2, 7, 8, 13); quarter(w, 3, 4, 9, 14);
            }
            for (int i = 0; i < 16; i++) w[i] += st[i];
            int n = Math.min(64, data.length - off);
            for (int i = 0; i < n; i++) {
                out[off + i] = (byte) (data[off + i] ^ (w[i >> 2] >>> ((i & 3) * 8)));
            }
        }
        return out;
    }

    // ------------------------------------------------------------------------------ helpers
    static byte[] hmac(byte[] key, byte[] data) throws Exception {
        Mac m = Mac.getInstance("HmacSHA256");
        m.init(new SecretKeySpec(key, "HmacSHA256"));
        return m.doFinal(data);
    }

    /* java.util.Base64 is API 26+ and this app supports 23, while android.util.Base64 cannot be
     * compiled or tested off-device. So: a small encoder, which also keeps this file Android-free. */
    private static final String B64 =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    static String b64(byte[] in) {
        StringBuilder sb = new StringBuilder(((in.length + 2) / 3) * 4);
        for (int i = 0; i < in.length; i += 3) {
            int b = (in[i] & 0xff) << 16;
            if (i + 1 < in.length) b |= (in[i + 1] & 0xff) << 8;
            if (i + 2 < in.length) b |= (in[i + 2] & 0xff);
            sb.append(B64.charAt((b >> 18) & 0x3f)).append(B64.charAt((b >> 12) & 0x3f));
            sb.append(i + 1 < in.length ? B64.charAt((b >> 6) & 0x3f) : '=');
            sb.append(i + 2 < in.length ? B64.charAt(b & 0x3f) : '=');
        }
        return sb.toString();
    }

    static byte[] unb64(String s) {
        s = s.trim().replace("=", "");
        int n = s.length() * 6 / 8;
        byte[] out = new byte[n];
        int buf = 0, bits = 0, pos = 0;
        for (int i = 0; i < s.length(); i++) {
            int v = B64.indexOf(s.charAt(i));
            if (v < 0) continue;
            buf = (buf << 6) | v; bits += 6;
            if (bits >= 8) { bits -= 8; out[pos++] = (byte) ((buf >> bits) & 0xff); }
        }
        return pos == n ? out : Arrays.copyOf(out, pos);
    }
}
