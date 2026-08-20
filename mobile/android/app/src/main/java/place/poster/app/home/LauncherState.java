package place.poster.app.home;

/**
 * IS THE PERSON LOOKING AT OUR HOME SCREEN RIGHT NOW — asked because being the launcher changes what
 * "the app is in the background" means.
 *
 * Folder sync divides the work by visibility, and the division is right: a hidden page's JavaScript
 * is throttled, so the native sweep takes over the moment MainActivity pauses. Nothing about that
 * needs changing for a launcher, and it is worth saying plainly that a sweep still only starts when a
 * folder is DUE by its own interval — pressing HOME cannot re-scan or re-upload anything, and the
 * fear that becoming the launcher would make sync "restart everything" does not have a mechanism
 * behind it. The claim set keeps the two engines off one folder, and it already survives this.
 *
 * WHAT DOES CHANGE IS WHEN "backgrounded" HAPPENS. Before, MainActivity pausing meant the person had
 * left PosterChan — a few times a day, usually with the screen about to go off. As the home app, it
 * means they pressed HOME, which is now the resting state of the phone and happens dozens of times an
 * hour, every one of them while the screen is on and a finger is on the glass. So a state that used
 * to imply "nobody is here" now routinely means "somebody is here, mid-gesture, about to open
 * something".
 *
 * That matters for one specific reason, and it is a reason the sync code already documents about
 * itself: a native sweep holds the folder's claim, and a page that then asks for it is refused and
 * can only say "syncing in the background — it will finish on its own". The comment on
 * FolderSyncPlugin.appInForeground calls that "a hang, and it is a hang they caused by opening the
 * app". As a launcher, the sequence that produces it — press home, sweep starts, open PosterChan — is
 * no longer a coincidence but the ordinary way the app gets opened.
 *
 * So a sweep stands down while our home screen is up AND the screen is interactive. That window is
 * seconds long in normal use, which is exactly why deferring costs nothing: the alarm re-fires, and
 * a phone's screen goes off many times an hour. It is not a battery heuristic dressed up as
 * correctness — the sweep is a wake lock, a foreground-service notification, hashing and radio, and
 * starting all of that in the half-second before somebody taps an icon is work that competes with
 * the thing they asked for.
 *
 * IT CANNOT STARVE. A phone parked on its home screen on a charger, screen never sleeping, would
 * otherwise never sync again — a deferral with no bound is an outage that looks like a policy. Past
 * MAX_DEFER_MS of continuous deferral the sweep proceeds anyway and this stops answering.
 *
 * Pure Java — the screen state is passed IN as a boolean rather than read here — so
 * tests/test_android_launcher_sync_quiet.py runs the rule instead of grepping for it.
 */
public final class LauncherState {

    /**
     * How long the home screen may hold a due sweep off before it goes ahead regardless. Half an
     * hour: long enough that ordinary use never reaches it, short enough that a phone left awake on
     * its home screen is late rather than broken.
     */
    public static final long MAX_DEFER_MS = 30L * 60L * 1000L;

    private static volatile boolean showing = false;
    /** When the CURRENT stretch of showing began — not the last time onStart ran. */
    private static volatile long since = 0L;
    /** Whether our home screen has run at all in this process. See {@link #weAreTheHomeScreen}. */
    private static volatile boolean everShown = false;

    private LauncherState() { }

    /** Our home screen came to the front. */
    public static synchronized void homeShown(long now) {
        everShown = true;
        // Only the FIRST of a run starts the clock: a redraw, a rotation or a drawer open must not
        // keep resetting it, or the starvation bound could never be reached.
        if (!showing) since = now;
        showing = true;
    }

    /** Our home screen went away — the person opened something. */
    public static synchronized void homeHidden() {
        showing = false;
        since = 0L;
    }

    /** Whether our home screen is the thing on screen. */
    public static synchronized boolean atHome() { return showing; }

    /**
     * Whether a due sweep should stand down for now.
     *
     * @param interactive whether the display is on and unlocked-ish — PowerManager.isInteractive().
     *                    Passed in so this file stays runnable off a device.
     * @return true only while somebody is plainly using the phone at our home screen, and only for
     *         as long as MAX_DEFER_MS allows.
     */
    public static synchronized boolean deferSweep(boolean interactive, long now) {
        if (!showing) return false;          // they are in an app, or the launcher is not ours
        if (!interactive) return false;      // screen off at the home screen: the ideal time to sync
        if (now < since) return false;       // a clock that moved backwards must not latch a deferral
        return now - since < MAX_DEFER_MS;
    }

    /** For the diagnostics panel: how long the current stretch has been deferring, or 0. */
    public static synchronized long deferredForMs(boolean interactive, long now) {
        return deferSweep(interactive, now) ? now - since : 0L;
    }

    /**
     * WHETHER THIS APP IS THE PHONE'S HOME SCREEN — answered by the fact that our home screen has
     * run, not by asking the package manager.
     *
     * It could be asked properly: HomeRoles.isDefaultHome resolves CATEGORY_HOME. But the caller
     * that needs this is inside folder sync, and reaching into the launcher package from there drags
     * HomeRoles, HomeActivity, DeskView and MainActivity into a compile that is deliberately scoped
     * to `sync` — tests/test_android_sync_compiles.py went from green to thirty-three errors on the
     * import alone. A dependency that costs the compile floor is the wrong dependency.
     *
     * The weaker signal is sufficient here, and it is a fact rather than a lookup: if HomeActivity
     * has started in this process, somebody's phone put our home screen on screen. A default home
     * app is started at boot and stays, so any process old enough to have paused MainActivity has
     * long since told us. It never un-sets, deliberately — a person who changes their launcher away
     * mid-session costs at most a deferred sweep, and the process will not outlive the change by
     * much.
     */
    public static synchronized boolean weAreTheHomeScreen() { return everShown; }

    /**
     * Whether the pause that just happened should hold its sweep back.
     *
     * SEPARATE FROM {@link #deferSweep} BECAUSE IT KNOWS LESS. MainActivity.onPause runs BEFORE
     * HomeActivity.onStart, so at that instant nothing knows the home screen is what is coming up —
     * `showing` is still false. What is knowable is that the screen is still on and that we are the
     * launcher, which together mean the person is right there, most likely one tap from opening
     * something. A screen going OFF also pauses, is not interactive, and sweeps exactly as before.
     */
    public static synchronized boolean deferHandover(boolean interactive) {
        return interactive && everShown;
    }

    /** Visible only so a test can start from a known state. */
    public static synchronized void resetForTest() { showing = false; since = 0L; everShown = false; }
}
