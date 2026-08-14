package place.poster.app.signer;

/**
 * bech32, for one job: turning a 32-byte public key into an `npub1…`.
 *
 * WHY IT EXISTS, and it is a bug report rather than a feature. NIP-55's `get_public_key` answers with
 * the npub — that is what Amber returns and what clients therefore parse. This app's signer returned
 * raw HEX, so a client did `nip19.decode()` on it, and since hex is full of `b` and bech32's alphabet
 * has no `b`, the app reported:
 *
 *     unknown letter "b"
 *
 * Which names the symptom perfectly and points nowhere near the cause. It is also the reason this
 * app's OWN client never noticed: `Nip55.getPublicKey` accepts hex or npub (`/^npub1/i.test(v) ?
 * decode : v`), so our signer talking to our client worked, and only a third-party app could see it.
 * Its own comment already documented the contract as "npub for get_public_key" — the client was
 * right and the signer was wrong.
 *
 * ENCODE ONLY. Nothing here needs to read an npub, and a decoder is where the checksum-validation
 * subtleties live; there is no reason to carry that risk for an unused direction. Verified against
 * `app/services/nostr/bech32.py`, which is a separate implementation in another language — the
 * checksum is the part that is silently wrong when it is wrong, since a bad one still LOOKS like an
 * npub and only fails in whatever app you hand it to.
 */
public final class Bech32 {

    private static final String CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

    private Bech32() { }

    /** `npub1…` for a 32-byte x-only public key. */
    public static String npub(byte[] pubkey32) {
        if (pubkey32 == null || pubkey32.length != 32) {
            throw new IllegalArgumentException("a public key is 32 bytes");
        }
        return encode("npub", convert8to5(pubkey32));
    }

    /** Regroup 8-bit bytes into the 5-bit values bech32 encodes, padding the last group with zeros. */
    static byte[] convert8to5(byte[] data) {
        int acc = 0, bits = 0;
        byte[] out = new byte[(data.length * 8 + 4) / 5];
        int n = 0;
        for (byte b : data) {
            acc = (acc << 8) | (b & 0xff);
            bits += 8;
            while (bits >= 5) {
                bits -= 5;
                out[n++] = (byte) ((acc >> bits) & 31);
            }
        }
        if (bits > 0) out[n++] = (byte) ((acc << (5 - bits)) & 31);
        if (n != out.length) {                      // cannot happen; a wrong length is a silent corruption
            byte[] cut = new byte[n];
            System.arraycopy(out, 0, cut, 0, n);
            return cut;
        }
        return out;
    }

    static String encode(String hrp, byte[] data5) {
        byte[] sum = checksum(hrp, data5);
        StringBuilder sb = new StringBuilder(hrp).append('1');
        for (byte b : data5) sb.append(CHARSET.charAt(b & 31));
        for (byte b : sum) sb.append(CHARSET.charAt(b & 31));
        return sb.toString();
    }

    private static byte[] checksum(String hrp, byte[] data5) {
        byte[] expanded = hrpExpand(hrp);
        byte[] values = new byte[expanded.length + data5.length + 6];
        System.arraycopy(expanded, 0, values, 0, expanded.length);
        System.arraycopy(data5, 0, values, expanded.length, data5.length);
        int poly = polymod(values) ^ 1;             // bech32 (not bech32m, which xors with 0x2bc830a3)
        byte[] out = new byte[6];
        for (int i = 0; i < 6; i++) out[i] = (byte) ((poly >> (5 * (5 - i))) & 31);
        return out;
    }

    private static byte[] hrpExpand(String hrp) {
        int n = hrp.length();
        byte[] out = new byte[n * 2 + 1];
        for (int i = 0; i < n; i++) out[i] = (byte) (hrp.charAt(i) >> 5);
        out[n] = 0;
        for (int i = 0; i < n; i++) out[n + 1 + i] = (byte) (hrp.charAt(i) & 31);
        return out;
    }

    private static int polymod(byte[] values) {
        final int[] GEN = {0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3};
        int chk = 1;
        for (byte v : values) {
            int top = chk >>> 25;
            chk = ((chk & 0x1ffffff) << 5) ^ (v & 31);
            for (int i = 0; i < 5; i++) if (((top >> i) & 1) != 0) chk ^= GEN[i];
        }
        return chk;
    }
}
