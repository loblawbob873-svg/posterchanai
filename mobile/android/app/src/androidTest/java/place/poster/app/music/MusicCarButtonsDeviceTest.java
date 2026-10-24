package place.poster.app.music;

import static org.junit.Assert.assertTrue;

import android.app.ActivityManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.media.AudioManager;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import place.poster.app.MainActivity;
import place.poster.app.phone.DialerActivity;
import place.poster.app.push.StayAwakeService;

/**
 * "Car media controls no longer work on music": a PLAIN BLUETOOTH car (AVRCP, not Android Auto) pressed
 * while PosterChan music plays in the background, and nothing at all happened.
 *
 * A Bluetooth head unit's buttons arrive on the phone as media KEY EVENTS that Android routes to a
 * media session -- exactly what {@link AudioManager#dispatchMediaKeyEvent} does. So every test here
 * plays a real track through the SHIPPED MusicPlayer (MusicPlayer -> MusicControls.update -> the
 * foreground MusicService and its MediaSession), sends the app to the background, presses keys the way
 * the car does, and asserts what the page's own audio element did: paused, played, skipped. The same
 * presses are also made through MediaControllerCompat's transport controls (the path a head unit's
 * AVRCP "addressed player" commands and the lock screen take).
 *
 * Failures carry `dumpsys media_session` -- which session Android made the media-button session -- and
 * the app's own press log (PressLog), so a red run says WHERE the key went, not only that it was lost.
 */
@RunWith(AndroidJUnit4.class)
public class MusicCarButtonsDeviceTest {

    private ActivityScenario<MainActivity> scenario;
    private WebView web;

    @After
    public void tearDown() {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        /* THE PAGE GOES FIRST, THEN THE SERVICE, THEN WAIT FOR IT TO BE GONE. Stopped while the page
         * still played, the page's next state push started a FRESH MusicService behind the test, and
         * the next case (the standby one) then turned Bluetooth autoplay on beside a live player
         * session -- measured on the nas emulator: the standby never opened, red for a reason that was
         * this teardown's. */
        try { bringBack(ctx); } catch (Throwable ignored) { }
        try { if (scenario != null) scenario.close(); } catch (Throwable ignored) { }
        try { ctx.startService(MusicService.intent(ctx, MusicService.ACTION_STOP)); }
        catch (Throwable ignored) { }
        for (int i = 0; i < 100 && MusicService.INSTANCE != null; i++) SystemClock.sleep(100);
        try { ctx.startService(new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_STOP)); }
        catch (Throwable ignored) { }
        try { MusicService.setAutoplayBluetooth(ctx, false); } catch (Throwable ignored) { }
    }

    /** The plain case: the app in the background, the car's keys and the session's transport controls. */
    @Test
    public void carKeysPauseResumeAndSkipTheTrackWithTheAppInTheBackground() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        startPlaying();
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
        SystemClock.sleep(1500);
        assertAdvancing("Home stopped the music before any key was pressed");
        pressesReachThePage(ctx, "background");
    }

    /**
     * Texts and the Phone app are TASKS OF THEIR OWN. Removing one of them from Recents calls
     * MusicService.onTaskRemoved, which used to shut the media session down while the song played on in
     * the untouched WebView task -- leaving the car's buttons with no session to land in.
     */
    @Test
    public void closingAnotherPosterChanTaskDoesNotTakeTheCarButtonsAway() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        startPlaying();
        ctx.startActivity(new Intent(ctx, DialerActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        ActivityManager.AppTask other = null;
        String seen = "";
        for (int i = 0; i < 60 && other == null; i++) {
            SystemClock.sleep(100);
            ActivityManager am = (ActivityManager) ctx.getSystemService(Context.ACTIVITY_SERVICE);
            List<ActivityManager.AppTask> tasks = am.getAppTasks();
            StringBuilder names = new StringBuilder();
            for (ActivityManager.AppTask t : tasks) {
                ComponentName c = null;
                try { c = t.getTaskInfo().baseIntent.getComponent(); } catch (Throwable ignored) { }
                String n = c == null ? "?" : c.getClassName();
                names.append(n).append(' ');
                if (c != null && !MainActivity.class.getName().equals(n)
                        && n.startsWith("place.poster.app.phone")) other = t;
            }
            seen = names.toString();
        }
        assertTrue("the Phone app never opened as a task of its own; tasks: " + seen, other != null);
        other.finishAndRemoveTask();      // what swiping its card out of Recents does
        SystemClock.sleep(2000);
        assertAdvancing("closing the Phone task stopped the music itself");
        assertTrue("closing ANOTHER PosterChan task (the Phone app) shut the music session down while the "
                + "song kept playing -- the car's buttons now have nowhere to go. " + sessionReport(),
                sessionActive());
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
        SystemClock.sleep(1200);
        pressesReachThePage(ctx, "after the Phone task was closed");
    }

    /**
     * With "Bluetooth autoplay" on, StayAwakeService keeps a STANDBY session (PosterChanStandby) so a car
     * has something to talk to before anything plays. Once a real track plays, the buttons must reach the
     * player, never the standby session -- whose pause used to be a no-op.
     */
    @Test
    public void theStandbySessionNeverTakesTheButtonsFromAPlayingTrack() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        MusicService.setAutoplayBluetooth(ctx, true);
        androidx.core.content.ContextCompat.startForegroundService(ctx,
                new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_START));
        boolean opened = false;
        for (int i = 0; i < 50 && !(opened = MusicService.standbySession); i++) SystemClock.sleep(100);
        assertTrue("the standby session never opened with Bluetooth autoplay on", opened);

        startPlaying();
        boolean dropped = false;
        for (int i = 0; i < 50 && !(dropped = !MusicService.standbySession); i++) SystemClock.sleep(100);
        assertTrue("the standby session stayed active beside the real one", dropped);

        // "Stay connected" is re-armed on every app start; that must not bring the standby session back
        // while a track plays.
        ctx.startService(new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_START));
        SystemClock.sleep(800);
        assertTrue("re-arming StayAwakeService re-opened the standby session beside a playing track",
                !MusicService.standbySession);

        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
        SystemClock.sleep(1500);
        pressesReachThePage(ctx, "with the standby switch on");
    }

    // ── the presses ───────────────────────────────────────────────────────────────────────────────

    private void pressesReachThePage(Context ctx, String when) throws Exception {
        AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);

        key(am, KeyEvent.KEYCODE_MEDIA_PAUSE);
        expect("the car's PAUSE key (" + when + ")", "window.__pcBackgroundAudio.paused===true");
        key(am, KeyEvent.KEYCODE_MEDIA_PLAY);
        expect("the car's PLAY key (" + when + ")", "window.__pcBackgroundAudio.paused===false");
        key(am, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE);
        expect("the car's PLAY_PAUSE key while playing (" + when + ")", "window.__pcBackgroundAudio.paused===true");
        key(am, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE);
        expect("the car's PLAY_PAUSE key while paused (" + when + ")", "window.__pcBackgroundAudio.paused===false");
        key(am, KeyEvent.KEYCODE_MEDIA_NEXT);
        expect("the car's NEXT key (" + when + ")", "(window.__pcNext||0)>=1");
        key(am, KeyEvent.KEYCODE_MEDIA_PREVIOUS);
        expect("the car's PREVIOUS key (" + when + ")", "(window.__pcPrev||0)>=1");

        // The same presses through the session's own transport controls.
        Object tc = controls(ctx);
        assertTrue("no PosterChanMusic session to build a controller for. " + sessionReport(), tc != null);
        transport(tc, "pause");
        expect("TransportControls.pause (" + when + ")", "window.__pcBackgroundAudio.paused===true");
        transport(tc, "play");
        expect("TransportControls.play (" + when + ")", "window.__pcBackgroundAudio.paused===false");
        transport(tc, "skipToNext");
        expect("TransportControls.skipToNext (" + when + ")", "(window.__pcNext||0)>=2");
        transport(tc, "skipToPrevious");
        expect("TransportControls.skipToPrevious (" + when + ")", "(window.__pcPrev||0)>=2");
    }

    private static void key(AudioManager am, int code) {
        long t = SystemClock.uptimeMillis();
        am.dispatchMediaKeyEvent(new KeyEvent(t, t, KeyEvent.ACTION_DOWN, code, 0));
        am.dispatchMediaKeyEvent(new KeyEvent(t, SystemClock.uptimeMillis(), KeyEvent.ACTION_UP, code, 0));
    }

    private void expect(String what, String js) throws Exception {
        for (int i = 0; i < 60; i++) {
            if ("true".equals(eval(web, js))) return;
            SystemClock.sleep(100);
        }
        throw new AssertionError(what + " did nothing to the page's music (" + js + " never became true). "
                + sessionReport());
    }

    /** Which session Android routes media keys to, and what the app recorded about each press. */
    private static String sessionReport() {
        StringBuilder r = new StringBuilder();
        r.append("musicService=").append(MusicService.INSTANCE != null)
         .append(" sessionActive=").append(sessionActive())
         .append(" standby=").append(MusicService.standbySession)
         .append(" pageListening=").append(MusicService.pageListening())
         .append(" note=").append(MusicService.note).append(" presses=[");
        for (PressLog.Entry e : PressLog.recent()) r.append(e).append("; ");
        r.append("]");
        try {
            String d = shell("dumpsys media_session");
            int i = d.indexOf("Media button session");
            r.append(" | dumpsys: ").append(i >= 0 ? d.substring(i, Math.min(d.length(), i + 400))
                    : d.substring(0, Math.min(d.length(), 600)));
        } catch (Throwable ignored) { }
        return r.toString();
    }

    private static boolean sessionActive() {
        MusicService s = MusicService.INSTANCE;
        return s != null && s.sessionActive();
    }

    /** MediaControllerCompat(ctx, session.getSessionToken()).getTransportControls(), by reflection: the
     *  session is private to the service, and the local javac floor (tests/test_android_device_tests_compile.py)
     *  has no androidx.media on its classpath. CI's assembleDebugAndroidTest has the real classes. */
    private static Object controls(Context ctx) {
        try {
            MusicService s = MusicService.INSTANCE;
            if (s == null) return null;
            java.lang.reflect.Field f = MusicService.class.getDeclaredField("session");
            f.setAccessible(true);
            Object session = f.get(s);
            if (session == null) return null;
            Object token = session.getClass().getMethod("getSessionToken").invoke(session);
            Class<?> mc = Class.forName("android.support.v4.media.session.MediaControllerCompat");
            Object controller = mc.getConstructor(Context.class, token.getClass()).newInstance(ctx, token);
            return mc.getMethod("getTransportControls").invoke(controller);
        } catch (Throwable t) {
            return null;
        }
    }

    private static void transport(Object tc, String method) throws Exception {
        tc.getClass().getMethod(method).invoke(tc);
    }

    // ── playing a real track through the shipped player (same harness as MusicBackgroundDeviceTest) ─

    private void startPlaying() throws Exception {
        scenario = ActivityScenario.launch(MainActivity.class);
        AtomicReference<WebView> ref = new AtomicReference<WebView>();
        for (int i = 0; i < 80 && ref.get() == null; i++) {
            scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
            if (ref.get() == null) SystemClock.sleep(100);
        }
        assertTrue("MainActivity never created its WebView", ref.get() != null);
        web = ref.get();
        waitForClientPage(web);
        String start = "(()=>{const n=240000,b=new ArrayBuffer(44+n),v=new DataView(b);"
                + "const s=(o,x)=>{for(let i=0;i<x.length;i++)v.setUint8(o+i,x.charCodeAt(i))};"
                + "s(0,'RIFF');v.setUint32(4,36+n,true);s(8,'WAVEfmt ');v.setUint32(16,16,true);"
                + "v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);"
                + "v.setUint32(28,8000,true);v.setUint16(32,1,true);v.setUint16(34,8,true);"
                + "s(36,'data');v.setUint32(40,n,true);for(let i=44;i<44+n;i++)"
                + "v.setUint8(i,128+Math.round(32*Math.sin(2*Math.PI*440*(i-44)/8000)));"
                + "const A=window.Audio;window.Audio=function(...x){const a=new A(...x);"
                + "window.__pcBackgroundAudio=a;return a};"
                + "const p=__PC.MusicPlayer,idx=__PC.filesIdx();p.ensure();window.Audio=A;"
                + "idx.data.files['device-test-track']={name:'car-buttons-device-test.wav',"
                + "folder:'Music',enc:true,mime:'audio/wav'};p.cur='device-test-track';"
                + "p.queue=['device-test-track'];"
                // A skip must be PERFORMED by the page's own handler; the real next() would fetch and
                // decrypt a library track this device does not have, so it is counted instead.
                + "window.__pcNext=0;window.__pcPrev=0;"
                + "p.next=function(){window.__pcNext++};p.prev=function(){window.__pcPrev++};"
                + "const a=window.__pcBackgroundAudio;"
                + "a.src=URL.createObjectURL(new Blob([b],{type:'audio/wav'}));a.loop=true;"
                + "return a.play().then(()=>{p._media();return a.currentTime})})()";
        eval(web, start);
        double at = 0;
        for (int i = 0; i < 30 && at <= 0.15; i++) {
            SystemClock.sleep(100);
            at = number(eval(web, "window.__pcBackgroundAudio.currentTime"));
        }
        assertTrue("the injected track never began playing: " + at, at > 0.15);
        boolean up = false;
        for (int i = 0; i < 50 && !(up = sessionActive() && MusicService.nowPlaying()); i++) SystemClock.sleep(100);
        assertTrue("the PosterChanMusic session never became active for the playing track. " + sessionReport(), up);
    }

    private void assertAdvancing(String why) throws Exception {
        double a = number(eval(web, "window.__pcBackgroundAudio.currentTime"));
        SystemClock.sleep(900);
        double b = number(eval(web, "window.__pcBackgroundAudio.currentTime"));
        assertTrue(why + " (" + a + " -> " + b + ")", b > a + 0.4);
    }

    private static void bringBack(Context ctx) {
        ctx.startActivity(new Intent(ctx, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
        SystemClock.sleep(500);
    }

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
        for (int attempt = 0; ; attempt++) {
            CountDownLatch done = new CountDownLatch(1);
            AtomicReference<String> result = new AtomicReference<String>("null");
            web.post(() -> web.evaluateJavascript(js, value -> { result.set(value); done.countDown(); }));
            if (done.await(15, TimeUnit.SECONDS)) return result.get();
            if (attempt >= 1) throw new AssertionError("WebView did not answer JavaScript (twice, 15s each)");
        }
    }

    private static double number(String json) {
        try { return Double.parseDouble(json == null ? "0" : json.replace("\"", "")); }
        catch (Exception ignored) { return 0; }
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
}
