package place.poster.app.home;

/** Pure timing/state for the launcher's double-Home shortcut. */
public final class HomeDoublePress {
    // This is measured when HOME intents reach the Activity, not when the person's finger lands.
    // Task/launcher animation can consume several hundred milliseconds, especially on older phones.
    public static final long MAX_GAP_MS = 2000L;
    private static long lastAt = 0L;
    private HomeDoublePress() { }

    /** Records one HOME delivery and returns true exactly once for an intentional pair. */
    public static synchronized boolean arrived(long now) {
        long previous = lastAt;
        lastAt = now;
        if (previous <= 0L || now < previous) return false;
        long gap = now - previous;
        /* Do not impose a minimum delivery gap. Some real devices queue both HOME intents behind
         * the launcher transition and deliver them in the same scheduler tick even though the
         * person's presses were distinct. HomeActivity suppresses the onNewIntent -> onStart echo
         * from one physical press with homeIntentBeforeStart, which is where bounce belongs. */
        if (gap > MAX_GAP_MS) return false;
        lastAt = 0L;
        return true;
    }

    /** Clears an incomplete pair and reports whether one actually existed.  The launcher uses the
     * answer across onStop -> onStart: otherwise the returning onStart/onNewIntent echo can replace
     * the cancelled timestamp with two synthetic arrivals and immediately reopen the app. */
    public static synchronized boolean clear() {
        boolean incomplete = lastAt > 0L;
        lastAt = 0L;
        return incomplete;
    }
}
