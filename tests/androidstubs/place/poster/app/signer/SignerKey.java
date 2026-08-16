package place.poster.app.signer;

import android.content.Context;

/** Stand-in for the Keystore-sealed account key: the sweep only ever asks whether it exists and
 *  loads it. Signatures copied from the real class. */
public final class SignerKey {
  private SignerKey() { }

  public static boolean have(Context ctx) { return false; }
  public static byte[] load(Context ctx) { return null; }
  public static String pubkey(Context ctx) { return ""; }
}
