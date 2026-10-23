package place.poster.app.sms;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.provider.Telephony;
import android.service.notification.StatusBarNotification;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Assume;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

import place.poster.app.RealUi;

/**
 * A NEW TEXT -> ITS NOTIFICATION -> A FINGER ON IT -> THE RIGHT CONVERSATION, on a real Android.
 *
 * SmsNotifyDeviceTest proves a card reaches the shade. Nothing proved that TAPPING it opens the
 * conversation it is about -- and the ways that goes wrong are all silent: a PendingIntent whose request
 * code collides with another thread's (FLAG_UPDATE_CURRENT then rewrites the first card to open the
 * second person), an Activity that is already open on somebody else and keeps showing them
 * (singleTop + onNewIntent), or a tap that opens the right screen with the message not on it.
 *
 * THE MESSAGE IS A REAL ONE when the runner is handed the emulator console (scripts/emulator_tests_on_nas.sh
 * passes `pcEmuConsolePort`/`pcEmuConsoleToken`): `sms send` on the console makes the emulated modem
 * deliver a genuine SMS, through the radio stack, telephony and the SMS_DELIVER broadcast to the default
 * messages app. Without them (CI's gradle run) the same 3GPP DELIVER PDU a modem would hand up is built
 * here and given to the app's own SmsDeliverReceiver -- the real producer of the notification either way,
 * never a hand-made notification.
 */
@RunWith(AndroidJUnit4.class)
public class SmsNotificationTapDeviceTest {

    private Context ctx;
    private String previousDefault;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        previousDefault = Telephony.Sms.getDefaultSmsPackage(ctx);
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            RealUi.shell("pm grant " + ctx.getPackageName() + " android.permission.POST_NOTIFICATIONS");
        }
        RealUi.awake();
        clearOurs();
    }

    @After
    public void tearDown() {
        RealUi.shell("cmd statusbar collapse");
        RealUi.shell("input keyevent KEYCODE_HOME");
        clearOurs();
        if (previousDefault != null && !previousDefault.equals(Telephony.Sms.getDefaultSmsPackage(ctx))) {
            RealUi.shell("cmd role add-role-holder android.app.role.SMS " + previousDefault);
        }
    }

    @Test
    public void tappingTheNotificationOpensThatConversationWithTheMessageOnScreen() throws Exception {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        String from = number(), body = "pc tap " + stamp();
        receive(from, body);
        waitForCard(body);

        tapCardInShade(body);
        assertConversationShows(from, body);
        RealUi.waitUntil("opening the conversation left its notification in the shade", 8_000,
                () -> card(body) == null);
    }

    /** Two people text; the OLDER card is tapped. A request-code collision between the two threads'
     *  PendingIntents (FLAG_UPDATE_CURRENT) would open the newer person instead. */
    @Test
    public void withTwoSendersEachCardOpensItsOwnPerson() throws Exception {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        String a = number(), b = number();
        String bodyA = "pc first " + stamp(), bodyB = "pc second " + stamp();
        receive(a, bodyA);
        waitForCard(bodyA);
        receive(b, bodyB);
        waitForCard(bodyB);

        tapCardInShade(bodyA);
        assertConversationShows(a, bodyA);
        assertTrue("the card for the OTHER person disappeared when this one was opened",
                card(bodyB) != null);
    }

    /** The Messages screen is already open on one person when another person's text is tapped: it must
     *  move to that person (singleTop -> onNewIntent), not keep showing the first. */
    @Test
    public void aTapWhileAnotherConversationIsOpenSwitchesToTheTappedOne() throws Exception {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        String a = number(), b = number();
        String bodyA = "pc open " + stamp(), bodyB = "pc switch " + stamp();
        receive(a, bodyA);
        waitForCard(bodyA);
        tapCardInShade(bodyA);
        assertConversationShows(a, bodyA);

        receive(b, bodyB);
        waitForCard(bodyB);
        tapCardInShade(bodyB);
        assertConversationShows(b, bodyB);
        assertTrue("the conversation still shows the PREVIOUS person's message after switching",
                RealUi.find(RealUi.named(bodyA)) == null);
    }

    // ------------------------------------------------------------------ the finger

    private void tapCardInShade(String body) throws Exception {
        RealUi.shell("cmd statusbar expand-notifications");
        RealUi.tap("the notification for \"" + body + "\" in the shade", RealUi.named(body), 10_000);
    }

    private void assertConversationShows(String from, String body) throws Exception {
        final String digits = from.replaceAll("[^0-9]", "");
        final String last7 = digits.substring(digits.length() - 7);
        RealUi.waitUntil("the tapped notification did not open the conversation with " + from, 12_000, () -> {
            RealUi.Hit name = RealUi.find(RealUi.id("pc_th_name"));
            return name != null && (name.text + name.desc).replaceAll("[^0-9]", "").endsWith(last7);
        });
        RealUi.waitFor("the message \"" + body + "\" in the opened conversation", RealUi.named(body), 10_000);
    }

    // ------------------------------------------------------------------ a text arriving

    private void receive(String from, String body) throws Exception {
        Bundle args = InstrumentationRegistry.getArguments();
        String port = args.getString("pcEmuConsolePort");
        String token = args.getString("pcEmuConsoleToken");
        if (port != null && !port.isEmpty()) {
            console(Integer.parseInt(port), token, "sms send " + from.replace("+", "") + " " + body);
            android.util.Log.i("PosterChanTest", "sms: \"" + body + "\" sent through the emulator's modem (console)");
            return;
        }
        Intent i = new Intent(Telephony.Sms.Intents.SMS_DELIVER_ACTION)
                .putExtra("pdus", new Object[]{ deliverPdu(from, body) })
                .putExtra("format", "3gpp");
        new SmsDeliverReceiver().onReceive(ctx, i);
        android.util.Log.i("PosterChanTest", "sms: \"" + body + "\" handed to SmsDeliverReceiver as a 3GPP PDU (no console)");
    }

    /** The emulator console, reached from inside the guest (10.0.2.2 is the host's loopback). */
    private static void console(int port, String token, String cmd) throws Exception {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress("10.0.2.2", port), 5000);
            s.setSoTimeout(5000);
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));
            OutputStream out = s.getOutputStream();
            readUntilOk(in);                                         // the banner ends in "OK"
            if (token != null && !token.isEmpty() && !"none".equals(token)) {
                out.write(("auth " + token.trim() + "\r\n").getBytes("UTF-8"));
                readUntilOk(in);
            }
            out.write((cmd + "\r\n").getBytes("UTF-8"));
            readUntilOk(in);
            out.write("quit\r\n".getBytes("UTF-8"));
        }
    }

    private static void readUntilOk(BufferedReader in) throws Exception {
        StringBuilder seen = new StringBuilder();
        for (String line; (line = in.readLine()) != null; ) {
            seen.append(line).append('\n');
            if (line.startsWith("OK")) return;
            if (line.startsWith("KO")) throw new AssertionError("emulator console refused: " + seen);
        }
        throw new AssertionError("emulator console closed: " + seen);
    }

    /** A GSM 03.40 SMS-DELIVER PDU, 7-bit default alphabet -- what the modem hands to telephony. */
    static byte[] deliverPdu(String from, String body) {
        java.io.ByteArrayOutputStream p = new java.io.ByteArrayOutputStream();
        p.write(0x00);                                               // no SMSC in the PDU
        p.write(0x04);                                               // SMS-DELIVER, no more messages
        String d = from.replaceAll("[^0-9]", "");
        p.write(d.length());
        p.write(from.startsWith("+") ? 0x91 : 0x81);
        for (int i = 0; i < d.length(); i += 2) {
            int lo = d.charAt(i) - '0';
            int hi = i + 1 < d.length() ? d.charAt(i + 1) - '0' : 0xF;
            p.write((hi << 4) | lo);
        }
        p.write(0x00);                                               // PID
        p.write(0x00);                                               // DCS: GSM 7-bit
        java.util.Calendar c = java.util.Calendar.getInstance(java.util.TimeZone.getTimeZone("UTC"));
        int[] t = { c.get(java.util.Calendar.YEAR) % 100, c.get(java.util.Calendar.MONTH) + 1,
                c.get(java.util.Calendar.DAY_OF_MONTH), c.get(java.util.Calendar.HOUR_OF_DAY),
                c.get(java.util.Calendar.MINUTE), c.get(java.util.Calendar.SECOND), 0 };
        for (int v : t) p.write(((v % 10) << 4) | (v / 10));         // semi-octets, swapped
        p.write(body.length());                                      // septets
        int acc = 0, bits = 0;
        for (int i = 0; i < body.length(); i++) {
            acc |= (body.charAt(i) & 0x7f) << bits;                  // ASCII letters/digits/space = GSM
            bits += 7;
            while (bits >= 8) { p.write(acc & 0xff); acc >>>= 8; bits -= 8; }
        }
        if (bits > 0) p.write(acc & 0xff);
        return p.toByteArray();
    }

    // ------------------------------------------------------------------ the shade, read back

    private StatusBarNotification card(String body) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return null;
        for (StatusBarNotification s : nm.getActiveNotifications()) {
            CharSequence t = s.getNotification().extras.getCharSequence(android.app.Notification.EXTRA_TEXT);
            if (t != null && body.contentEquals(t)) return s;
        }
        return null;
    }

    private void waitForCard(String body) throws Exception {
        RealUi.waitUntil("a text was delivered and no notification for it appeared", 20_000,
                () -> card(body) != null);
        assertNotNull(card(body));
    }

    private void clearOurs() {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) try { nm.cancelAll(); } catch (Throwable ignored) { }
    }

    private boolean takeRole() {
        if (ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx))) return true;
        RealUi.shell("cmd role add-role-holder android.app.role.SMS " + ctx.getPackageName());
        return ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx));
    }

    private static int seq = 0;
    private static String number() {
        // A fresh conversation per case (and per run): NANP 555-01xx fictional numbers.
        return "+1555" + String.format(java.util.Locale.ROOT, "%07d",
                (int) ((System.currentTimeMillis() / 10 + (++seq) * 7919) % 10_000_000));
    }

    private static String stamp() {
        return Long.toString(System.currentTimeMillis() % 100_000_000, 36) + (++seq);
    }
}
