package androidx.core.content;

/**
 * The documented way to load a drawable across platform versions.
 *
 * Stubbed to the one member the phone shell uses. `Resources.getDrawable(int)` is what it replaces,
 * and the difference is not cosmetic: for a VectorDrawable the plain call is the one that can
 * silently return or draw nothing on some versions — see Skin.icon.
 */
public final class ContextCompat {
  private ContextCompat() { }
  public static android.graphics.drawable.Drawable getDrawable(android.content.Context c, int res) { return null; }
  public static android.content.ComponentName startForegroundService(
      android.content.Context c, android.content.Intent intent) {
    return c.startForegroundService(intent);
  }
}
