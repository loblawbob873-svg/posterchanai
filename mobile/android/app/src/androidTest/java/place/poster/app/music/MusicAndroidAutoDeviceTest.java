package place.poster.app.music;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.content.res.XmlResourceParser;
import android.os.SystemClock;
import android.support.v4.media.MediaBrowserCompat;
import android.support.v4.media.session.MediaControllerCompat;
import android.view.KeyEvent;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.xmlpull.v1.XmlPullParser;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * "Media controls worked before Android Auto took over (ie skip songs)."
 *
 * Over plain Bluetooth the car's keys are AVRCP commands routed to the playing media session
 * (MusicCarButtonsDeviceTest covers that). ANDROID AUTO IS A DIFFERENT PATH: its media screen and the
 * steering wheel drive one media app, chosen from the apps that declare a MediaBrowserService and the
 * `com.google.android.gms.car.application` meta-data, through a MediaController built from the token that
 * browser hands it. The app declared neither, so under Android Auto nothing the driver pressed could
 * reach the player.
 *
 * Each test here does what Android Auto does, in its order -- find the browser through PackageManager,
 * connect to it, read the library root, take the session token from the BROWSER (never from the service
 * object) and press through that controller -- and judges every press by the page's own audio element,
 * the only thing that makes sound.
 */
@RunWith(AndroidJUnit4.class)
public class MusicAndroidAutoDeviceTest {

    private static final String BROWSE_ACTION = "android.media.browse.MediaBrowserService";
    private static final String CAR_META = "com.google.android.gms.car.application";

    private PlayingPage page;
    private MediaBrowserCompat browser;

    @After
    public void tearDown() {
        disconnect();
        if (page != null) page.close();
        Context ctx = ctx();
        try { ctx.startService(MusicService.intent(ctx, MusicService.ACTION_STOP)); } catch (Throwable ignored) { }
        for (int i = 0; i < 100 && MusicService.INSTANCE != null; i++) SystemClock.sleep(100);
    }

    /** What Android Auto reads before it will list the app at all. */
    @Test
    public void androidAutoCanFindTheAppAsAMediaApp() throws Exception {
        Context ctx = ctx();
        PackageManager pm = ctx.getPackageManager();
        List<ResolveInfo> found = pm.queryIntentServices(
                new Intent(BROWSE_ACTION).setPackage(ctx.getPackageName()), 0);
        assertTrue("no MediaBrowserService is declared -- Android Auto has nothing to bind, so under AA the "
                + "car's buttons can never reach the player", found != null && !found.isEmpty());
        assertEquals(MusicService.class.getName(), found.get(0).serviceInfo.name);
        assertTrue("the media browser is not exported -- Android Auto (another app) cannot bind it",
                found.get(0).serviceInfo.exported);

        ApplicationInfo ai = pm.getApplicationInfo(ctx.getPackageName(), PackageManager.GET_META_DATA);
        int res = ai.metaData == null ? 0 : ai.metaData.getInt(CAR_META, 0);
        assertTrue("no " + CAR_META + " meta-data -- Android Auto does not treat the app as a media app", res != 0);
        boolean media = false;
        try (XmlResourceParser x = ctx.getResources().getXml(res)) {
            for (int t = x.getEventType(); t != XmlPullParser.END_DOCUMENT; t = x.next()) {
                if (t == XmlPullParser.START_TAG && "uses".equals(x.getName())
                        && "media".equals(x.getAttributeValue(null, "name"))) media = true;
            }
        }
        assertTrue("the automotive descriptor does not declare <uses name=\"media\"/>", media);
    }

    /** The reported case: a track playing, the app in the background, presses from Android Auto. */
    @Test
    public void androidAutoSkipsPausesAndPlaysTheTrack() throws Exception {
        page = PlayingPage.start();
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
        SystemClock.sleep(1500);

        MediaControllerCompat.TransportControls tc = connectLikeAndroidAuto();
        List<MediaBrowserCompat.MediaItem> root = children();
        assertTrue("Android Auto's library is empty -- nothing to press", !root.isEmpty());
        assertTrue("the library row cannot be played", root.get(0).isPlayable());

        tc.skipToNext();
        page.expect("Android Auto's NEXT did not skip the song", "window.__pcNext>=1");
        tc.skipToPrevious();
        page.expect("Android Auto's PREVIOUS did nothing", "window.__pcPrev>=1");
        tc.pause();
        page.expect("Android Auto's PAUSE did not pause the music", "window.__pcBackgroundAudio.paused===true");
        tc.play();
        page.expect("Android Auto's PLAY did not resume the music", "window.__pcBackgroundAudio.paused===false");
        tc.pause();
        page.expect("second pause", "window.__pcBackgroundAudio.paused===true");
        tc.playFromMediaId(root.get(0).getMediaId(), null);
        page.expect("playing Android Auto's library row did not resume the music",
                "window.__pcBackgroundAudio.paused===false");
    }

    /**
     * Android Auto usually connects FIRST -- the car starts it before anything plays -- so the service is
     * created by a BIND, not a start. The page's first push then has to make it foreground on its own,
     * and Android Auto leaving (an unbind) must not take the playing session with it.
     */
    @Test
    public void musicStartedAfterAndroidAutoConnectedSurvivesAndroidAutoLeaving() throws Exception {
        connectLikeAndroidAuto();
        assertNotNull("binding the media browser did not create the music service", MusicService.INSTANCE);
        assertTrue("Android Auto's library is empty before anything played", !children().isEmpty());

        page = PlayingPage.start();
        assertTrue("the first push after Android Auto connected never made the service foreground "
                + "(no notification, and an unbind would destroy it) note=" + MusicService.note, foreground());

        disconnect();
        SystemClock.sleep(1500);
        assertNotNull("Android Auto disconnecting destroyed the music service", MusicService.INSTANCE);
        assertTrue("Android Auto disconnecting closed the media session",
                MusicService.INSTANCE.sessionActive());
        assertEquals("false", page.eval("window.__pcBackgroundAudio.paused"));

        // And plain Bluetooth keys still land after Android Auto has gone.
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
        SystemClock.sleep(1200);
        android.media.AudioManager am = (android.media.AudioManager) ctx().getSystemService(Context.AUDIO_SERVICE);
        am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_MEDIA_NEXT));
        am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_MEDIA_NEXT));
        page.expect("a Bluetooth NEXT after Android Auto left did not skip", "window.__pcNext>=1");
    }

    /** Another app sending the service its own intents (it is exported now) must change nothing. */
    @Test
    public void anotherAppCannotDriveTheServiceThroughItsExportedIntents() throws Exception {
        page = PlayingPage.start();
        Context ctx = ctx();
        // No in-process secret: what any other app on the phone could send.
        ctx.startService(new Intent(ctx, MusicService.class).setAction(MusicService.ACTION_DISMISS));
        ctx.startService(new Intent(ctx, MusicService.class).setAction(MusicService.ACTION_UPDATE)
                .putExtra(MusicService.EXTRA_TITLE, "spoofed").putExtra(MusicService.EXTRA_PLAYING, false));
        SystemClock.sleep(1500);
        assertNotNull("a foreign DISMISS closed the player", MusicService.INSTANCE);
        assertTrue("a foreign DISMISS closed the media session", MusicService.INSTANCE.sessionActive());
        assertTrue("a foreign UPDATE rewrote the now-playing title", !"spoofed".equals(MusicService.nowTitle()));
        assertEquals("false", page.eval("window.__pcBackgroundAudio.paused"));
    }

    // ---------------------------------------------------------------------------------------------

    private static Context ctx() { return InstrumentationRegistry.getInstrumentation().getTargetContext(); }

    /** PackageManager discovery -> connect -> the BROWSER's token -> a controller, as Android Auto does. */
    private MediaControllerCompat.TransportControls connectLikeAndroidAuto() throws Exception {
        Context ctx = ctx();
        List<ResolveInfo> found = ctx.getPackageManager().queryIntentServices(
                new Intent(BROWSE_ACTION).setPackage(ctx.getPackageName()), 0);
        assertTrue("no media browser to connect to (what Android Auto would find: nothing)",
                found != null && !found.isEmpty());
        ComponentName cn = new ComponentName(found.get(0).serviceInfo.packageName, found.get(0).serviceInfo.name);
        CountDownLatch connected = new CountDownLatch(1);
        AtomicReference<String> failed = new AtomicReference<>(null);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            browser = new MediaBrowserCompat(ctx, cn, new MediaBrowserCompat.ConnectionCallback() {
                @Override public void onConnected() { connected.countDown(); }
                @Override public void onConnectionFailed() { failed.set("refused"); connected.countDown(); }
                @Override public void onConnectionSuspended() { failed.set("suspended"); connected.countDown(); }
            }, null);
            browser.connect();
        });
        assertTrue("the media browser never answered a connection", connected.await(10, TimeUnit.SECONDS));
        assertTrue("the media browser refused the connection: " + failed.get(), failed.get() == null);
        AtomicReference<MediaControllerCompat> mc = new AtomicReference<>();
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() ->
                mc.set(new MediaControllerCompat(ctx, browser.getSessionToken())));
        return mc.get().getTransportControls();
    }

    private List<MediaBrowserCompat.MediaItem> children() throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<List<MediaBrowserCompat.MediaItem>> got = new AtomicReference<>();
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() ->
                browser.subscribe(browser.getRoot(), new MediaBrowserCompat.SubscriptionCallback() {
                    @Override public void onChildrenLoaded(String parentId, List<MediaBrowserCompat.MediaItem> c) {
                        got.set(c); done.countDown();
                    }
                    @Override public void onError(String parentId) { done.countDown(); }
                }));
        assertTrue("Android Auto's library never loaded", done.await(10, TimeUnit.SECONDS));
        assertNotNull("the library root answered an error", got.get());
        return got.get();
    }

    private void disconnect() {
        MediaBrowserCompat b = browser;
        browser = null;
        if (b == null) return;
        try { InstrumentationRegistry.getInstrumentation().runOnMainSync(b::disconnect); } catch (Throwable ignored) { }
    }

    private static boolean foreground() throws Exception {
        long end = SystemClock.uptimeMillis() + 5000;
        while (SystemClock.uptimeMillis() < end) {
            MusicService s = MusicService.INSTANCE;
            if (s != null) {
                java.lang.reflect.Field f = MusicService.class.getDeclaredField("foreground");
                f.setAccessible(true);
                if (f.getBoolean(s)) return true;
            }
            SystemClock.sleep(100);
        }
        return false;
    }
}
