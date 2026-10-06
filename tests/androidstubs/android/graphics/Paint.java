package android.graphics;

/** Signature-only. */
public class Paint {
  public static final int ANTI_ALIAS_FLAG = 1;
  public enum Style { FILL, STROKE, FILL_AND_STROKE }
  public enum Align { LEFT, CENTER, RIGHT }
  public Paint() {}
  public Paint(int flags) {}
  public void setStyle(Style style) {}
  public Shader setShader(Shader shader) { return shader; }
  public void setColor(int color) {}
  public void setStrokeWidth(float width) {}
  public void setShadowLayer(float radius, float dx, float dy, int shadowColor) {}
  public void clearShadowLayer() {}
  public Typeface setTypeface(Typeface typeface) { return typeface; }
  public void setTextAlign(Align align) {}
  public void setTextSize(float textSize) {}
}
