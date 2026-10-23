package place.poster.app.phone;

import static org.junit.Assert.assertEquals;

import android.content.Context;
import android.content.Intent;
import android.graphics.Rect;
import android.telecom.TelecomManager;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Assume;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import place.poster.app.RealUi;

/**
 * THE PHONE APP, PRESSED. DialerDeviceTest counts the keys and measures their boxes; this one dials
 * with them -- a real touch on each digit as drawn, the number read back off the screen -- and then
 * places a REAL call on the emulator's modem with the green button and hangs it up with the red one on
 * PosterChan's own in-call screen.
 *
 * The call half needs the phone's DIALER role: only the default dialer's InCallService is bound by
 * telecom, i.e. only then is PosterChan's call screen the one a call actually shows. Taken through the
 * shell and handed back afterwards; a device that refuses it SKIPS that case with a reason.
 */
@RunWith(AndroidJUnit4.class)
public class DialerTapDeviceTest {

    private Context ctx;
    private String previousDialer;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        TelecomManager tm = (TelecomManager) ctx.getSystemService(Context.TELECOM_SERVICE);
        previousDialer = tm == null ? null : tm.getDefaultDialerPackage();
        RealUi.awake();
        open();
    }

    @After
    public void tearDown() {
        TelecomManager tm = (TelecomManager) ctx.getSystemService(Context.TELECOM_SERVICE);
        try { if (tm != null && tm.isInCall()) RealUi.shell("input keyevent KEYCODE_ENDCALL"); } catch (Throwable ignored) { }
        RealUi.shell("input keyevent KEYCODE_HOME");
        if (previousDialer != null && tm != null && !previousDialer.equals(tm.getDefaultDialerPackage())) {
            RealUi.shell("cmd role add-role-holder android.app.role.DIALER " + previousDialer);
        }
    }

    @Test
    public void typingOnTheKeypadPutsTheDigitsOnScreenAndBackspaceTakesOneOff() throws Exception {
        type("5550100");
        assertEquals("the keypad's digits are not what the screen shows", "5550100", shown());

        RealUi.tap("the backspace key", RealUi.id("pc_dl_back"), 5_000);
        RealUi.waitUntil("backspace did not take ONE digit off", 5_000, () -> "555010".equals(shown()));

        // Holding the backspace clears the whole number.
        RealUi.hold(RealUi.waitFor("the backspace key", RealUi.id("pc_dl_back"), 5_000), 900);
        RealUi.waitUntil("holding backspace did not clear the number", 5_000, () -> shown().isEmpty());
    }

    /** "+" is only reachable by HOLDING zero, and it is the only way to type an international number. */
    @Test
    public void holdingZeroTypesAPlus() throws Exception {
        RealUi.hold(key("0"), 900);
        RealUi.waitUntil("holding 0 did not type +", 5_000, () -> numberText().startsWith("+"));
        type("15550100");
        assertEquals("+15550100", "+" + shown());
    }

    /**
     * THE KEYPAD MUST NOT MOVE UNDER THE FINGER. Typing filters the recent calls as you go, and on the
     * KEYPAD tab the list tabs' empty state ("No recent calls") was switched on and off with every
     * digit that went from matching a recent call to matching none. That line sits in the same column
     * as the pad, so the pad jumped by its height -- and on a short phone the keys were RE-SIZED and
     * rebuilt, so a press landing mid-rebuild was lost (measured on the CI-shaped 320x640 emulator: the
     * seventh digit of 555-0100 never arrived). A phone with any call history does this on every number.
     */
    @Test
    public void theKeypadDoesNotMoveUnderTheFingerWhileTyping() throws Exception {
        android.net.Uri row = recentCall("+15550177");
        try {
            open();
            Rect before = key("1").bounds;
            for (char c : "555019".toCharArray()) {
                type(String.valueOf(c));
                // Observe the screen a moment after each press: the filtering (and any re-layout) runs
                // asynchronously behind the digit, which is exactly when the next finger lands.
                long end = android.os.SystemClock.uptimeMillis() + 700;
                while (android.os.SystemClock.uptimeMillis() < end) {
                    Rect now = key("1").bounds;
                    if (!now.equals(before)) {
                        throw new AssertionError("typing '" + c + "' MOVED THE KEYPAD under the finger: key 1 was at "
                                + before.toShortString() + ", now " + now.toShortString() + ". On screen: " + RealUi.describe());
                    }
                    android.os.SystemClock.sleep(100);
                }
            }
        } finally {
            if (row != null) try { ctx.getContentResolver().delete(row, null, null); } catch (Throwable ignored) { }
        }
    }

    /** One entry in the phone's own call log, so typing has something to match and then stop matching. */
    private android.net.Uri recentCall(String number) {
        android.content.ContentValues v = new android.content.ContentValues();
        v.put(android.provider.CallLog.Calls.NUMBER, number);
        v.put(android.provider.CallLog.Calls.TYPE, android.provider.CallLog.Calls.OUTGOING_TYPE);
        v.put(android.provider.CallLog.Calls.DATE, System.currentTimeMillis());
        v.put(android.provider.CallLog.Calls.DURATION, 12);
        v.put(android.provider.CallLog.Calls.NEW, 0);
        try {
            android.net.Uri u = ctx.getContentResolver().insert(android.provider.CallLog.Calls.CONTENT_URI, v);
            Assume.assumeTrue("could not write the call log on this device", u != null);
            return u;
        } catch (SecurityException e) {
            Assume.assumeTrue("no WRITE_CALL_LOG on this install: " + e.getMessage(), false);
            return null;
        }
    }

    @Test
    public void theGreenButtonPlacesARealCallAndTheRedOneOnOurCallScreenEndsIt() throws Exception {
        Assume.assumeTrue("could not make PosterChan this phone's dialer", takeDialerRole());
        // The role changes which activities answer; start from a fresh Phone screen.
        open();
        type("5550123");
        RealUi.tap("the call button", RealUi.id("pc_dl_call"), 5_000);

        final TelecomManager tm = (TelecomManager) ctx.getSystemService(Context.TELECOM_SERVICE);
        RealUi.waitUntil("pressing call placed no call on the modem", 15_000, tm::isInCall);
        // The number is drawn as the NAME when no contact matches it (the number line then stays empty).
        RealUi.waitUntil("PosterChan's in-call screen never showed the number that was dialled", 15_000, () -> {
            RealUi.Hit name = RealUi.find(RealUi.id("pc_ic_name"));
            RealUi.Hit num = RealUi.find(RealUi.id("pc_ic_number"));
            String shown = (name == null ? "" : name.text) + "|" + (num == null ? "" : num.text);
            return shown.replaceAll("[^0-9|]", "").contains("5550123");
        });

        RealUi.tap("the end-call button on the call screen", RealUi.id("pc_ic_end"), 5_000);
        RealUi.waitUntil("the red button did not end the call", 15_000, () -> !tm.isInCall());
        RealUi.waitUntil("the call screen stayed up after the call ended", 10_000,
                () -> RealUi.find(RealUi.id("pc_ic_end")) == null);
    }

    // ------------------------------------------------------------------ the pad

    private void open() {
        ctx.startActivity(new Intent(ctx, DialerActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
        RealUi.waitFor("the Phone app's number field", RealUi.id("pc_dl_number"), 10_000);
        RealUi.waitFor("the keypad", RealUi.id("pc_dl_pad"), 10_000);
    }

    private RealUi.Hit key(String digit) {
        Rect pad = RealUi.waitFor("the keypad", RealUi.id("pc_dl_pad"), 5_000).bounds;
        return RealUi.waitFor("key " + digit + " on the keypad", RealUi.inside(pad, RealUi.named(digit)), 5_000);
    }

    private void type(String digits) throws Exception {
        String before = shown();
        for (int i = 0; i < digits.length(); i++) {
            RealUi.tap(key(String.valueOf(digits.charAt(i))));
            final String want = before + digits.substring(0, i + 1);
            RealUi.waitUntil("pressing " + digits.charAt(i) + " did not add it to the number", 5_000,
                    () -> want.equals(shown()));
        }
    }

    private String numberText() {
        RealUi.Hit n = RealUi.find(RealUi.id("pc_dl_number"));
        return n == null ? "" : n.text;
    }

    private String shown() { return numberText().replaceAll("[^0-9]", ""); }

    private boolean takeDialerRole() {
        TelecomManager tm = (TelecomManager) ctx.getSystemService(Context.TELECOM_SERVICE);
        if (tm == null) return false;
        if (ctx.getPackageName().equals(tm.getDefaultDialerPackage())) return true;
        RealUi.shell("cmd role add-role-holder android.app.role.DIALER " + ctx.getPackageName());
        return ctx.getPackageName().equals(tm.getDefaultDialerPackage());
    }
}
