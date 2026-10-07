package place.poster.app;

/**
 * WHICH MEMORY WARNINGS THE PAGE IS TOLD ABOUT (MainActivity.onTrimMemory → store.js `pc:trim-memory`).
 *
 * Android's levels: RUNNING_MODERATE 5, RUNNING_LOW 10, RUNNING_CRITICAL 15 while the app is in front;
 * UI_HIDDEN 20 when it has just gone behind something; BACKGROUND 40, MODERATE 60, COMPLETE 80 while it
 * sits in the background. Moderate is ignored -- trimming on it would throw away cache a person is about
 * to scroll back to for a warning Android itself treats as advisory. From RUNNING_LOW up, and once hidden,
 * a smaller cache is cheaper than the renderer being reclaimed. Pure, so the rule is run off-device.
 */
public final class TrimPolicy {
    private TrimPolicy() { }

    public static boolean relieve(int level) {
        return level >= 10;
    }
}
