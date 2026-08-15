package place.poster.app.signer;

import java.lang.reflect.Method;

/**
 * libsecp256k1, when the phone has it — the reason Amber is fast and we were not.
 *
 * THE MEASUREMENT. The pure-Java signer in `Nostr` takes 36ms per Schnorr signature on a warmed
 * DESKTOP core (four point multiplications: the pubkey, the nonce, and a self-verify that is over
 * half the cost). libsecp256k1 does the same signature in about 50 MICROseconds. A NIP-46 request
 * needs two signatures and two ECDH, so on a phone that difference is the whole of "this signer is
 * slower than amber to get events published". It was never better code on their side; it is C.
 *
 * REFLECTION, NOT AN IMPORT, and that is load-bearing twice. `Nostr` and `Crypt` are compiled and
 * RUN by tests/test_android_nip55_signer.py under plain javac/java — no Android, no JNI, no aar — so
 * that the shipped crypto can be checked byte-for-byte against the repo's own Python implementation.
 * A compile-time reference to fr.acinq.secp256k1 would break that, and the cross-check is worth more
 * than the tidiness. It also means a build without the dependency, or a phone whose ABI has no
 * native library, degrades to the Java path instead of failing to start.
 *
 * IT PROVES ITSELF BEFORE IT IS TRUSTED. This cannot be tested on this machine, and a signing
 * routine that is subtly wrong does not throw — it produces a signature relays reject, or worse, one
 * that verifies against itself and nothing else. So the first call checks the native answer against
 * the pure-Java implementation that is already known-correct: the derived pubkey must match exactly,
 * and the first native signature must verify under `Nostr.verify`. Either check failing disables
 * native for the life of the process and leaves the Java path in charge. The cost of that safety is
 * one extra verification, once.
 */
final class Native {

    private Native() { }

    /** null = not yet decided, TRUE = proven good, FALSE = unavailable or disagreed with Java. */
    private static volatile Boolean usable = null;
    private static Object ctx;
    private static Class<?> api;
    private static Method mSignSchnorr, mPubkeyCreate, mTweakMul;
    /** ECDH is decided SEPARATELY: a build whose API has moved that one method must not lose signing
     *  too, and vice versa. */
    private static volatile Boolean ecdhUsable = null;
    /** Readable by the panel: why the fast path is off, when it is off. */
    static volatile String why = "";

    private static synchronized boolean ready() {
        if (usable != null) return usable;
        usable = Boolean.FALSE;
        try {
            Class<?> k = Class.forName("fr.acinq.secp256k1.Secp256k1");
            api = k;
            ctx = k.getMethod("get").invoke(null);
            if (ctx == null) { why = "no secp256k1 instance"; return false; }
            mSignSchnorr = k.getMethod("signSchnorr", byte[].class, byte[].class, byte[].class);
            mPubkeyCreate = k.getMethod("pubkeyCreate", byte[].class);

            /* THE PROOF, against the implementation that is already cross-checked with Python.
             * A fixed key, so the comparison is deterministic and cannot pass by luck. */
            byte[] sec = new byte[32];
            for (int i = 0; i < 32; i++) sec[i] = (byte) (i + 1);
            byte[] msg = new byte[32];
            for (int i = 0; i < 32; i++) msg[i] = (byte) (i * 7 + 3);

            byte[] mine = pubkeyRaw(sec);
            byte[] theirs = Nostr.pubkey(sec);
            if (mine == null || theirs == null || !java.util.Arrays.equals(mine, theirs)) {
                why = "native pubkey disagreed with the Java one";
                return false;
            }
            byte[] sig = (byte[]) mSignSchnorr.invoke(ctx, msg, sec, new byte[32]);
            if (sig == null || sig.length != 64 || !Nostr.verify(msg, theirs, sig)) {
                why = "native signature did not verify";
                return false;
            }
            usable = Boolean.TRUE;
            why = "";
        } catch (Throwable t) {
            // No dependency, no native library for this ABI, or a changed API. All the same answer.
            why = String.valueOf(t.getClass().getSimpleName());
            usable = Boolean.FALSE;
        }
        return usable;
    }

    /** x-only pubkey, or null. `pubkeyCreate` answers 65 bytes (0x04 || X || Y); nostr wants X. */
    private static byte[] pubkeyRaw(byte[] sec) {
        try {
            byte[] full = (byte[]) mPubkeyCreate.invoke(ctx, (Object) sec);
            if (full == null || full.length < 33) return null;
            byte[] x = new byte[32];
            System.arraycopy(full, 1, x, 0, 32);
            return x;
        } catch (Throwable t) {
            return null;
        }
    }

    /** The x-only pubkey for `sec`, or null to mean "use the Java path". */
    static byte[] pubkey(byte[] sec) {
        if (!ready()) return null;
        return pubkeyRaw(sec);
    }

    /** A BIP-340 signature, or null to mean "use the Java path". */
    static byte[] sign(byte[] msg32, byte[] sec, byte[] aux) {
        if (!ready()) return null;
        try {
            byte[] sig = (byte[]) mSignSchnorr.invoke(ctx, msg32, sec, aux == null ? new byte[32] : aux);
            return (sig != null && sig.length == 64) ? sig : null;
        } catch (Throwable t) {
            return null;
        }
    }

    /* ---------------------------------------------------------------------------------------------
     * ECDH — the half that was left in BigInteger, and the half a MESSAGE costs.
     *
     * Signing was moved to C and decryption was not, which is a strange place to stop once you look
     * at what each surface actually does. Publishing an event is ONE signature. Restoring a DM
     * history is one ECDH per gift wrap and another per sealed message — two point multiplications
     * per message, none of which the fast path touched. Reported as "messages take forever to
     * decrypt on this signer, worse than amber", which is the same sentence that produced the
     * signing fix, about the same missing C.
     *
     * `pubKeyTweakMul` and not `ecdh`: libsecp256k1's own ecdh() returns SHA256 of the compressed
     * point, and both NIP-04 and NIP-44 want the RAW x coordinate. Multiplying the peer's point by
     * our scalar and taking x is what Nostr.sharedX does in Java, exactly.
     *
     * Proven against that Java implementation before it is trusted, for the reason in the class
     * header: a wrong shared secret does not throw. It produces ciphertext nobody can read and
     * "decrypts" nothing, which on this path would look like a signer that answers with garbage.
     * ------------------------------------------------------------------------------------------ */
    private static synchronized boolean ecdhReady() {
        if (ecdhUsable != null) return ecdhUsable;
        ecdhUsable = Boolean.FALSE;
        if (!ready()) return false;                 // no library at all — `why` already says so
        try {
            mTweakMul = api.getMethod("pubKeyTweakMul", byte[].class, byte[].class);

            byte[] a = new byte[32], b = new byte[32];
            for (int i = 0; i < 32; i++) { a[i] = (byte) (i + 1); b[i] = (byte) (0x40 + i); }
            byte[] peer = Nostr.pubkey(b);
            byte[] mine = sharedRaw(a, peer);
            byte[] theirs = Nostr.sharedX(a, peer);
            if (mine == null || theirs == null || !java.util.Arrays.equals(mine, theirs)) {
                why = "native ECDH disagreed with the Java one";
                return false;
            }
            /* BOTH DIRECTIONS. A shared secret that is only right when this key is the sender is
             * not a shared secret, and every conversation has two ends. */
            byte[] back1 = sharedRaw(b, Nostr.pubkey(a));
            byte[] back2 = Nostr.sharedX(b, Nostr.pubkey(a));
            if (back1 == null || !java.util.Arrays.equals(back1, back2)
                || !java.util.Arrays.equals(back1, mine)) {
                why = "native ECDH is not symmetric";
                return false;
            }
            ecdhUsable = Boolean.TRUE;
        } catch (Throwable t) {
            why = String.valueOf(t.getClass().getSimpleName());
            ecdhUsable = Boolean.FALSE;
        }
        return ecdhUsable;
    }

    /** The x-only pubkey is lifted to the EVEN-Y point, which is what `liftX` gives Java. */
    private static byte[] sharedRaw(byte[] sec, byte[] pub32) {
        try {
            byte[] pk = new byte[33];
            pk[0] = 2;
            System.arraycopy(pub32, 0, pk, 1, 32);
            byte[] out = (byte[]) mTweakMul.invoke(ctx, pk, sec);
            // 0x04||X||Y (65) or 0x02|0x03||X (33) — x starts at 1 either way.
            if (out == null || out.length < 33) return null;
            byte[] x = new byte[32];
            System.arraycopy(out, 1, x, 0, 32);
            return x;
        } catch (Throwable t) {
            return null;
        }
    }

    /** The raw ECDH shared x, or null to mean "use the Java path". */
    static byte[] sharedX(byte[] sec, byte[] pub32) {
        if (!ecdhReady()) return null;
        return sharedRaw(sec, pub32);
    }

    /** True when signatures are being made in C rather than in BigInteger. For the panel. */
    static boolean active() {
        return ready();
    }

    /** True when DECRYPTION is too — the two can differ, so the panel must not report one as both. */
    static boolean ecdhActive() {
        return ecdhReady();
    }
}
