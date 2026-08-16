package place.poster.app;

import android.app.Notification;
import android.content.Context;

/**
 * A STAND-IN for the app's own shared-notification helper, so the background sweep service can be
 * type-checked without dragging in the signer, the media session and the generated R class.
 *
 * It is here for the same reason the platform stubs are, and it carries the same duty: the
 * SIGNATURES must match the real one. `othersRunning` in particular went from boolean to int when a
 * third service appeared, and a stub still declaring the boolean would let a caller pass `false` and
 * compile — which is precisely the mistake this file exists to catch.
 */
public final class RunningNote {
  public static final int ID = 4712;
  public static final String CHANNEL = "pcai_running";
  public static final int SIGNER = 1, STAY = 2, SYNC = 3;

  private RunningNote() { }

  public static void ensureChannel(Context ctx) { }
  public static String text() { return ""; }
  public static Notification build(Context ctx) { return null; }
  public static void refresh(Context ctx) { }
  public static boolean othersRunning(int me) { return false; }
}
