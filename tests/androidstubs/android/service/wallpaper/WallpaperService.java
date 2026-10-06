package android.service.wallpaper;

/** Signature-only — lets tests/test_android_live_wallpaper.py TYPE-CHECK CyberWallpaper here. */
public abstract class WallpaperService extends android.content.Context {
  public abstract Engine onCreateEngine();
  // The real class is a ContextWrapper; these two satisfy the stub Context's abstract methods.
  public android.content.ContentResolver getContentResolver() { return null; }
  public android.content.SharedPreferences getSharedPreferences(String name, int mode) { return null; }
  public class Engine {
    public void onCreate(android.view.SurfaceHolder holder) {}
    public void onDestroy() {}
    public void onVisibilityChanged(boolean visible) {}
    public void onSurfaceChanged(android.view.SurfaceHolder holder, int format, int width, int height) {}
    public void onSurfaceDestroyed(android.view.SurfaceHolder holder) {}
    public void onOffsetsChanged(float xOffset, float yOffset, float xOffsetStep, float yOffsetStep, int xPixelOffset, int yPixelOffset) {}
    public android.os.Bundle onCommand(String action, int x, int y, int z, android.os.Bundle extras, boolean resultRequested) { return null; }
    public void setOffsetNotificationsEnabled(boolean enabled) {}
    public android.view.SurfaceHolder getSurfaceHolder() { return null; }
  }
}
