package place.poster.app.shortcut;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.util.Log;

import place.poster.app.MainActivity;
import place.poster.app.home.HomeActivity;
import place.poster.app.home.LaunchView;

/**
 * ONE POSTERCHAN SCREEN AS AN APP IN THE PHONE'S DRAWER.
 *
 * Messages and Phone are drawer apps because they are NATIVE activities — an alias with
 * MAIN/LAUNCHER points straight at them. Email is not: the mail client is a view inside the WebView,
 * and there is nothing for an alias to target. Reported as "no Email app phone launcher either",
 * and the same is true of every other screen somebody might want on the phone's own home screen
 * rather than on ours.
 *
 * So this is a trampoline: no layout, no window, no WebView of its own. It starts MainActivity with
 * the view extra the launcher's own tiles already use — `HomeActivity.EXTRA_VIEW`, consumed by
 * `HomePlugin.consumeLaunchView` and turned into `PC.switchView` — and finishes before it has drawn
 * anything.
 *
 * WHICH VIEW IS A MANIFEST FACT, NOT A JAVA ONE. The alias that was launched carries
 * `<meta-data android:name="pc.view" .../>`, read back off the launching component, so a second
 * screen in the drawer is an alias and a string and no code at all. Reading it from the component
 * rather than from a per-view subclass is what makes that true: `getComponentName()` on an alias
 * returns the ALIAS, not its target, which is the same property `.ShareToAi` relies on to tell its
 * two share destinations apart.
 *
 * IT MUST NOT BE A TASK OF ITS OWN. `FLAG_ACTIVITY_NEW_TASK` plus the app's normal launch flags
 * brings the existing app forward if it is already running — which is what tapping Email should do
 * when PosterChan is already open — instead of stacking a second copy behind a transparent shim.
 *
 * AND IT MUST NOT LOOK LIKE ONE. The first version also set ACTION_MAIN and CATEGORY_LAUNCHER,
 * reasoning that this is a launcher entry so it should carry a launcher intent. That is exactly
 * backwards: an activity declared singleTask, handed the intent the home screen sends, is brought
 * back the way the person left it — extras and all discarded. Every drawer app opened the last
 * screen instead of its own ("email app is loading News", "same for other apps"). The component is
 * explicit; those two decorations only ever cost the payload.
 * And `finish()` runs unconditionally: an activity with no window that fails to start its target and
 * then stays is a phone showing nothing with no way to say why.
 */
public class ViewActivity extends Activity {

    private static final String TAG = "PosterChan";
    /** The manifest key an alias sets to say which client view it opens. */
    public static final String META_VIEW = "pc.view";

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        String view = viewOf(this, getComponentName());
        try {
            boolean particular = view != null && !view.isEmpty();
            // Parked BEFORE the start. See LaunchView: the intent extra alone only survives a COLD
            // start, and this trampoline is most often pressed while the app is already running.
            if (particular) LaunchView.request(view, System.currentTimeMillis());
            else LaunchView.clear();
            Intent i = new Intent(this, MainActivity.class)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            if (particular) {
                i.putExtra(HomeActivity.EXTRA_VIEW, view);
                // The timestamp is what makes the extra safe to replay: HomePlugin ignores one older
                // than a minute, so a stale intent restored with the task does not yank somebody
                // back to Email days later.
                i.putExtra(HomeActivity.EXTRA_VIEW_AT, System.currentTimeMillis());
            }
            startActivity(i);
        } catch (Throwable t) {
            Log.w(TAG, "shortcut: could not open the app at '" + view + "'", t);
        }
        finish();
    }

    /** The view an alias declares, or "" — which opens the app with no particular screen. */
    public static String viewOf(android.content.Context ctx, ComponentName who) {
        if (ctx == null || who == null) return "";
        try {
            ActivityInfo ai = ctx.getPackageManager()
                    .getActivityInfo(who, PackageManager.GET_META_DATA);
            if (ai == null || ai.metaData == null) return "";
            String v = ai.metaData.getString(META_VIEW);
            return v == null ? "" : v.trim();
        } catch (Throwable t) {
            Log.w(TAG, "shortcut: no meta-data on " + who.flattenToShortString(), t);
            return "";
        }
    }
}
