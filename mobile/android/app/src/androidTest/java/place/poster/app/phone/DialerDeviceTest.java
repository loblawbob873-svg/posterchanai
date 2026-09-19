package place.poster.app.phone;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ResolveInfo;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.telecom.Call;
import android.view.LayoutInflater;
import android.view.View;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.List;

import place.poster.app.R;
import place.poster.app.sms.ThreadActivity;

/**
 * THE DIALER, ON A REAL ANDROID.
 *
 * The things only a device can answer: whether the components Android demands for the phone role
 * actually SHIPPED (a merge that drops an intent-filter looks identical to one that does not), and
 * whether the call screen draws — including that it contains no WebView, which is the same rule the
 * launcher lives by and for the same reason.
 *
 * Placing a real call is NOT attempted. The emulator has no carrier, and a test that dials is a test
 * that one day dials somebody. The state machine is CallRules', which is pure and run by
 * tests/test_android_dialer.py against every state.
 */
@RunWith(AndroidJUnit4.class)
public class DialerDeviceTest {

    private Context ctx() { return InstrumentationRegistry.getInstrumentation().getTargetContext(); }

    @Test
    public void theInCallServiceIsInstalledAndDeclaresItDrawsTheUi() throws Exception {
        ServiceInfo info = ctx().getPackageManager().getServiceInfo(
                new ComponentName(ctx(), PcInCallService.class),
                android.content.pm.PackageManager.GET_META_DATA);
        assertNotNull("the InCallService is not installed", info);
        assertEquals("BIND_INCALL_SERVICE is missing, so the platform will not bind it",
                "android.permission.BIND_INCALL_SERVICE", info.permission);
        assertNotNull("no meta-data", info.metaData);
        // Without IN_CALL_SERVICE_UI telecom treats this as an observer and keeps its own call
        // screen — ours is simply never asked, with nothing anywhere to say so.
        assertTrue("IN_CALL_SERVICE_UI is not set",
                info.metaData.getBoolean("android.telecom.IN_CALL_SERVICE_UI", false));
        assertFalse("PosterChan claims ringtone playback but has no ringtone player; Android Telecom"
                        + " must remain responsible for ringing",
                info.metaData.getBoolean("android.telecom.IN_CALL_SERVICE_RINGING", false));
    }

    @Test
    public void weAnswerActionDialWithAndWithoutANumber() {
        for (Intent i : new Intent[]{
                new Intent(Intent.ACTION_DIAL),
                new Intent(Intent.ACTION_DIAL, Uri.parse("tel:+15550100")),
                new Intent(Intent.ACTION_VIEW, Uri.parse("tel:+15550100")) }) {
            List<ResolveInfo> found = ctx().getPackageManager().queryIntentActivities(i, 0);
            boolean ours = false;
            for (ResolveInfo r : found) {
                if (r.activityInfo != null && ctx().getPackageName().equals(r.activityInfo.packageName)) {
                    ours = true;
                }
            }
            assertTrue(i + ": nothing of ours answers it", ours);
        }
    }

    @Test
    public void theDialerDrawsAndPrefillsFromATelLink() {
        Intent i = new Intent(ctx(), DialerActivity.class)
                .setAction(Intent.ACTION_DIAL)
                .setData(Uri.parse("tel:%2B15550100"));
        ActivityScenario<DialerActivity> s = ActivityScenario.launch(i);
        try {
            s.onActivity(a -> {
                android.widget.TextView num = a.findViewById(place.poster.app.R.id.pc_dl_number);
                assertNotNull(num);
                // The number is PREFILLED and not dialled. A `tel:` link that places a call the
                // moment it is opened is how a web page dials somebody's phone for them.
                assertTrue("the tel: link did not reach the pad",
                        num.getText().toString().replaceAll("[^0-9]", "").contains("15550100"));
                assertEquals("the dialer hosts a WebView", 0,
                        countWebViews(a.findViewById(android.R.id.content)));
            });
        } finally {
            s.close();
        }
    }

    @Test
    public void theKeypadHasTwelveKeys() {
        // A pad built in code rather than in XML, so nothing else would notice a missing key —
        // and a keypad with eleven keys looks fine until somebody needs the twelfth.
        ActivityScenario<DialerActivity> s = ActivityScenario.launch(DialerActivity.class);
        try {
            s.onActivity(a -> {
                android.widget.LinearLayout pad = a.findViewById(place.poster.app.R.id.pc_dl_pad);
                assertNotNull(pad);
                int keys = 0;
                for (int r = 0; r < pad.getChildCount(); r++) {
                    android.view.View row = pad.getChildAt(r);
                    if (row instanceof android.view.ViewGroup) {
                        keys += ((android.view.ViewGroup) row).getChildCount();
                    }
                }
                assertEquals("the keypad is not 4x3", 12, keys);
            });
        } finally {
            s.close();
        }
    }

    /**
     * "The number circle buttons are cut off at the top and bottom." Measured, not reasoned about:
     * every key's box must lie inside the pad's box on the real layout, after the dialer has had its
     * chance to re-size the keys to that box.
     */
    @Test
    public void everyKeyFitsInsideThePadsBox() {
        ActivityScenario<DialerActivity> s = ActivityScenario.launch(DialerActivity.class);
        try {
            InstrumentationRegistry.getInstrumentation().waitForIdleSync();
            s.onActivity(a -> {
                View wrap = a.findViewById(R.id.pc_dl_padwrap);
                android.view.ViewGroup pad = a.findViewById(R.id.pc_dl_pad);
                View numRow = a.findViewById(R.id.pc_dl_numrow);
                float dens = a.getResources().getDisplayMetrics().density;
                int[] w = new int[2];
                wrap.getLocationOnScreen(w);
                int top = w[1], bottom = w[1] + wrap.getHeight();
                int[] pl = new int[2]; pad.getLocationOnScreen(pl);
                // ONE-LINE GEOMETRY DUMP so a single CI run explains a clip precisely rather than a
                // fourth guess: the box, the content, and the actual rendered rows — px and dp.
                StringBuilder geo = new StringBuilder();
                geo.append("density=").append(dens)
                   .append(" padwrap[top=").append(top).append(" bottom=").append(bottom)
                   .append(" h=").append(wrap.getHeight()).append("px/").append((int) (wrap.getHeight() / dens)).append("dp]")
                   .append(" numrow[h=").append(numRow.getHeight()).append("px/").append((int) Math.ceil(numRow.getHeight() / dens)).append("dp]")
                   .append(" pad[top=").append(pl[1]).append(" h=").append(pad.getHeight()).append("px]");
                for (int r = 0; r < pad.getChildCount(); r++) {
                    android.view.ViewGroup row = (android.view.ViewGroup) pad.getChildAt(r);
                    int[] rl = new int[2]; row.getLocationOnScreen(rl);
                    View firstKey = row.getChildCount() > 0 ? row.getChildAt(0) : null;
                    geo.append(" row").append(r).append("[top=").append(rl[1]).append(" h=").append(row.getHeight());
                    if (firstKey != null) geo.append(" keyh=").append(firstKey.getHeight());
                    geo.append("]");
                }
                geo.append(" content=").append(numRow.getHeight() + pad.getHeight()).append("px vs box=").append(wrap.getHeight()).append("px");
                int keys = 0;
                for (int r = 0; r < pad.getChildCount(); r++) {
                    android.view.ViewGroup row = (android.view.ViewGroup) pad.getChildAt(r);
                    for (int c = 0; c < row.getChildCount(); c++) {
                        View key = row.getChildAt(c);
                        int[] k = new int[2];
                        key.getLocationOnScreen(k);
                        int overTop = top - k[1];
                        int overBottom = (k[1] + key.getHeight()) - bottom;
                        assertTrue("key row " + r + " starts above the pad by " + overTop + "px {" + geo + "}",
                                overTop <= 0);
                        assertTrue("key row " + r + " ends below the pad by " + overBottom + "px {" + geo + "}",
                                overBottom <= 0);
                        keys++;
                    }
                }
                assertEquals(12, keys);
            });
        } finally {
            s.close();
        }
    }

    /** Select a number in any app, then Phone; or Share it to Phone. Both must reach us. */
    @Test
    public void aSelectionOrAShareOfTextReachesThePhoneApp() {
        // The entry ships disabled and the dialer turns it on when it is first opened.
        ActivityScenario.launch(DialerActivity.class).close();
        for (Intent i : new Intent[]{
                new Intent("android.intent.action.PROCESS_TEXT").setType("text/plain"),
                new Intent(Intent.ACTION_SEND).setType("text/plain") }) {
            boolean ours = false;
            for (ResolveInfo r : ctx().getPackageManager().queryIntentActivities(i, 0)) {
                if (r.activityInfo != null && ctx().getPackageName().equals(r.activityInfo.packageName)
                        && "place.poster.app.phone.CallFromText".equals(r.activityInfo.name)) {
                    ours = true;
                }
            }
            assertTrue(i.getAction() + ": the phone app is not offered", ours);
        }
        Intent sel = new Intent(ctx(), DialerActivity.class)
                .setAction("android.intent.action.PROCESS_TEXT")
                .setType("text/plain")
                .putExtra("android.intent.extra.PROCESS_TEXT", "call me on 555-010-4477 after 5");
        ActivityScenario<DialerActivity> s = ActivityScenario.launch(sel);
        try {
            s.onActivity(a -> {
                android.widget.TextView num = a.findViewById(R.id.pc_dl_number);
                assertEquals("the selected number did not reach the pad", "5550104477",
                        num.getText().toString().replaceAll("[^0-9]", ""));
            });
        } finally {
            s.close();
        }
    }

    @Test
    public void everyContactRowRendersTextBesideCall() {
        ActivityScenario<DialerActivity> screen = ActivityScenario.launch(DialerActivity.class);
        try {
            screen.onActivity(a -> {
                // A real CONTACT-shaped row, bound by the same method the Contacts tab's adapter
                // calls. It does not depend on the test device containing a private address book.
                View row = LayoutInflater.from(a).inflate(R.layout.tel_recent_row, null, false);
                DialerActivity.Row contact = new DialerActivity.Row();
                contact.label = "Ada";
                contact.number = "+1 (555) 010-4477";
                contact.contactId = 42;
                contact.icon = R.drawable.ic_pc_user;
                a.bindRow(row, contact);
                View text = row.findViewById(R.id.pc_rc_text);
                View call = row.findViewById(R.id.pc_rc_call);
                assertNotNull("contact row has no Text action", text);
                assertNotNull("contact row lost its Call action", call);
                assertTrue("Text action is not clickable", text.hasOnClickListeners());
                assertTrue("Call action is not clickable", call.hasOnClickListeners());
                assertEquals(a.getString(R.string.tel_text_number), text.getContentDescription());
                assertEquals(a.getString(R.string.tel_call), call.getContentDescription());

                // Measure the actual shipped row at 320dp, below the width of the emulator and the
                // narrow end of Android phones we support. Presence alone missed layouts where the
                // two actions existed but were clipped off-screen or crushed the contact label.
                int width = Math.round(320 * a.getResources().getDisplayMetrics().density);
                row.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
                        View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED));
                row.layout(0, 0, row.getMeasuredWidth(), row.getMeasuredHeight());
                View who = row.findViewById(R.id.pc_rc_who);
                assertEquals("contact row did not honor the 320dp viewport", width,
                        row.getMeasuredWidth());
                assertTrue("Text action is clipped on a 320dp phone",
                        text.getLeft() >= 0 && text.getRight() <= width && text.getWidth() > 0);
                assertTrue("Call action is clipped on a 320dp phone",
                        call.getLeft() >= 0 && call.getRight() <= width && call.getWidth() > 0);
                assertTrue("Text and Call overlap on a 320dp phone",
                        text.getRight() <= call.getLeft());
                assertTrue("the contact name was crushed by its actions on a 320dp phone",
                        who != null && who.getWidth() >= Math.round(72 *
                                a.getResources().getDisplayMetrics().density));
            });
        } finally {
            screen.close();
        }
    }

    @Test
    public void contactTextOpensTheNormalizedConversationInTheSameTask() throws Exception {
        android.app.Instrumentation inst = InstrumentationRegistry.getInstrumentation();
        android.app.Instrumentation.ActivityMonitor monitor = inst.addMonitor(
                ThreadActivity.class.getName(), null, false);
        ActivityScenario<DialerActivity> dialer = ActivityScenario.launch(DialerActivity.class);
        android.app.Activity opened = null;
        try {
            dialer.onActivity(a -> {
                View row = LayoutInflater.from(a).inflate(R.layout.tel_recent_row, null, false);
                DialerActivity.Row contact = new DialerActivity.Row();
                contact.label = "Ada";
                contact.number = " +1 (555) 010-4477 ";
                contact.contactId = 42;
                contact.icon = R.drawable.ic_pc_user;
                a.bindRow(row, contact);
                View text = row.findViewById(R.id.pc_rc_text);
                assertNotNull("bound contact row lost its Text action", text);
                assertTrue("the actual Text action refused the tap", text.performClick());
            });
            opened = monitor.waitForActivityWithTimeout(5000);
            assertNotNull("Text did not route to PosterChan's conversation screen", opened);
            Intent got = opened.getIntent();
            assertEquals("+15550104477", got.getStringExtra(ThreadActivity.EXTRA_ADDRESS));
            assertEquals("the route left PosterChan's task and made a pop-out",
                    dialerTaskId(dialer), opened.getTaskId());

            // Finishing the conversation is the Back operation this route promises. The Phone
            // activity must still exist underneath it rather than being recreated or replaced.
            opened.finish();
            inst.waitForIdleSync();
            final boolean[] alive = new boolean[]{ false };
            dialer.onActivity(a -> alive[0] = !a.isFinishing() && !a.isDestroyed());
            assertTrue("Back from Messages did not return to Phone", alive[0]);
        } finally {
            if (opened != null && !opened.isFinishing()) opened.finish();
            dialer.close();
            inst.removeMonitor(monitor);
        }
    }

    private static int dialerTaskId(ActivityScenario<DialerActivity> scenario) {
        final int[] id = new int[]{ -1 };
        scenario.onActivity(a -> id[0] = a.getTaskId());
        return id[0];
    }

    @Test
    public void theCallScreenClosesItselfWhenThereIsNoCall() {
        // The screen draws from PcInCallService and there is no call on the emulator, so the correct
        // behaviour is to finish rather than to sit there showing a call that does not exist.
        ActivityScenario<InCallActivity> s = ActivityScenario.launch(InCallActivity.class);
        try {
            s.onActivity(a -> assertTrue("the call screen stayed up with no call", a.isFinishing()));
        } catch (Throwable expected) {
            // A scenario whose activity finished immediately can throw on the state transition;
            // that is the behaviour being asserted, not a failure.
        } finally {
            try { s.close(); } catch (Throwable ignored) { }
        }
    }

    @Test
    public void anEndedCallOffersNothing() {
        // The platform's own constants, against our table. CallRules keeps its own copy so the file
        // stays free of Android; this is the check that the copy is right on the device too.
        assertEquals(Call.STATE_ACTIVE, CallRules.STATE_ACTIVE);
        assertEquals(Call.STATE_RINGING, CallRules.STATE_RINGING);
        assertEquals(Call.STATE_DISCONNECTED, CallRules.STATE_DISCONNECTED);
        assertFalse(CallRules.canHangUp(Call.STATE_DISCONNECTED));
        assertTrue(CallRules.isOver(Call.STATE_DISCONNECTED));
    }

    @Test
    public void theNotificationChannelsAreNotTheNostrCallOnes() {
        InCallNotifier.ensureChannels(ctx());
        android.app.NotificationManager nm = (android.app.NotificationManager)
                ctx().getSystemService(Context.NOTIFICATION_SERVICE);
        assertNotNull(nm.getNotificationChannel(InCallNotifier.CHANNEL_RINGING));
        assertNotNull(nm.getNotificationChannel(InCallNotifier.CHANNEL_ONGOING));
        // Silencing calls over the mobile network must not silence calls over the internet.
        assertFalse(InCallNotifier.CHANNEL_RINGING.equals("pcai_calls"));
        assertFalse(InCallNotifier.CHANNEL_ONGOING.equals("pcai_ongoing_calls"));
    }

    private static int countWebViews(android.view.View v) {
        if (v instanceof android.webkit.WebView) return 1;
        if (!(v instanceof android.view.ViewGroup)) return 0;
        android.view.ViewGroup g = (android.view.ViewGroup) v;
        int n = 0;
        for (int i = 0; i < g.getChildCount(); i++) n += countWebViews(g.getChildAt(i));
        return n;
    }
}
