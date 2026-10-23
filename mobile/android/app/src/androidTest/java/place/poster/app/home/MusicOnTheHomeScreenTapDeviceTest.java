package place.poster.app.home;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.List;

import place.poster.app.RealUi;
import place.poster.app.music.MusicWidget;
import place.poster.app.music.PlayingPage;

/**
 * THE MUSIC CONTROLS ON THE HOME SCREEN, PRESSED: the 4x1 music WIDGET placed on PosterChan's own home
 * screen, and the launcher's now-playing strip.
 *
 * WidgetDeviceTest proves the widget is listed, previewed and can be placed; nothing pressed its
 * buttons. A widget's button is a PendingIntent inside RemoteViews hosted by the launcher -- the press
 * crosses the launcher, the system, MusicWidget's broadcast, MusicService.fromWidget and the WebView
 * before anything is heard, and it is the surface most often pressed with the app closed. Each step
 * here is a real touch on the control as the launcher drew it; what it did is read off the page's
 * audio element.
 */
@RunWith(AndroidJUnit4.class)
public class MusicOnTheHomeScreenTapDeviceTest {

    private Context ctx;
    private boolean wasEnabled;
    private Widgets widgets;
    private PlayingPage page;
    private int placed = -1;
    private LauncherPrefs prefs;
    private String geom = "", deskBefore = null;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        wasEnabled = HomeRoles.launcherComponentEnabled(ctx);
        HomeRoles.enableLauncherComponent(ctx, true);
        widgets = new Widgets(ctx);
        widgets.start();
        prefs = new LauncherPrefs(ctx);
        RealUi.awake();
    }

    @After
    public void tearDown() {
        RealUi.shell("input keyevent KEYCODE_HOME");
        if (page != null) page.close();
        if (!geom.isEmpty() && deskBefore != null) prefs.setDesk(geom, deskBefore);
        if (placed >= 0) widgets.release(placed);
        widgets.stop();
        RealUi.shell("appwidget revokebind --package " + ctx.getPackageName() + " --user 0");
        HomeRoles.enableLauncherComponent(ctx, wasEnabled);
    }

    @Test
    public void theWidgetsPlayPauseAndSkipButtonsReachThePlayingPage() throws Exception {
        page = PlayingPage.start();
        placeMusicWidget();
        home();

        RealUi.tap("the widget's pause button", RealUi.id("mw_play"), 10_000);
        page.expect("the widget's PAUSE", "window.__pcBackgroundAudio.paused===true");
        RealUi.waitUntil("the widget kept showing Pause after the music paused", 8_000, () -> {
            RealUi.Hit b = RealUi.find(RealUi.id("mw_play"));
            return b != null && "Play".equalsIgnoreCase(b.desc);
        });

        RealUi.tap("the widget's play button", RealUi.id("mw_play"), 10_000);
        page.expect("the widget's PLAY", "window.__pcBackgroundAudio.paused===false");

        // SKIP BUTTONS ONLY WHERE THE WIDGET DRAWS THEM. MusicWidget.render deliberately drops prev/next
        // below 240dp (art + title + three 44dp buttons do not fit) -- which is the whole width of the
        // home screen on a 320dp phone, CI's emulator among them. Pressed wherever they are drawn.
        RealUi.Hit play = RealUi.waitFor("the widget's play button", RealUi.id("mw_play"), 5_000);
        if (RealUi.find(RealUi.id("mw_next")) == null) {
            android.util.Log.i("PosterChanTest", "music widget is compact here (" + play.bounds.toShortString()
                    + " play button): no skip buttons to press");
            return;
        }
        RealUi.tap("the widget's next button", RealUi.id("mw_next"), 10_000);
        page.expect("the widget's NEXT", "(window.__pcNext||0)>=1");
        RealUi.tap("the widget's previous button", RealUi.id("mw_prev"), 10_000);
        page.expect("the widget's PREVIOUS", "(window.__pcPrev||0)>=1");
    }

    @Test
    public void theLaunchersNowPlayingStripPausesAndResumes() throws Exception {
        page = PlayingPage.start();
        home();
        RealUi.tap("the home screen's now-playing pause", RealUi.id("pc_home_now_toggle"), 10_000);
        page.expect("the now-playing strip's PAUSE", "window.__pcBackgroundAudio.paused===true");
        RealUi.tap("the home screen's now-playing play", RealUi.id("pc_home_now_toggle"), 10_000);
        page.expect("the now-playing strip's PLAY", "window.__pcBackgroundAudio.paused===false");
    }

    // ------------------------------------------------------------------ the launcher

    private void home() {
        ctx.startActivity(new Intent(ctx, HomeActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP));
        RealUi.waitFor("PosterChan's home screen", RealUi.id("pc_home_desk"), 15_000);
    }

    /** Bind the music widget and put it on the desk through the launcher's real placement. */
    private void placeMusicWidget() throws Exception {
        RealUi.shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        Widgets.Choice music = null;
        List<Widgets.Choice> rows = widgets.providers(200, 200);
        for (Widgets.Choice c : rows) {
            if (new ComponentName(ctx, MusicWidget.class).equals(c.provider())) music = c;
        }
        assertNotNull("the music widget is not offered to the launcher", music);
        final Widgets.Choice pick = music;
        final int[] made = { -1 };
        final String[] g = { "" };
        // Once to read the grid this phone gets, then an EMPTY desk for that grid (put back in @After):
        // the launcher places from the desk it LOADED, so the room has to exist before it starts.
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            RealUi.waitFor("PosterChan's home screen", RealUi.id("pc_home_desk"), 15_000);
            s.onActivity(a -> {
                android.view.ViewGroup h = (android.view.ViewGroup) a.findViewById(place.poster.app.R.id.pc_home_desk);
                DeskView d = (DeskView) h.getChildAt(0);
                g[0] = HomeMetrics.geometry(d.cols(), d.rows());
            });
        } finally {
            s.close();
        }
        geom = g[0];
        deskBefore = prefs.desk(geom);
        prefs.setDesk(geom, "");
        s = ActivityScenario.launch(HomeActivity.class);
        try {
            RealUi.waitFor("PosterChan's home screen", RealUi.id("pc_home_desk"), 15_000);
            s.onActivity(a -> made[0] = widgets.add(a, pick));
            assertTrue("the music widget could not be bound", made[0] >= 0);
            placed = made[0];
            s.onActivity(a -> a.placeWidget(made[0]));
        } finally {
            s.close();
        }
    }
}
