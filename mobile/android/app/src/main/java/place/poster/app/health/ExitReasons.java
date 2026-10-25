package place.poster.app.health;

/**
 * WHY THE APP'S PROCESS ENDED, IN WORDS -- the labels for android.app.ApplicationExitInfo.
 *
 * "The desktop starts like a fresh new session" on the tablet, with NO toast (2026-10-25). A WebView renderer death
 * always shows one (MainActivity.surviveRenderProcessDeath), so a silent fresh start is the whole PROCESS ending:
 * a Java crash, a native crash, an ANR, or Android reclaiming memory. Those need different fixes, and Android keeps
 * the answer (getHistoricalProcessExitReasons, API 30+). AppHealthPlugin reads it; this names it.
 *
 * Pure on purpose -- no android.* import -- so tests/test_android_exit_reasons.py compiles and RUNS it with plain
 * javac. The constants are ApplicationExitInfo's documented values, written as numbers so they cannot depend on
 * the compileSdk a build happens to use.
 */
public final class ExitReasons {
    private ExitReasons() {}

    public static String reason(int code) {
        switch (code) {
            case 1: return "exit-self";
            case 2: return "signaled";
            case 3: return "low-memory";
            case 4: return "crash";
            case 5: return "crash-native";
            case 6: return "anr";
            case 7: return "initialization-failure";
            case 8: return "permission-change";
            case 9: return "excessive-resource-usage";
            case 10: return "user-requested";
            case 11: return "user-stopped";
            case 12: return "dependency-died";
            case 13: return "other";
            case 14: return "freezer";
            case 15: return "package-state-change";
            case 16: return "package-updated";
            default: return "unknown";
        }
    }

    /** RunningAppProcessInfo importance at the moment the process ended. */
    public static String importance(int value) {
        if (value <= 100) return "foreground";
        if (value <= 125) return "foreground-service";
        if (value <= 200) return "visible";
        if (value <= 230) return "perceptible";
        if (value <= 300) return "service";
        if (value <= 325) return "top-sleeping";
        if (value <= 350) return "cant-save-state";
        if (value <= 400) return "cached";
        return "gone";
    }

    /** Was the person looking at the app when it ended? (foreground or visible -- "in the middle of using it") */
    public static boolean onScreen(int importance) {
        return importance > 0 && importance <= 200;
    }

    /** Ended by Android or by a fault, as opposed to the person or an update -- the ones worth a report. */
    public static boolean unexpected(int code) {
        switch (code) {
            case 2: case 3: case 4: case 5: case 6: case 9: case 12: case 14: case 0: case 13:
                return true;
            default:
                return false;
        }
    }
}
