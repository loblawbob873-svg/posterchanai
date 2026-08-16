package place.poster.app.push;

import android.content.Context;

/**
 * A STAND-IN for the "stay connected" service. Folder sync only reads two facts off it — whether it
 * is running and whether the user asked for it — and it reports them so the background panel can
 * say so; it no longer DEPENDS on either, which is the fix this stub exists alongside.
 *
 * Stubbed rather than compiled because the real class pulls in the media session, the audio device
 * callback and the notification builder — none of which folder sync touches.
 */
public class StayAwakeService {
  public static boolean running = false;
  public static boolean wanted(Context ctx) { return false; }
  public static void setWanted(Context ctx, boolean on) { }
}
