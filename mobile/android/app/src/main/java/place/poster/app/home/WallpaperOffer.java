package place.poster.app.home;

/**
 * THE POSTERCHAN LIVE WALLPAPER IS THE LAUNCHER'S DEFAULT -- as far as Android lets it be one.
 *
 * "if you enable the poster chan launcher, can you make that live wallpaper the default? They can change
 * the wallpaper later of course". Android lets no ordinary app SET a live wallpaper: that is
 * WallpaperManager.setWallpaperComponent, guarded by SET_WALLPAPER_COMPONENT, a signature|privileged
 * permission. What an app can do is open the system's own preview of it, where applying is one tap. So the
 * default is that preview, offered ONCE: the first time the launcher is the home screen while some other
 * wallpaper is on it. Once means once -- dismissed, or changed later to a photo, it is never offered again;
 * changing it back is ⋯ → Set wallpaper. Pure, so the rule is run off-device.
 */
public final class WallpaperOffer {
    private WallpaperOffer() { }

    public static boolean shouldOffer(boolean isDefaultHome, boolean oursIsActive, boolean alreadyOffered) {
        return isDefaultHome && !oursIsActive && !alreadyOffered;
    }
}
