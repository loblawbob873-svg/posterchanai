package place.poster.app.signer;

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * secp256k1 + BIP-340, in plain Java, so this app can sign for OTHER apps without a WebView.
 *
 * WHY THIS EXISTS AT ALL. Signing used to happen in the WebView, because that is where the key was.
 * That makes us a signer only while a browser engine is resident — which Android takes away the
 * moment the app is backgrounded, and which costs a foreground service and its battery to prevent.
 * A NIP-55 request from another app arrives as an Intent: the OS starts us, we answer, we exit. That
 * is zero cost when idle, and it is the whole reason to do the maths here instead of there.
 *
 * DELIBERATELY FREE OF ANDROID IMPORTS. Nothing in this file touches the framework, so `javac` can
 * compile it on a build machine and `java` can run it — which is what
 * `tests/test_android_nip55_signer.py` does, cross-checking every operation against
 * `app/services/nostr/bip340.py`, the implementation this repo has been signing with all along.
 * Hand-written crypto that has never been compared to a reference is how you ship a key-destroying
 * bug; byte-equality against a known-good implementation is the only test worth having here, and
 * BIP-340 is deterministic given the aux, so byte-equality is achievable rather than approximate.
 *
 * A PORT, NOT A DESIGN. The structure mirrors bip340.py line for line on purpose — same tagged
 * hashes, same even-y normalisation, same nonce derivation — so the two can be diffed by eye when
 * one of them changes.
 */
public final class Nostr {

    private Nostr() { }

    public static final BigInteger P = new BigInteger(
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F", 16);
    public static final BigInteger N = new BigInteger(
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16);
    private static final BigInteger GX = new BigInteger(
            "79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798", 16);
    private static final BigInteger GY = new BigInteger(
            "483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8", 16);

    /** Affine point, or null for the point at infinity. */
    private static final class Pt {
        final BigInteger x, y;
        Pt(BigInteger x, BigInteger y) { this.x = x; this.y = y; }
    }

    /* NOT `BigInteger.TWO`. That constant exists in the JDK from Java 9 and on ANDROID only from
     * API 31 — this app's minSdk is 23. It compiles happily against compileSdk 35 and then throws
     * NoSuchFieldError on every phone older than Android 12, at the first signature. The unit tests
     * here run on a desktop JDK, so they cannot see it either: a green CI build and a dead signer on
     * a third of devices. Anything referenced from this package has to exist at API 23. */
    private static final BigInteger TWO = BigInteger.valueOf(2);

    private static final Pt G = new Pt(GX, GY);

    private static Pt add(Pt p1, Pt p2) {
        if (p1 == null) return p2;
        if (p2 == null) return p1;
        if (p1.x.equals(p2.x) && !p1.y.equals(p2.y)) return null;      // P + (-P)
        BigInteger lam;
        if (p1.x.equals(p2.x) && p1.y.equals(p2.y)) {
            lam = BigInteger.valueOf(3).multiply(p1.x).multiply(p1.x)
                    .multiply(TWO.multiply(p1.y).modPow(P.subtract(TWO), P))
                    .mod(P);
        } else {
            lam = p2.y.subtract(p1.y)
                    .multiply(p2.x.subtract(p1.x).modPow(P.subtract(TWO), P)).mod(P);
        }
        BigInteger x3 = lam.multiply(lam).subtract(p1.x).subtract(p2.x).mod(P);
        BigInteger y3 = lam.multiply(p1.x.subtract(x3)).subtract(p1.y).mod(P);
        return new Pt(x3, y3);
    }

    private static Pt mul(Pt p, BigInteger d) {
        Pt r = null;
        for (int i = 0; i < 256; i++) {
            if (d.testBit(i)) r = add(r, p);
            p = add(p, p);
        }
        return r;
    }

    private static boolean evenY(Pt p) { return !p.y.testBit(0); }

    private static byte[] be32(BigInteger v) {
        byte[] raw = v.mod(P.max(N)).toByteArray();   // only used for values < 2^256
        byte[] out = new byte[32];
        int len = Math.min(32, raw.length);
        System.arraycopy(raw, raw.length - len, out, 32 - len, len);
        return out;
    }

    private static BigInteger uint(byte[] b) { return new BigInteger(1, b); }

    public static byte[] sha256(byte[]... parts) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            for (byte[] p : parts) md.update(p);
            return md.digest();
        } catch (Exception e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    private static byte[] tagged(String tag, byte[]... parts) {
        byte[] t = sha256(tag.getBytes(StandardCharsets.UTF_8));
        byte[][] all = new byte[parts.length + 2][];
        all[0] = t; all[1] = t;
        System.arraycopy(parts, 0, all, 2, parts.length);
        return sha256(all);
    }

    /** x-only public key for a 32-byte secret. */
    public static byte[] pubkey(byte[] sec) {
        BigInteger d = uint(sec);
        // The range check stays HERE, ahead of the fast path: a bad seckey must throw the same way
        // whichever implementation answers, and libsecp256k1 reports it as a null rather than an
        // exception — which would turn "bad seckey" into a silent fall-through to this same code.
        if (d.signum() <= 0 || d.compareTo(N) >= 0) throw new IllegalArgumentException("bad seckey");
        byte[] fast = Native.pubkey(sec);
        if (fast != null) return fast;
        return be32(mul(G, d).x);
    }

    /** BIP-340 signature. `aux` may be null for random; pass 32 bytes to make it deterministic. */
    public static byte[] sign(byte[] msg32, byte[] sec, byte[] aux) {
        if (aux == null) { aux = new byte[32]; new SecureRandom().nextBytes(aux); }
        /* libsecp256k1 if this phone has it — 36ms of BigInteger against about 50 microseconds of C,
         * twice per NIP-46 request. `Native` proves itself against the code below before it is
         * trusted and answers null whenever it cannot, so this stays the implementation of record
         * (and the only one the javac/java cross-check against Python ever sees). */
        byte[] fast = Native.sign(msg32, sec, aux);
        if (fast != null) return fast;
        BigInteger d0 = uint(sec);
        if (d0.signum() <= 0 || d0.compareTo(N) >= 0) throw new IllegalArgumentException("bad seckey");
        Pt pp = mul(G, d0);
        BigInteger d = evenY(pp) ? d0 : N.subtract(d0);
        byte[] t = be32(d.xor(uint(tagged("BIP0340/aux", aux))));
        byte[] rand = tagged("BIP0340/nonce", t, be32(pp.x), msg32);
        BigInteger k0 = uint(rand).mod(N);
        if (k0.signum() == 0) throw new IllegalStateException("nonce generation failed");
        Pt r = mul(G, k0);
        BigInteger k = evenY(r) ? k0 : N.subtract(k0);
        BigInteger e = uint(tagged("BIP0340/challenge", be32(r.x), be32(pp.x), msg32)).mod(N);
        byte[] sig = new byte[64];
        System.arraycopy(be32(r.x), 0, sig, 0, 32);
        System.arraycopy(be32(k.add(e.multiply(d)).mod(N)), 0, sig, 32, 32);
        // The reference verifies its own output before returning, and so does this: two
        // implementations disagreeing must not be discoverable only by a relay rejecting the event.
        if (!verify(msg32, be32(pp.x), sig)) throw new IllegalStateException("bad signature produced");
        return sig;
    }

    private static Pt liftX(BigInteger x) {
        if (x.compareTo(P) >= 0) return null;
        BigInteger c = x.modPow(BigInteger.valueOf(3), P).add(BigInteger.valueOf(7)).mod(P);
        BigInteger y = c.modPow(P.add(BigInteger.ONE).divide(BigInteger.valueOf(4)), P);
        if (!y.modPow(TWO, P).equals(c)) return null;
        return new Pt(x, y.testBit(0) ? P.subtract(y) : y);
    }

    public static boolean verify(byte[] msg32, byte[] pub32, byte[] sig64) {
        try {
            Pt pp = liftX(uint(pub32));
            if (pp == null) return false;
            BigInteger r = uint(java.util.Arrays.copyOfRange(sig64, 0, 32));
            BigInteger s = uint(java.util.Arrays.copyOfRange(sig64, 32, 64));
            if (r.compareTo(P) >= 0 || s.compareTo(N) >= 0) return false;
            BigInteger e = uint(tagged("BIP0340/challenge", java.util.Arrays.copyOfRange(sig64, 0, 32),
                                       pub32, msg32)).mod(N);
            Pt rr = add(mul(G, s), mul(pp, N.subtract(e)));
            return rr != null && evenY(rr) && rr.x.equals(r);
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * ECDH as Nostr uses it: the shared point's X coordinate, unhashed.
     *
     * NIP-04 uses these 32 bytes directly as an AES key; NIP-44 runs them through HKDF first. Not
     * the same as standard ECDH, which hashes — getting this wrong produces ciphertext that no other
     * client can read, which is a silent interoperability failure rather than a crash.
     */
    public static byte[] sharedX(byte[] sec, byte[] pub32) {
        Pt pp = liftX(uint(pub32));
        if (pp == null) throw new IllegalArgumentException("bad peer pubkey");
        return be32(mul(pp, uint(sec)).x);
    }

    /**
     * NIP-01 serialization and the event id.
     *
     * WRITTEN BY HAND RATHER THAN THROUGH org.json, and that is a correctness requirement, not a
     * preference. The id is the sha256 of an exact byte string, so any difference in escaping
     * changes it — and Android's `JSONObject.toString()` escapes a forward slash as `\/`, which is
     * legal JSON and a DIFFERENT id. Every event would carry a valid signature over the wrong id and
     * be rejected by relays that check, or accepted and unverifiable by ones that do not.
     *
     * `tagsJson` is passed through verbatim because it arrived as JSON from the caller and is already
     * exactly what they want signed; `content` is escaped here, to the minimal set NIP-01 names.
     */
    public static String escape(String s) {
        StringBuilder b = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                case '\b': b.append("\\b"); break;
                case '\f': b.append("\\f"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.toString();
    }

    public static String serialize(String pubHex, long createdAt, int kind, String tagsJson,
                                   String content) {
        return "[0,\"" + pubHex + "\"," + createdAt + "," + kind + ","
                + (tagsJson == null || tagsJson.isEmpty() ? "[]" : tagsJson)
                + ",\"" + escape(content) + "\"]";
    }

    public static String eventId(String pubHex, long createdAt, int kind, String tagsJson,
                                 String content) {
        try {
            return hex(sha256(serialize(pubHex, createdAt, kind, tagsJson, content)
                    .getBytes("UTF-8")));
        } catch (Exception e) {
            throw new IllegalStateException("UTF-8 unavailable", e);
        }
    }

    // ---- hex, because every Nostr wire format is hex ------------------------------------------
    private static final char[] HEX = "0123456789abcdef".toCharArray();

    public static String hex(byte[] b) {
        char[] out = new char[b.length * 2];
        for (int i = 0; i < b.length; i++) {
            out[i * 2] = HEX[(b[i] >> 4) & 0xf];
            out[i * 2 + 1] = HEX[b[i] & 0xf];
        }
        return new String(out);
    }

    public static byte[] unhex(String s) {
        int n = s.length() / 2;
        byte[] out = new byte[n];
        for (int i = 0; i < n; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }
}
