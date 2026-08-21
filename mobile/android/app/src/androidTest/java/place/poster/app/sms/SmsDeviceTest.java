package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Instrumentation;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.net.Uri;
import android.provider.Telephony;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Assume;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.List;

/**
 * THE MESSAGES APP, ON A REAL ANDROID.
 *
 * Two things here cannot be checked anywhere else, and both are the kind that reach a phone as
 * silence rather than as an error:
 *
 *  1. WHETHER THE ROLE IS GRANTABLE AT ALL. Android demands four components before it will offer an
 *     app as the default messages app, and an app that is missing one simply never appears in the
 *     picker. Reading the manifest proves it was written; asking the installed package manager to
 *     resolve each one proves it SHIPPED — a merge that drops an intent-filter looks identical
 *     otherwise.
 *  2. WHETHER A MESSAGE ACTUALLY LANDS IN THE PROVIDER. `content://sms` accepts a write from a
 *     non-default app and does nothing with it: the insert returns a URI and the row is not there.
 *     That is exactly the shape of "my texts stopped saving", and only a device shows it.
 *
 * The role is taken through the shell (UiAutomation runs as the shell UID) and GIVEN BACK in
 * @After — leaving the emulator's default messages app changed would affect every test after this
 * one on the same boot. If it cannot be taken, the tests that need it SKIP with a reason rather than
 * passing: a check that could not run is not a check that passed.
 */
@RunWith(AndroidJUnit4.class)
public class SmsDeviceTest {

    private Context ctx;
    private String previousDefault;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        previousDefault = Telephony.Sms.getDefaultSmsPackage(ctx);
    }

    @After
    public void tearDown() {
        if (previousDefault != null && !previousDefault.equals(Telephony.Sms.getDefaultSmsPackage(ctx))) {
            shellQuiet("cmd role add-role-holder android.app.role.SMS " + previousDefault);
        }
    }

    // ------------------------------------------------------------------ the role's four parts

    @Test
    public void theSmsDeliverReceiverIsInstalledAndReachable() {
        assertResolves(new Intent(Telephony.Sms.Intents.SMS_DELIVER_ACTION), true);
    }

    @Test
    public void theWapPushReceiverIsInstalledAndReachable() {
        Intent i = new Intent(Telephony.Sms.Intents.WAP_PUSH_DELIVER_ACTION);
        i.setType("application/vnd.wap.mms-message");
        assertResolves(i, true);
    }

    @Test
    public void theSendToActivityAnswersEveryMessageScheme() {
        for (String scheme : new String[]{ "sms", "smsto", "mms", "mmsto" }) {
            Intent i = new Intent(Intent.ACTION_SENDTO, Uri.parse(scheme + ":+15550100"));
            List<ResolveInfo> found = ctx.getPackageManager().queryIntentActivities(i, 0);
            assertTrue(scheme + ": nothing of ours answers it", ours(found));
        }
    }

    @Test
    public void theRespondViaMessageServiceIsInstalled() {
        Intent i = new Intent("android.intent.action.RESPOND_VIA_MESSAGE",
                              Uri.parse("smsto:+15550100"));
        List<ResolveInfo> found = ctx.getPackageManager().queryIntentServices(i, 0);
        assertTrue("no respond-via-message service", ours(found));
    }

    @Test
    public void theExportedReceiversAreGuardedByPlatformPermissions() throws Exception {
        // A signature permission the platform alone holds. Without it, any app on the phone could
        // put a message into somebody's inbox that looks exactly like a real one.
        for (String cls : new String[]{ "place.poster.app.sms.SmsDeliverReceiver",
                                        "place.poster.app.sms.MmsDeliverReceiver" }) {
            android.content.pm.ActivityInfo info = ctx.getPackageManager().getReceiverInfo(
                    new ComponentName(ctx, cls), 0);
            assertNotNull(cls + " is not installed", info);
            assertTrue(cls + " is exported with no permission",
                    info.permission != null && info.permission.startsWith("android.permission.BROADCAST_"));
        }
    }

    // ------------------------------------------------------------------ the provider

    @Test
    public void aStoredMessageIsReallyInThePhonesOwnStore() {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());

        long when = System.currentTimeMillis();
        String body = "pc-device-test-" + when;
        String from = "+15550100";
        Uri row = SmsStore.storeInbox(ctx, from, body, when, 0);
        assertNotNull("the provider refused the insert", row);
        try {
            List<SmsMsg> back = SmsStore.recent(ctx, 50);
            SmsMsg found = null;
            for (SmsMsg m : back) if (body.equals(m.body)) found = m;
            // THE ASSERTION THIS TEST EXISTS FOR. A non-default app's insert returns a URI and
            // stores nothing; only reading it back tells the two apart.
            assertNotNull("the message was accepted and not stored", found);
            assertTrue("stored as outgoing", found.incoming());
            assertFalse("stored as already read", found.read);
            assertEquals("the archive address is not stable across a round trip",
                    SmsKeys.docId(from, when, body, true), found.docId());

            assertTrue("marking read changed nothing", SmsStore.markRead(ctx, found.threadId) >= 1);

            assertEquals("the delete did not remove it", 1,
                    SmsStore.delete(ctx, new long[]{ found.id }));
            for (SmsMsg m : SmsStore.recent(ctx, 50)) {
                assertFalse("it came back", body.equals(m.body));
            }
            row = null;
        } finally {
            if (row != null) try { ctx.getContentResolver().delete(row, null, null); } catch (Throwable ignored) { }
        }
    }

    @Test
    public void aThreadIdIsMintedForANumber() {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        long id = SmsStore.threadIdFor(ctx, "+15550111");
        assertTrue("no thread id", id > 0);
        assertEquals("the same number minted two threads", id,
                SmsStore.threadIdFor(ctx, "+1 555 0111"));
    }

    @Test
    public void sendingRefusesWhenWeAreNotTheDefaultApp() {
        // Deliberately WITHOUT taking the role. A non-default app can still reach SmsManager and may
        // not write the provider, so the message would be sent and then be missing from the thread
        // it was sent in — which reads as "it didn't send".
        Assume.assumeFalse("this device already has us as the default",
                Telephony.Sms.getDefaultSmsPackage(ctx).equals(ctx.getPackageName()));
        SmsSender.Result r = SmsSender.send(ctx, "+15550100", "should not go out");
        assertFalse("a non-default app sent a message", r.ok);
        assertTrue("it failed without saying why", r.error.length() > 0);
    }

    @Test
    public void theNotificationChannelIsItsOwnAndNotTheCallRinger() {
        // Sharing a channel with `pcai_calls` would mean somebody silencing their texts also
        // silences incoming calls, with no way back except uninstalling the app.
        SmsNotifier.ensureChannel(ctx);
        android.app.NotificationManager nm =
                (android.app.NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        assertNotNull(nm);
        assertNotNull("the SMS channel was not created", nm.getNotificationChannel(SmsNotifier.CHANNEL));
        assertFalse("the SMS channel is the call ringer", SmsNotifier.CHANNEL.equals("pcai_calls"));
    }

    // ------------------------------------------------------------------ plumbing

    private boolean takeRole() {
        if (ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx))) return true;
        shellQuiet("cmd role add-role-holder android.app.role.SMS " + ctx.getPackageName());
        if (ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx))) return true;
        // Pre-29 devices and some images have no `cmd role`; the old secure setting still works.
        shellQuiet("settings put secure sms_default_application " + ctx.getPackageName());
        return ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx));
    }

    private void assertResolves(Intent i, boolean receiver) {
        List<ResolveInfo> found = receiver
                ? ctx.getPackageManager().queryBroadcastReceivers(i, PackageManager.MATCH_ALL)
                : ctx.getPackageManager().queryIntentActivities(i, 0);
        assertTrue(i.getAction() + ": nothing of ours answers it", ours(found));
    }

    private boolean ours(List<ResolveInfo> found) {
        if (found == null) return false;
        for (ResolveInfo r : found) {
            String pkg = r.activityInfo != null ? r.activityInfo.packageName
                       : r.serviceInfo != null ? r.serviceInfo.packageName : null;
            if (ctx.getPackageName().equals(pkg)) return true;
        }
        return false;
    }

    private static void shellQuiet(String cmd) {
        try { shell(cmd); } catch (Throwable ignored) { }
    }

    private static String shell(String cmd) throws Exception {
        Instrumentation in = InstrumentationRegistry.getInstrumentation();
        try (InputStream is = new android.os.ParcelFileDescriptor.AutoCloseInputStream(
                in.getUiAutomation().executeShellCommand(cmd))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    // ------------------------------------------------------------- a whole conversation, on a real
    //                                                               provider

    /**
     * THE REPORTED FAULT, REPRODUCED RATHER THAN REASONED ABOUT.
     *
     * "all replies I made to 2 people are not there", "i see replies i made to my dad at jul 2,
     * nothing after which is today". Four builds went out against theories about why, each argued
     * from a description of a screen. This puts both halves of a conversation into a real provider
     * and reads the conversation back the way the app does, so the answer is measured on an Android
     * instead of inferred from here.
     */
    @Test
    public void aConversationShowsBothHalvesOfItself() {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());

        long now = System.currentTimeMillis();
        String them = "+15550188";
        String theirs = "pc-in-" + now;
        String mine = "pc-out-" + now;

        Uri a = SmsStore.storeInbox(ctx, them, theirs, now - 2000, 0);
        assertNotNull("the provider refused the incoming message", a);
        // Stored the way a reply is: no thread id known, address as the app holds it.
        Uri b = SmsStore.storeSent(ctx, them, mine, now - 1000, Telephony.Sms.MESSAGE_TYPE_SENT);
        assertNotNull("the provider refused the outgoing message", b);
        try {
            List<SmsStore.Thread> threads = Messages.threads(ctx, 200, false);
            SmsStore.Thread found = null;
            for (SmsStore.Thread t : threads) {
                for (SmsMsg m : Messages.thread(ctx, t.ids, 200)) {
                    if (theirs.equals(m.body) || mine.equals(m.body)) { found = t; break; }
                }
                if (found != null) break;
            }
            assertNotNull("neither message appears in any conversation the app lists", found);

            List<SmsMsg> rows = Messages.thread(ctx, found.ids, 200);
            boolean sawTheirs = false, sawMine = false;
            for (SmsMsg m : rows) {
                if (theirs.equals(m.body)) sawTheirs = true;
                if (mine.equals(m.body)) sawMine = true;
            }
            assertTrue("their message is missing from the conversation", sawTheirs);
            // The one that was actually wrong on the phone.
            assertTrue("MY OWN REPLY IS MISSING FROM THE CONVERSATION", sawMine);

            // And it is ONE conversation, not one per direction.
            int holding = 0;
            for (SmsStore.Thread t : threads) {
                for (SmsMsg m : Messages.thread(ctx, t.ids, 200)) {
                    if (theirs.equals(m.body) || mine.equals(m.body)) { holding++; break; }
                }
            }
            assertEquals("one person, more than one conversation", 1, holding);
        } finally {
            try { ctx.getContentResolver().delete(a, null, null); } catch (Throwable ignored) { }
            try { ctx.getContentResolver().delete(b, null, null); } catch (Throwable ignored) { }
        }
    }

    /**
     * DOES A DIFFERENT SPELLING OF THE SAME NUMBER MINT A SECOND CONVERSATION? Asserted as fact in
     * a commit message here; never measured. The answer decides whether a reply may be stored
     * against a resolved address at all, so it is asked of the platform rather than assumed either
     * way -- `aThreadIdIsMintedForANumber` only ever tried spellings that differ by whitespace.
     */
    @Test
    public void aCountryCodeIsNotASecondConversation() {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        long withCode = SmsStore.threadIdFor(ctx, "+15550199");
        long without = SmsStore.threadIdFor(ctx, "5550199");
        assertTrue("no thread id", withCode > 0 && without > 0);
        assertEquals("the same person has two conversations, one per spelling of their number",
                withCode, without);
    }

    /**
     * THE PROVIDER'S CONVERSATION LIST IS THE ONE THE APP SHOWS. Fossify Messages reads
     * `content://mms-sms/conversations?simple=true` and was right on a phone where this app was
     * wrong; folding the list out of the messages is the fallback. If the provider answers at all,
     * that is what must reach the screen.
     */
    @Test
    public void theConversationListComesFromTheProvider() {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        long now = System.currentTimeMillis();
        Uri a = SmsStore.storeInbox(ctx, "+15550177", "pc-list-" + now, now, 0);
        assertNotNull("the provider refused the insert", a);
        try {
            assertFalse("the provider's own conversation table came back empty",
                    SmsStore.platformThreads(ctx, 200, false).isEmpty());
        } finally {
            try { ctx.getContentResolver().delete(a, null, null); } catch (Throwable ignored) { }
        }
    }
}
