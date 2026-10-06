package android.graphics;

public final class BitmapFactory {
  public static class Options {
    public boolean inJustDecodeBounds;
    public int inSampleSize;
    public boolean inScaled;
    public int outWidth;
    public int outHeight;
  }
  public static Bitmap decodeResource(android.content.res.Resources res, int id, Options opts) { return null; }
  public static Bitmap decodeByteArray(byte[] data, int offset, int length, Options opts) {
    return null;
  }
}
