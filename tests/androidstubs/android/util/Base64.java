package android.util;

/**
 * FUNCTIONAL, unlike most of these stubs, and deliberately so.
 *
 * The rest of tests/androidstubs is signature-only: it exists so javac can type-check code that
 * touches the platform, and anything that would actually CALL the platform cannot run here. Base64
 * is different because it is not a platform service at all — it is pure byte manipulation with an
 * exact, published answer, and it sits underneath every piece of the signer's cryptography
 * (`Crypt.nip44Encrypt`, `nip04Encrypt`, the payload framing). Left inert it returned null, so the
 * only thing a test could do with the real NIP-44 code was compile it.
 *
 * Delegating to java.util.Base64 makes the signer's crypto RUNNABLE off-device, which is the whole
 * point: a wrong conversation key or a mis-framed payload produces a perfectly well-formed event
 * that the other end silently cannot read — the exact failure that had a paired app showing up in
 * the signer list while the site never logged in. Now it is checked against known vectors instead.
 *
 * The flags that matter here are modelled honestly: NO_WRAP is java.util.Base64's own behaviour,
 * DEFAULT adds the trailing newline and 76-column wrapping Android's does, and URL_SAFE swaps the
 * alphabet. Nothing else is implemented — a flag this file does not know about would silently
 * behave like DEFAULT, so it throws instead.
 */
public final class Base64 {

  public static final int DEFAULT = 0;
  public static final int NO_PADDING = 1;
  public static final int NO_WRAP = 2;
  public static final int CRLF = 4;
  public static final int URL_SAFE = 8;

  private Base64() { }

  public static byte[] decode(String str, int flags) {
    if (str == null) return null;
    // Android is lenient about wrapping on the way IN whatever the flags say, so strip it here
    // rather than making every caller pass the flag its producer happened to use.
    String s = str.replaceAll("[\\r\\n]", "");
    java.util.Base64.Decoder d = (flags & URL_SAFE) != 0
        ? java.util.Base64.getUrlDecoder() : java.util.Base64.getDecoder();
    return d.decode(s);
  }

  public static byte[] decode(byte[] input, int flags) {
    return decode(new String(input, java.nio.charset.StandardCharsets.US_ASCII), flags);
  }

  public static String encodeToString(byte[] input, int flags) {
    if (input == null) return null;
    if ((flags & ~(NO_PADDING | NO_WRAP | CRLF | URL_SAFE)) != 0) {
      throw new UnsupportedOperationException("unmodelled Base64 flag: " + flags);
    }
    java.util.Base64.Encoder e = (flags & URL_SAFE) != 0
        ? java.util.Base64.getUrlEncoder() : java.util.Base64.getEncoder();
    if ((flags & NO_PADDING) != 0) e = e.withoutPadding();
    String out = e.encodeToString(input);
    if ((flags & NO_WRAP) != 0) return out;
    // Android's DEFAULT wraps at 76 columns and ends with a line break.
    String nl = (flags & CRLF) != 0 ? "\r\n" : "\n";
    StringBuilder sb = new StringBuilder();
    for (int i = 0; i < out.length(); i += 76) {
      sb.append(out, i, Math.min(i + 76, out.length())).append(nl);
    }
    return sb.toString();
  }

  public static byte[] encode(byte[] input, int flags) {
    return encodeToString(input, flags).getBytes(java.nio.charset.StandardCharsets.US_ASCII);
  }
}
