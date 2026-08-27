package place.poster.app.music;

import static org.junit.Assert.assertTrue;
import static org.junit.Assert.assertEquals;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.core.content.ContextCompat;
import androidx.lifecycle.Lifecycle;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.AtomicInteger;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;

import place.poster.app.MainActivity;
import place.poster.app.home.HomeRoles;
import place.poster.app.home.LauncherState;

/** The reported failure: a playing track stopped as soon as Android Home was pressed. */
@RunWith(AndroidJUnit4.class)
public class MusicBackgroundDeviceTest {

    @Test
    public void tabletDesktopStateSurvivesHomeAndRotationInBothTasks() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        boolean wasEnabled = HomeRoles.launcherComponentEnabled(ctx);
        String oldHome = firstRoleHolder(shell("cmd role get-role-holders android.app.role.HOME"));
        HomeRoles.enableLauncherComponent(ctx, true);
        shell("cmd role add-role-holder android.app.role.HOME " + ctx.getPackageName());
        SystemClock.sleep(500);
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        AtomicReference<MainActivity> activity = new AtomicReference<MainActivity>();
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            AtomicInteger task = new AtomicInteger();
            scenario.onActivity(a -> {
                activity.set(a);
                ref.set(findWebView(a.findViewById(android.R.id.content)));
                task.set(a.getTaskId());
            });
            WebView web = waitForWebView(ref, scenario);
            waitForClientPage(web);
            assertEquals("\"desktop-ready\"", eval(web,
                    "localStorage.setItem('osMode','true');"
                    + "window.__pcTabletLifecycle={windows:['files','messages'],focus:'messages',snap:'right'};"
                    + "'desktop-ready'"));

            InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
            SystemClock.sleep(900);
            assertTrue("HOME did not background the Desktop activity",
                    scenario.getState() == Lifecycle.State.CREATED);
            assertTrue("HOME did not show the independent native launcher", LauncherState.atHome());

            // MainActivity declares orientation/screenSize configChanges. A tablet rotation while
            // its separate task is behind HomeActivity must not recreate its Bridge/WebView.
            scenario.onActivity(a -> a.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE));
            SystemClock.sleep(500);
            assertTrue("background rotation foregrounded or destroyed Desktop",
                    scenario.getState() == Lifecycle.State.CREATED);

            scenario.moveToState(Lifecycle.State.RESUMED);
            AtomicReference<WebView> resumed = new AtomicReference<WebView>();
            AtomicInteger resumedTask = new AtomicInteger();
            scenario.onActivity(a -> {
                resumed.set(findWebView(a.findViewById(android.R.id.content)));
                resumedTask.set(a.getTaskId());
            });
            assertTrue("returning from launcher replaced Desktop's live WebView", web == resumed.get());
            assertEquals("launcher conflated the HOME and Desktop tasks", task.get(), resumedTask.get());
            assertEquals("\"true|files,messages|messages|right\"", eval(web,
                    "localStorage.getItem('osMode')+'|'+window.__pcTabletLifecycle.windows.join(',')+'|'"
                    + "+window.__pcTabletLifecycle.focus+'|'+window.__pcTabletLifecycle.snap"));

            scenario.onActivity(a -> a.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT));
            SystemClock.sleep(500);
            AtomicReference<WebView> rotated = new AtomicReference<WebView>();
            scenario.onActivity(a -> rotated.set(findWebView(a.findViewById(android.R.id.content))));
            assertTrue("foreground rotation replaced Desktop's live WebView", web == rotated.get());
            assertEquals("\"true|messages\"", eval(web,
                    "localStorage.getItem('osMode')+'|'+window.__pcTabletLifecycle.focus"));
        } finally {
            restoreDeviceState(ctx, oldHome, wasEnabled, scenario, activity);
        }
    }

    @Test
    public void aPlayingWebViewTrackKeepsAdvancingAfterHome() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        boolean wasEnabled = HomeRoles.launcherComponentEnabled(ctx);
        String oldHome = firstRoleHolder(shell("cmd role get-role-holders android.app.role.HOME"));
        HomeRoles.enableLauncherComponent(ctx, true);
        shell("cmd role add-role-holder android.app.role.HOME " + ctx.getPackageName());
        SystemClock.sleep(500);
        assertTrue("the emulator did not assign PosterChan the HOME role", HomeRoles.isDefaultHome(ctx));
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        AtomicReference<MainActivity> activity = new AtomicReference<MainActivity>();
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            scenario.onActivity(a -> {
                activity.set(a);
                ref.set(findWebView(a.findViewById(android.R.id.content)));
            });
            WebView web = waitForWebView(ref, scenario);
            waitForClientPage(web);

            // A small generated WAV avoids network, account and Blossom dependencies. Looping it
            // exercises Chromium's real media clock while MusicService reproduces production's
            // foreground-playback condition.
            /* Thirty seconds, not four. Starting the foreground service and sending Home through
             * instrumentation can take several seconds on a loaded emulator. With the former
             * four-second looping WAV, healthy continuous playback wrapped currentTime from 1.14
             * to 0.91 and the monotonic assertion falsely called that a stop. Keep loop enabled so
             * the media remains valid if a very slow device exceeds thirty seconds, but make the
             * measurement window far shorter than one lap. */
            String start = "(()=>{const n=240000,b=new ArrayBuffer(44+n),v=new DataView(b);"
                    + "const s=(o,x)=>{for(let i=0;i<x.length;i++)v.setUint8(o+i,x.charCodeAt(i))};"
                    + "s(0,'RIFF');v.setUint32(4,36+n,true);s(8,'WAVEfmt ');v.setUint32(16,16,true);"
                    + "v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);"
                    + "v.setUint32(28,8000,true);v.setUint16(32,1,true);v.setUint16(34,8,true);"
                    + "s(36,'data');v.setUint32(40,n,true);for(let i=44;i<44+n;i++)v.setUint8(i,128);"
                    + "let a=new Audio(URL.createObjectURL(new Blob([b],{type:'audio/wav'})));"
                    + "a.loop=true;window.__pcBackgroundAudio=a;return a.play().then(()=>a.currentTime)})()";
            eval(web, start);
            double before = 0;
            for (int i = 0; i < 30 && before <= 0.15; i++) {
                SystemClock.sleep(100);
                before = number(eval(web, "window.__pcBackgroundAudio.currentTime"));
            }
            assertTrue("the injected track never began playing: " + before, before > 0.15);

            Intent service = new Intent(ctx, MusicService.class).setAction(MusicService.ACTION_UPDATE)
                    .putExtra(MusicService.EXTRA_TITLE, "background-device-test")
                    .putExtra(MusicService.EXTRA_ARTIST, "PosterChan")
                    .putExtra(MusicService.EXTRA_PLAYING, true)
                    .putExtra(MusicService.EXTRA_POSITION, before)
                    .putExtra(MusicService.EXTRA_DURATION, 30.0);
            ContextCompat.startForegroundService(ctx, service);
            SystemClock.sleep(500);

            InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
            SystemClock.sleep(1600);
            double after = number(eval(web, "window.__pcBackgroundAudio.currentTime"));
            assertTrue("audio stopped on Home (before=" + before + ", after=" + after + ")",
                    after > before + 0.7);
            /* Advancing audio alone does not prove Home worked: a broken launcher transition can
             * leave MainActivity visible while its track quite correctly keeps playing. Verify the
             * two promises together. ActivityScenario reports CREATED only after this Activity has
             * actually lost the screen; when PosterChan is the configured HOME app, LauncherState
             * is set by HomeActivity.onStart and proves the native launcher owns that screen. */
            assertTrue("PosterChan's WebView stayed visible after Home: " + scenario.getState(),
                    scenario.getState() == Lifecycle.State.CREATED);
            assertTrue("Home backgrounded the player but did not show PosterChan's launcher",
                    LauncherState.atHome());
        } finally {
            try { ctx.startService(new Intent(ctx, MusicService.class).setAction(MusicService.ACTION_STOP)); }
            catch (Throwable ignored) { }
            restoreDeviceState(ctx, oldHome, wasEnabled, scenario, activity);
        }
    }

    /** Restore global emulator state even when HOME left ActivityScenario in CREATED/STOPPED.
     * onActivity() requires RESUMED and therefore cannot be used from a failing test's cleanup. */
    private static void restoreDeviceState(Context ctx, String oldHome, boolean wasEnabled,
                                           ActivityScenario<MainActivity> scenario,
                                           AtomicReference<MainActivity> activity) {
        try {
            MainActivity a = activity.get();
            if (a != null) InstrumentationRegistry.getInstrumentation().runOnMainSync(
                    () -> a.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED));
        } catch (Throwable ignored) { }
        try { shell("cmd role remove-role-holder android.app.role.HOME " + ctx.getPackageName()); }
        catch (Throwable ignored) { }
        if (!oldHome.isEmpty() && !oldHome.equals(ctx.getPackageName()))
            try { shell("cmd role add-role-holder android.app.role.HOME " + oldHome); }
            catch (Throwable ignored) { }
        try {
            ctx.startActivity(new Intent(ctx, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
            SystemClock.sleep(250);
            scenario.moveToState(Lifecycle.State.RESUMED);
        } catch (Throwable ignored) { }
        try { scenario.close(); } catch (Throwable ignored) { }
        try { HomeRoles.enableLauncherComponent(ctx, wasEnabled); } catch (Throwable ignored) { }
    }

    private static String shell(String cmd) throws Exception {
        try (InputStream is = new android.os.ParcelFileDescriptor.AutoCloseInputStream(
                InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand(cmd))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096]; int n;
            while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    private static String firstRoleHolder(String output) {
        String normalized = output == null ? "" : output.replace('[', ' ').replace(']', ' ').trim();
        return normalized.isEmpty() ? "" : normalized.split("\\s+")[0];
    }

    private static WebView waitForWebView(AtomicReference<WebView> ref,
                                          ActivityScenario<MainActivity> scenario) throws Exception {
        for (int i = 0; i < 80; i++) {
            scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
            if (ref.get() != null) return ref.get();
            SystemClock.sleep(100);
        }
        throw new AssertionError("MainActivity never created its WebView");
    }

    /** A WebView object exists before Capacitor navigates it away from about:blank. Injecting the
     * test audio into that provisional document makes it disappear during the real navigation, so
     * currentTime reads as zero and the test fails before it ever presses Home. Wait for the bundled
     * origin and a completed document, then prove it stayed there across a scheduling turn. */
    private static void waitForClientPage(WebView web) throws Exception {
        String prior = "";
        for (int i = 0; i < 120; i++) {
            String state = eval(web, "location.href+'|'+document.readyState");
            boolean client = state.contains("https://localhost/") || state.contains("capacitor://localhost/");
            if (client && state.contains("|complete") && state.equals(prior)) return;
            prior = state;
            SystemClock.sleep(100);
        }
        throw new AssertionError("bundled client page never became stable: " + prior);
    }

    private static WebView findWebView(View v) {
        if (v instanceof WebView) return (WebView) v;
        if (!(v instanceof ViewGroup)) return null;
        ViewGroup g = (ViewGroup) v;
        for (int i = 0; i < g.getChildCount(); i++) {
            WebView found = findWebView(g.getChildAt(i));
            if (found != null) return found;
        }
        return null;
    }

    private static String eval(WebView web, String js) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<String>("null");
        web.post(() -> web.evaluateJavascript(js, value -> { result.set(value); done.countDown(); }));
        assertTrue("WebView did not answer JavaScript", done.await(15, TimeUnit.SECONDS));
        return result.get();
    }

    private static double number(String json) {
        try { return Double.parseDouble(json == null ? "0" : json.replace("\"", "")); }
        catch (Exception ignored) { return 0; }
    }
}
