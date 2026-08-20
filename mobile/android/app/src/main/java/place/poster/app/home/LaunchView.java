package place.poster.app.home;

/**
 * WHICH SCREEN A TILE ASKED FOR, PARKED WHERE THE ACTIVITY MANAGER CANNOT DROP IT.
 *
 * Reported as "on tablet, email app is loading News!" and then "same for other apps" — which is the
 * shape of the bug, not a second report. Every PosterChan tile and every drawer alias funnelled
 * through one of two identical intent builders, so they all failed together, and they all failed the
 * same way: the app came forward showing whatever it had been showing last.
 *
 * The channel was an intent extra, and an extra only survives if the intent is DELIVERED. Both
 * builders dressed their intent as a launcher press — ACTION_MAIN plus CATEGORY_LAUNCHER — on an
 * activity declared singleTask. That is the exact intent the system receives when somebody taps an
 * icon on their home screen, and its contract is "bring this app back the way I left it", not "here
 * is a payload". So on a WARM start the extras went nowhere. Nothing threw, nothing logged, the app
 * animated forward perfectly, and the only visible symptom was the wrong screen — which is
 * indistinguishable from a tile wired to the wrong view, and that is what four rounds of reading the
 * catalogue were spent on.
 *
 * A static field cannot be dropped by any of that. ViewActivity, HomeActivity and MainActivity are
 * one process (none of them declares android:process), so a request written here in the launcher is
 * readable by the plugin before the target activity has finished resuming. It is the same reason the
 * music controls talk to MusicService.INSTANCE directly instead of through an Intent.
 *
 * THE INTENT EXTRA IS KEPT, and is not redundant. Exactly one case has no static state to read: a
 * COLD start, where the process is created by this very launch — and that is precisely the case
 * where the extra always worked, because a freshly created activity is handed its intent by
 * definition. The two carriers cover disjoint halves, so the pair has no gap. Whichever answers
 * first wins and clears itself.
 *
 * STALENESS IS PART OF THE HANDOFF, not decoration. A parked request is one press; if the app takes
 * a minute to reach a state where it can read one, the person has moved on, and opening Email over
 * whatever they are now doing is a worse answer than opening nothing. Same rule and same minute as
 * MusicPlugin's parked press, deliberately — they are the same problem.
 *
 * Pure Java, no Android imports, so tests/test_android_launch_view.py RUNS it.
 */
public final class LaunchView {

    /** How old a parked request may be before it is dropped rather than performed. */
    public static final long MAX_AGE_MS = 60_000L;

    private static String pending = "";
    private static long at = 0L;

    private LaunchView() { }

    /**
     * Park the view a tile just asked for. Called on the launcher side, immediately before the
     * activity is started — never after, since the target can resume and read before the caller's
     * next line on a fast device.
     */
    public static synchronized void request(String view, long now) {
        if (view == null || view.trim().isEmpty()) { pending = ""; at = 0L; return; }
        pending = view.trim();
        at = now;
    }

    /**
     * The parked request, or "". Reading CONSUMES it: a launch extra that is not consumed is
     * re-performed on every later resume, which is how a press could yank somebody back to Email
     * long after they had moved on.
     */
    public static synchronized String take(long now) {
        String v = pending;
        long when = at;
        pending = "";
        at = 0L;
        if (v.isEmpty()) return "";
        if (now - when >= MAX_AGE_MS) return "";
        // A clock that has gone backwards (a manual set, a timezone-less RTC coming up) must not make
        // a fresh request look like it arrived from the future and get performed for ever after.
        if (now < when) return "";
        return v;
    }

    /** Whether a request is parked and still fresh, without consuming it. For diagnostics only. */
    public static synchronized boolean waiting(long now) {
        return !pending.isEmpty() && now >= at && now - at < MAX_AGE_MS;
    }

    /** Drop anything parked. Used when the app is told to open nothing in particular. */
    public static synchronized void clear() { pending = ""; at = 0L; }
}
