package android.graphics;

import java.io.OutputStream;

public final class Bitmap {
  public enum CompressFormat { JPEG, PNG, WEBP }
  public boolean compress(CompressFormat format, int quality, OutputStream out) { return true; }
  public void recycle() {}
  public int getWidth() { return 0; }
  public int getHeight() { return 0; }
}
