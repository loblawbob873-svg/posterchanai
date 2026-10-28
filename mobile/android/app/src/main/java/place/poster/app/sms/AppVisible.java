package place.poster.app.sms;

/**
 * IS THE APP ON SCREEN? Written by the Activity's own lifecycle and read by the background drain.
 *
 * It exists to keep two senders off one radio. The client's JavaScript drains the outbox when it is
 * visible; SmsOutbox refuses while this says true. Without that they are two readers of one request
 * with no agreement between them, and the failure is a text sent TWICE -- which cannot be undone.
 *
 * A static, because the two sides are a foreground service and an Activity in one process and the
 * question is about this process. It is deliberately NOT a guess from ActivityManager: "is my app
 * in the foreground" is answerable exactly and cheaply from onResume/onPause, and an inference
 * would be wrong in precisely the window that matters -- the seconds around the app opening.
 *
 * Defaults to FALSE, which is the safe direction: a process that has not started an Activity is one
 * where the client's drain is certainly not running, so the background drain is the only sender.
 */
public final class AppVisible {

    private static volatile boolean on = false;
    private static volatile long shownAt = 0L;

    private AppVisible() { }

    public static void set(boolean visible) { on = visible; if (visible) shownAt = System.currentTimeMillis(); }

    public static boolean is() { return on; }

    /** When the app last came on screen (wall clock, ms), 0 if never in this process. MusicService reads it to tell a
     *  revive Android REFUSED from one that is merely slow -- see LaunchPress.abandonRefused. */
    public static long shownAt() { return shownAt; }
}
