package android.graphics;

import java.io.OutputStream;

public final class Bitmap {
  public enum CompressFormat { JPEG, PNG, WEBP }
  public enum Config { ALPHA_8, RGB_565, ARGB_8888 }
  public static Bitmap createBitmap(int width, int height, Config config) { return new Bitmap(); }
  public void setPixel(int x, int y, int color) {}
  public boolean compress(CompressFormat format, int quality, OutputStream out) { return true; }
  public void recycle() {}
  public int getWidth() { return 0; }
  public int getHeight() { return 0; }
}
