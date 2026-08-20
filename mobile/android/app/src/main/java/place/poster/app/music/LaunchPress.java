package place.poster.app.music;

/**
 * A TRANSPORT PRESS MADE FROM OUTSIDE THE APP, PARKED WHERE THE ACTIVITY MANAGER CANNOT DROP IT.
 *
 * Reported as "on tablet: clicking play on music widget opens up default posterchan app page instead
 * of music" — which is the same bug LaunchView was written for, in a second place, and it survived
 * that fix because nobody looked here.
 *
 * `MusicWidget.launch` and `MusicService.revive` both built their intent with ACTION_MAIN plus
 * CATEGORY_LAUNCHER at an activity declared singleTask. That is the exact intent the system sends
 * when somebody taps an icon on their home screen, and its contract is "bring this app back the way
 * I left it", not "here is a payload". So on a WARM start the press went nowhere: the app animated
 * forward on whatever screen it had been on, the music did not start, and nothing threw and nothing
 * logged. `MainActivity.onNewIntent` calls `setIntent`, which is why this looked covered — but that
 * only runs when the intent is DELIVERED, and a launcher-shaped intent at a singleTask activity is
 * not.
 *
 * A static field cannot be dropped by any of that. MusicService, MusicWidget's receiver and
 * MainActivity are one process (none of them declares android:process), so a press written here is
 * readable by the plugin before the activity has finished resuming — the same reason the transport
 * talks to `MusicService.INSTANCE` directly instead of through an Intent.
 *
 * THE INTENT EXTRA IS KEPT and is not redundant: a COLD start is the one case with no process to
 * have parked anything, and it is exactly the case where the extra always worked, a freshly created
 * activity being handed its intent by definition. The two carriers cover disjoint halves.
 *
 * STALENESS IS PART OF THE HANDOFF. A parked press is one press; if the app takes a minute to reach
 * a state where it can perform one, the person has moved on, and starting the music over somebody
 * who had since paused it is a worse answer than doing nothing. Same rule and same minute as
 * LaunchView, deliberately — they are the same problem.
 *
 * Pure Java, no Android imports, so tests/test_android_launch_press.py RUNS it.
 */
public final class LaunchPress {

    /** How old a parked press may be before it is dropped rather than performed. */
    public static final long MAX_AGE_MS = 60_000L;

    private static String pending = "";
    private static long at = 0L;

    private LaunchPress() { }

    /**
     * Park the press. Called immediately BEFORE the activity is started, never after: on a fast
     * device the target can resume and read before the caller's next line runs.
     */
    public static synchronized void request(String action, long now) {
        if (action == null || action.trim().isEmpty()) { pending = ""; at = 0L; return; }
        pending = action.trim();
        at = now;
    }

    /**
     * The parked press, or "". Reading CONSUMES it: a press that is not consumed is performed again
     * on every later resume, which is how a widget tap restarts the music hours afterwards.
     */
    public static synchronized String take(long now) {
        String v = pending;
        long when = at;
        pending = "";
        at = 0L;
        if (v.isEmpty()) return "";
        if (now - when >= MAX_AGE_MS) return "";
        // A clock that has gone backwards (a manual set, a timezone-less RTC coming up) must not make
        // a fresh press look like it arrived from the future and get performed for ever after.
        if (now < when) return "";
        return v;
    }

    /** Whether a press is parked and still fresh, without consuming it. For diagnostics only. */
    public static synchronized boolean waiting(long now) {
        return !pending.isEmpty() && now >= at && now - at < MAX_AGE_MS;
    }

    /** Drop anything parked. */
    public static synchronized void clear() { pending = ""; at = 0L; }
}
