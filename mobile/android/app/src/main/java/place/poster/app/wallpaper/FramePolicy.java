package place.poster.app.wallpaper;

/**
 * When the wallpaper may draw — the battery half of a live wallpaper, kept pure so it is RUN in a test.
 *
 * A wallpaper lives for as long as the phone is on. The PosterChan home screen's standing rule is that
 * nothing it owns ticks while nobody is looking (the HOME role keeps the process resident for the life
 * of the battery), and a live wallpaper is the one thing in it that animates — so it animates ONLY while
 * visible. Not visible (screen off, an app in front, the wallpaper preview closed) means NO scheduled
 * frame at all: not a slower one, none. The next frame is drawn when Android says it is visible again.
 *
 * Battery saver drops it to ~12 fps rather than freezing it: the person chose an animated wallpaper,
 * and a frozen one reads as broken, while 12 fps of slow neon is most of the effect for well under half
 * the cost.
 */
public final class FramePolicy {
    private FramePolicy() { }

    public static final long NORMAL_MS = 33;      // ~30 fps: the motion here is slow; 60 buys nothing
    public static final long SAVER_MS = 83;       // ~12 fps

    /** Milliseconds until the next frame, or -1 for "schedule nothing". */
    public static long nextDelayMs(boolean visible, boolean powerSave) {
        if (!visible) return -1;
        return powerSave ? SAVER_MS : NORMAL_MS;
    }
}
