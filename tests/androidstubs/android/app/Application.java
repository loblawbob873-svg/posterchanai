package android.app;

/* Enough of android.app.Application to COMPILE PosterChanApp, which installs the app-wide uncaught
 * exception handler. A test that cannot compile is a test that does not exist, only quieter — and
 * that has already cost this repo a whole rewrite's worth of coverage on the Android sweep.
 *
 * Deliberately NOT extending the Context stub: Context there is abstract with a surface this class
 * has no use for, and a stub that has to grow to stay compilable is a stub that will stop being
 * maintained. Nothing in PosterChanApp calls a Context method. */
public class Application {
    public void onCreate() { }
}
