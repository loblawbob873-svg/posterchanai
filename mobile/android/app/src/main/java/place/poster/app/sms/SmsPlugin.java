package place.poster.app.sms;

import android.content.Context;
import android.content.Intent;
import android.provider.Telephony;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;

import org.json.JSONArray;

import java.util.List;

/**
 * THE APP'S WINDOW ONTO THE PHONE'S MESSAGES — read, send, delete, and be told when one arrives.
 *
 * IT IS A WINDOW, NOT A STORE. The system SMS provider is authoritative on the device (only the
 * default app may write it, and every other app and every backup reads it), and everything here
 * reads or writes THAT. What the client does on top is publish an encrypted per-message Nostr
 * document so a laptop can read and answer the same conversation — an archive across devices, never
 * a replacement. When the two disagree, the phone wins.
 *
 * That is also why `list` takes a TIMESTAMP rather than a row id: a row id is local to one phone, so
 * a restored backup renumbers every message and would republish the entire history.
 *
 * A DELETE IS TWO DELETES. This removes the phone's copy; sms.js removes the archive. Doing one
 * without the other means the next sync puts the message back, so the client owns the pairing and
 * says out loud which copies went.
 */
@CapacitorPlugin(
    name = "Sms",
    permissions = {
        @Permission(alias = "sms", strings = {
            "android.permission.READ_SMS",
            "android.permission.SEND_SMS",
            "android.permission.RECEIVE_SMS",
        }),
        // ITS OWN ALIAS, not folded in with the SMS three. A refusal of one must not be readable as
        // a refusal of the other: being unable to READ texts and being unable to ANNOUNCE one are
        // different failures with different fixes, and the screen has to be able to say which.
        @Permission(alias = "notify", strings = { "android.permission.POST_NOTIFICATIONS" }),
    }
)
public class SmsPlugin extends Plugin {

    /**
     * STATIC, like the music service's counters and for the same reason: an incoming message is
     * delivered to a BROADCAST RECEIVER, which frequently runs when there is no plugin instance and
     * no WebView at all. The receiver must be able to call this without caring, and the app finds
     * out only if it happens to be alive.
     */
    private static volatile SmsPlugin live;

    @Override
    public void load() { live = this; }

    @Override
    protected void handleOnDestroy() { if (live == this) live = null; }

    static void onIncoming(String from, String body, long when) {
        SmsPlugin p = live;
        if (p == null) return;
        JSObject o = new JSObject();
        o.put("address", from);
        o.put("body", body);
        o.put("date", when);
        p.notifyListeners("smsIn", o);
    }

    static void onSendResult(String row, boolean ok, int code) {
        SmsPlugin p = live;
        if (p == null) return;
        JSObject o = new JSObject();
        o.put("row", row);
        o.put("ok", ok);
        o.put("code", code);
        p.notifyListeners("smsSent", o);
    }

    /**
     * WHETHER ANDROID WILL ACTUALLY LET US READ THIS PHONE'S MESSAGES.
     *
     * A DANGEROUS PERMISSION IS NOT GRANTED BY BEING DECLARED, and being the default SMS app does not
     * grant it either — those are two separate switches and only one of them is ever offered by
     * Android on its own. The `@CapacitorPlugin(permissions = ...)` block above says which
     * permissions this plugin's "sms" alias covers; it does not ask for them. Nothing did. So every
     * read below was refused by the provider, `SmsStore.query` turned the refusal into an empty
     * list, and the Texts screen said "No messages on this phone" over a full inbox — reported as
     * "i see 0 of my sms messages in Text", and then "still missing a nice sms app on android".
     */
    private boolean mayRead() {
        try {
            return getPermissionState("sms") == com.getcapacitor.PermissionState.GRANTED
                || getContext().checkSelfPermission(android.Manifest.permission.READ_SMS)
                   == android.content.pm.PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            return false;
        }
    }

    /**
     * ASK FOR IT, from the screen that needs it.
     *
     * At the moment the person opens Texts, with the explanation already beside it — the same rule
     * as the contacts switch and the music notification, and the difference between a prompt that is
     * granted and one that is dismissed on reflex. A refusal is not an error: it resolves
     * `granted:false` and the screen says what is missing and offers the ask again.
     */
    /**
     * WHETHER A NEW TEXT CAN BE ANNOUNCED AT ALL.
     *
     * "make sure notifications work on new text messages ... otherwise useless", and it was exactly
     * that: on Android 13+ POST_NOTIFICATIONS is a runtime grant, `NotificationManager.notify` does
     * NOTHING without it, and nothing in the messages half ever asked. Music, screen sharing and
     * push each declare and request it for their own flows — so a person who had never opened the
     * player and never turned push on had never been asked, and every incoming text arrived in
     * silence with the message correctly stored and the screen correctly drawn.
     *
     * The channel being switched off by hand counts too, and is a different sentence: Android
     * granted it and the person muted it.
     */
    private boolean mayNotify() {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 33
                    && getContext().checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                       != android.content.pm.PackageManager.PERMISSION_GRANTED) return false;
            android.app.NotificationManager nm = (android.app.NotificationManager)
                    getContext().getSystemService(Context.NOTIFICATION_SERVICE);
            return nm == null || nm.areNotificationsEnabled();
        } catch (Throwable t) {
            return false;
        }
    }

    /** Ask for it, from the screen that needs it. A refusal resolves `granted:false`, not an error. */
    @PluginMethod
    public void ensureNotify(PluginCall call) {
        if (android.os.Build.VERSION.SDK_INT < 33 || mayNotify()) { finishNotify(call); return; }
        requestPermissionForAlias("notify", call, "notifyPermission");
    }

    @com.getcapacitor.annotation.PermissionCallback
    private void notifyPermission(PluginCall call) { finishNotify(call); }

    private void finishNotify(PluginCall call) {
        JSObject o = new JSObject();
        o.put("granted", mayNotify());
        call.resolve(o);
    }

    @PluginMethod
    public void ensureRead(PluginCall call) {
        if (mayRead()) { finishEnsure(call); return; }
        requestPermissionForAlias("sms", call, "smsPermission");
    }

    @com.getcapacitor.annotation.PermissionCallback
    private void smsPermission(PluginCall call) { finishEnsure(call); }

    private void finishEnsure(PluginCall call) {
        JSObject o = new JSObject();
        o.put("granted", mayRead());
        call.resolve(o);
    }

    @PluginMethod
    public void status(PluginCall call) {
        Context ctx = getContext();
        JSObject o = new JSObject();
        o.put("isDefault", HasRole.sms(ctx));
        // WHO ANDROID ACTUALLY NAMES, so the screen can state a fact instead of a verdict.
        // "android keeps saying posterchan is not the phones messaging app but I see all my texts"
        // is unanswerable from the app's side as long as the only thing reported is a boolean: it
        // could be a role that was never granted, a role granted in another profile, or a device
        // with no telephony at all. The package name tells the three apart in one line, and it is
        // the same measurement the boolean above is derived from, so the two cannot disagree.
        String cur = "";
        try { cur = Telephony.Sms.getDefaultSmsPackage(ctx); } catch (Throwable ignored) { }
        o.put("defaultPackage", cur == null ? "" : cur);
        o.put("package", ctx.getPackageName());
        // THE SECOND OPINION, REPORTED SEPARATELY. RoleManager and the legacy default-package row
        // are two different tables on Android 10+, and OEM builds do not always keep them in step.
        // Collapsing them into one boolean is what let the app tell somebody who had just set it as
        // their messages app that it was not; showing both means a disagreement is visible instead.
        o.put("roleHeld", HasRole.roleHeld(ctx));
        o.put("canNotify", mayNotify());
        // A TABLET IS NOT AN SMS APP THAT LOST AN ARGUMENT. With no telephony there is no default
        // messages app to be, and telling somebody to set one in Settings is advice they cannot take.
        o.put("telephony", HasRole.smsCapable(ctx));
        // THREE KINDS OF EMPTY, AND THEY ARE NOT THE SAME SENTENCE — the same distinction the native
        // ThreadListActivity draws. "you have no texts", "I am not allowed to read them" and "I can
        // read them but I am not the app that receives them" all rendered as one sentence, and the
        // middle one is the only one a tap can fix.
        o.put("canRead", mayRead());
        o.put("unread", SmsStore.unreadCount(ctx));
        // MMS IS NOT SUPPORTED and the client must be able to say so on the screen where somebody is
        // deciding whether to hand this app their messages. Reported rather than assumed, so the day
        // it IS supported nothing else has to change.
        o.put("mms", false);
        call.resolve(o);
    }

    /**
     * EVERY MEASUREMENT BEHIND "IT IS NOT WORKING AS MY MESSAGES APP", in one call.
     *
     * Four rounds were spent on that report without a device here, because from this side the
     * failure REPORTS SUCCESS: the role is set, the screen is drawn, and nothing throws. The four
     * components below are the ones Android demands before it will even OFFER the role — an app
     * missing one never appears in the picker and a role "granted" to it silently does nothing —
     * and the two role answers come from two different platform tables that OEM builds do not
     * always keep in step. Reported rather than judged: this returns what was asked and what came
     * back, and the screen prints it.
     */
    @PluginMethod
    public void diagnose(PluginCall call) {
        Context ctx = getContext();
        JSObject o = new JSObject();
        o.put("package", ctx.getPackageName());
        String cur = "";
        try { cur = Telephony.Sms.getDefaultSmsPackage(ctx); } catch (Throwable ignored) { }
        o.put("defaultPackage", cur == null ? "" : cur);
        o.put("roleHeld", HasRole.roleHeld(ctx));
        o.put("canRead", mayRead());
        o.put("canNotify", mayNotify());
        android.content.pm.PackageManager pm = ctx.getPackageManager();
        JSObject parts = new JSObject();
        parts.put("smsDeliver", resolvesReceiver(pm, new Intent(Telephony.Sms.Intents.SMS_DELIVER_ACTION)));
        Intent wap = new Intent(Telephony.Sms.Intents.WAP_PUSH_DELIVER_ACTION);
        wap.setType("application/vnd.wap.mms-message");
        parts.put("mmsDeliver", resolvesReceiver(pm, wap));
        parts.put("sendTo", resolvesActivity(pm, new Intent(Intent.ACTION_SENDTO,
                android.net.Uri.parse("smsto:+15550100"))));
        parts.put("respondViaMessage", resolvesService(pm, new Intent(
                "android.intent.action.RESPOND_VIA_MESSAGE",
                android.net.Uri.parse("smsto:+15550100"))));
        o.put("components", parts);
        // WHAT THE PROVIDER ACTUALLY ANSWERED, which is the only line that separates "no texts"
        // from "I was not allowed to look".
        int seen = -1;
        try { seen = SmsStore.recent(ctx, 5).size(); } catch (Throwable ignored) { }
        o.put("read", seen);
        o.put("refused", SmsStore.refused());
        // ALL THREE SIGNALS, RAW. "can this device do SMS" has been answered wrongly twice now, and
        // a single boolean cannot say which of them lied. Reported separately so the next report
        // settles it instead of starting another round.
        JSObject cap = new JSObject();
        cap.put("smsCapable", HasRole.smsCapable(ctx));
        try {
            android.telephony.TelephonyManager tm = (android.telephony.TelephonyManager)
                    ctx.getSystemService(Context.TELEPHONY_SERVICE);
            cap.put("isSmsCapable", tm != null && tm.isSmsCapable());
        } catch (Throwable t) { cap.put("isSmsCapable", "threw"); }
        try {
            cap.put("featureTelephony", ctx.getPackageManager().hasSystemFeature(
                    android.content.pm.PackageManager.FEATURE_TELEPHONY));
            cap.put("featureMessaging", android.os.Build.VERSION.SDK_INT >= 31
                    && ctx.getPackageManager().hasSystemFeature(
                           android.content.pm.PackageManager.FEATURE_TELEPHONY_MESSAGING));
        } catch (Throwable ignored) { }
        cap.put("sdk", android.os.Build.VERSION.SDK_INT);
        o.put("capability", cap);
        call.resolve(o);
    }

    private boolean resolvesReceiver(android.content.pm.PackageManager pm, Intent i) {
        return ours(pm.queryBroadcastReceivers(i, android.content.pm.PackageManager.MATCH_ALL));
    }

    private boolean resolvesActivity(android.content.pm.PackageManager pm, Intent i) {
        return ours(pm.queryIntentActivities(i, 0));
    }

    private boolean resolvesService(android.content.pm.PackageManager pm, Intent i) {
        return ours(pm.queryIntentServices(i, 0));
    }

    private boolean ours(java.util.List<android.content.pm.ResolveInfo> found) {
        if (found == null) return false;
        String mine = getContext().getPackageName();
        for (android.content.pm.ResolveInfo r : found) {
            String pkg = r.activityInfo != null ? r.activityInfo.packageName
                       : r.serviceInfo != null ? r.serviceInfo.packageName : null;
            if (mine.equals(pkg)) return true;
        }
        return false;
    }

    /** The newest messages, or everything after a timestamp — what the archive publishes. */
    @PluginMethod
    public void list(PluginCall call) {
        long since = call.getLong("since", 0L);
        int limit = call.getInt("limit", 500);
        List<SmsMsg> rows = since > 0
                ? SmsStore.since(getContext(), since, limit)
                : SmsStore.recent(getContext(), limit);
        JSObject o = new JSObject();
        o.put("messages", toJson(rows));
        call.resolve(o);
    }

    @PluginMethod
    public void threads(PluginCall call) {
        JSONArray arr = new JSONArray();
        for (SmsStore.Thread t : SmsStore.threads(getContext(), call.getInt("limit", 500))) {
            JSObject o = new JSObject();
            o.put("id", t.id);
            o.put("address", t.address);
            o.put("name", PhoneBook.label(getContext(), t.address));
            o.put("snippet", t.snippet);
            o.put("date", t.date);
            o.put("unread", t.unread);
            arr.put(o);
        }
        JSObject out = new JSObject();
        out.put("threads", arr);
        call.resolve(out);
    }

    @PluginMethod
    public void thread(PluginCall call) {
        long id = call.getLong("id", 0L);
        JSObject o = new JSObject();
        o.put("messages", toJson(SmsStore.thread(getContext(), id, call.getInt("limit", 500))));
        call.resolve(o);
    }

    /**
     * Send a text. Used by the app's own compose screen AND by a send another device asked for over
     * the archive — same path, so a message typed on a laptop is stored, sent and archived exactly
     * like one typed here.
     */
    @PluginMethod
    public void send(PluginCall call) {
        String to = call.getString("to", "");
        String body = call.getString("body", "");
        SmsSender.Result r = SmsSender.send(getContext(), to, body);
        JSObject o = new JSObject();
        o.put("ok", r.ok);
        o.put("error", r.error);
        o.put("parts", r.parts);
        o.put("row", r.row == null ? "" : r.row.toString());
        call.resolve(o);
    }

    @PluginMethod
    public void markRead(PluginCall call) {
        long id = call.getLong("id", 0L);
        int n = SmsStore.markRead(getContext(), id);
        SmsNotifier.clear(getContext(), id);
        JSObject o = new JSObject();
        o.put("marked", n);
        call.resolve(o);
    }

    /** Delete this phone's copies. The archive's copies are the client's half of the same delete. */
    @PluginMethod
    public void delete(PluginCall call) {
        JSArray ids = call.getArray("ids");
        long[] arr = new long[0];
        try {
            if (ids != null) {
                List<Object> raw = ids.toList();
                arr = new long[raw.size()];
                for (int i = 0; i < raw.size(); i++) arr[i] = Long.parseLong(String.valueOf(raw.get(i)));
            }
        } catch (Throwable ignored) { }
        JSObject o = new JSObject();
        o.put("deleted", SmsStore.delete(getContext(), arr));
        call.resolve(o);
    }

    @PluginMethod
    public void deleteThread(PluginCall call) {
        JSObject o = new JSObject();
        o.put("deleted", SmsStore.deleteThread(getContext(), call.getLong("id", 0L)));
        call.resolve(o);
    }

    /** Who a number belongs to, from the phone's whole address book — never a second contact store. */
    @PluginMethod
    public void nameFor(PluginCall call) {
        JSObject o = new JSObject();
        o.put("name", PhoneBook.nameOf(getContext(), call.getString("number", "")));
        call.resolve(o);
    }

    private JSONArray toJson(List<SmsMsg> rows) {
        JSONArray arr = new JSONArray();
        for (SmsMsg m : rows) {
            JSObject o = new JSObject();
            o.put("id", m.id);
            o.put("thread", m.threadId);
            o.put("address", m.address);
            o.put("body", m.body);
            o.put("date", m.date);
            o.put("type", m.type);
            o.put("incoming", m.incoming());
            o.put("read", m.read);
            // The archive's address for this message, computed HERE so the phone and every other
            // device derive it from the same rule rather than from two copies of it.
            o.put("doc", m.docId());
            arr.put(o);
        }
        return arr;
    }
}
