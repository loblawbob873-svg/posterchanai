package android.graphics;

/** Signature-only. */
public class Canvas {
  public Canvas() {}
  public Canvas(Bitmap bitmap) {}
  public void drawRect(float left, float top, float right, float bottom, Paint paint) {}
  public void drawCircle(float cx, float cy, float radius, Paint paint) {}
  public void drawLine(float startX, float startY, float stopX, float stopY, Paint paint) {}
  public void drawText(String text, float x, float y, Paint paint) {}
  public void drawOval(RectF oval, Paint paint) {}
  public void drawBitmap(Bitmap bitmap, Rect src, RectF dst, Paint paint) {}
  public int save() { return 0; }
  public void restore() {}
  public void scale(float sx, float sy, float px, float py) {}
  public void drawBitmap(Bitmap bitmap, float left, float top, Paint paint) {}
}
