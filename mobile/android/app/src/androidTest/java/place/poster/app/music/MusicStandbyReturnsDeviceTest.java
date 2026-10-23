package place.poster.app.music;

import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;

import place.poster.app.RealUi;
import place.poster.app.push.StayAwakeService;

/**
 * BLUETOOTH AUTOPLAY AFTER THE PLAYER CLOSES. With "Bluetooth autoplay" on, StayAwakeService keeps a
 * STANDBY media session so a car has a media app to talk to (and a PLAY to route) before anything plays.
 * A real track stands it down -- two active sessions in one app let a head unit pick the wrong one. Its
 * own comment says the standby must be reopened "whenever a real one has come and gone", and nothing
 * did: openStandbySession() ran only from StayAwakeService.onStartCommand, so once the player closed
 * the car saw NO PosterChan media app until the app was next started from scratch.
 *
 * Measured on the emulator first (MusicCarButtonsDeviceTest went red on it: the player of the previous
 * case was still shutting down when the switch was turned on, and the standby never came back).
 */
@RunWith(AndroidJUnit4.class)
public class MusicStandbyReturnsDeviceTest {

    private PlayingPage page;

    @After
    public void tearDown() {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        if (page != null) page.close();
        try { ctx.startService(new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_STOP)); }
        catch (Throwable ignored) { }
        try { MusicService.setAutoplayBluetooth(ctx, false); } catch (Throwable ignored) { }
    }

    @Test
    public void theStandbySessionComesBackWhenThePlayerCloses() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        page = PlayingPage.start();
        // The person's own switch (Settings -> Phone -> Bluetooth autoplay), through the page: the page
        // re-sends its option on every load, so a value set natively before it would be overwritten.
        page.eval("__PC.MusicPlayer.setAutoplayBT(true)");
        RealUi.waitUntil("the autoplay switch never reached the phone", 5000, () -> MusicService.autoplayBluetooth(ctx));
        androidx.core.content.ContextCompat.startForegroundService(ctx,
                new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_START));
        SystemClock.sleep(1500);
        assertTrue("a standby session opened beside a playing track", !MusicService.standbySession);

        // Off again in the PAGE's storage only (the next test's page must not inherit it); the phone keeps
        // the switch on until @After, which is the state this case is about.
        page.eval("localStorage.setItem('pc_music_autoplay_bt','0')");
        page.close();
        page = null;
        assertTrue("THE PLAYER CLOSED AND THE STANDBY SESSION NEVER CAME BACK -- with Bluetooth autoplay on,"
                + " the car now sees no PosterChan media app at all until the app is restarted. note="
                + MusicService.note, within(8000, true));
    }

    private static boolean within(long ms, boolean want) {
        long end = SystemClock.uptimeMillis() + ms;
        while (SystemClock.uptimeMillis() < end) {
            if (MusicService.standbySession == want) return true;
            SystemClock.sleep(100);
        }
        return MusicService.standbySession == want;
    }
}
