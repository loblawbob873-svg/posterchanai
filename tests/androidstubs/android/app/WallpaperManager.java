package android.app;

/** Signature-only. */
public class WallpaperManager {
  public static final String ACTION_CHANGE_LIVE_WALLPAPER = "android.service.wallpaper.CHANGE_LIVE_WALLPAPER";
  public static final String ACTION_LIVE_WALLPAPER_CHOOSER = "android.service.wallpaper.LIVE_WALLPAPER_CHOOSER";
  public static final String EXTRA_LIVE_WALLPAPER_COMPONENT = "android.service.wallpaper.extra.LIVE_WALLPAPER_COMPONENT";
  public static final String COMMAND_TAP = "android.wallpaper.tap";
  public static WallpaperManager getInstance(android.content.Context context) { return new WallpaperManager(); }
  public WallpaperInfo getWallpaperInfo() { return null; }
  public void sendWallpaperCommand(android.os.IBinder windowToken, String action, int x, int y, int z, android.os.Bundle extras) {}
}
