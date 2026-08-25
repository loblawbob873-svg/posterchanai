package place.poster.app.home;

/** Pure timing/state for the launcher's double-Home shortcut. */
public final class HomeDoublePress {
    // This is measured when HOME intents reach the Activity, not when the person's finger lands.
    // Task/launcher animation can consume several hundred milliseconds, especially on older phones.
    public static final long MAX_GAP_MS = 1200L;
    public static final long MIN_GAP_MS = 80L;
    private static long lastAt = 0L;
    private HomeDoublePress() { }

    /** Records one HOME delivery and returns true exactly once for an intentional pair. */
    public static synchronized boolean arrived(long now) {
        long previous = lastAt;
        lastAt = now;
        if (previous <= 0L || now < previous) return false;
        long gap = now - previous;
        if (gap < MIN_GAP_MS || gap > MAX_GAP_MS) return false;
        lastAt = 0L;
        return true;
    }

    public static synchronized void clear() { lastAt = 0L; }
}
