package androidx.core.graphics.drawable;

/**
 * Tinting a drawable the documented way. `setColorFilter` is what it replaces; on a VectorDrawable
 * that already carries a tint, the plain call is the combination that renders nothing.
 */
public final class DrawableCompat {
  private DrawableCompat() { }
  public static android.graphics.drawable.Drawable wrap(android.graphics.drawable.Drawable d) { return d; }
  public static void setTint(android.graphics.drawable.Drawable d, int color) { }
}
