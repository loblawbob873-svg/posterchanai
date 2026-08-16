package place.poster.app.signer;

import android.content.Context;

/** Stand-in for the Keystore-sealed account key: the sweep only ever asks whether it exists and
 *  loads it. Signatures copied from the real class. */
public final class SignerKey {
  private SignerKey() { }

  /** Settable, so a test can drive the paths that only exist for an account whose key IS here. */
  public static boolean HAVE = false;
  public static boolean have(Context ctx) { return HAVE; }
  public static byte[] load(Context ctx) { return null; }
  public static String pubkey(Context ctx) { return ""; }
}
