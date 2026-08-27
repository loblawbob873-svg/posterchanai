package place.poster.app.sms;

import android.content.Context;
import android.content.Intent;
import android.provider.Telephony;
import android.util.Base64;
import android.os.Bundle;
import android.telephony.SmsManager;

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
        /* PICTURE MESSAGES, AND THE TWO HALVES OF THAT ARE NOT THE SAME ANSWER.
         *
         * `mms` — this build READS `content://mms`, so the history already on the phone (everything
         * ever sent, and everything received while another app was the default) is on the screen.
         * `mmsFetch` — whether an INCOMING picture message can be pulled off the carrier's MMSC.
         * MmsDeliverReceiver performs that download and MmsDownloadedReceiver announces the
         * completed provider row; this stays separate from merely reading old MMS history.
         *
         * One boolean for both is what lets a screen promise the second while delivering the first.
         * The client prints them separately, on the screen where somebody is deciding whether to
         * hand this app their messages. */
        o.put("mms", true);
        o.put("mmsFetch", true);
        /* `mmsRefused` IS DELIBERATELY NOT HERE. `MmsStore.refused()` describes THE LAST READ, and
         * `status` performs none — reported from here it is whatever some earlier call left behind,
         * which is a stale fact wearing a fresh one's clothes. It rides on `list` and `threads`,
         * read on the same thread immediately after the query it describes. */
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
        // THE TWO HALVES OF "PICTURE MESSAGES", so a report can say which one is missing. Reading
        // the phone's existing ones and FETCHING a new one off the carrier are different pieces of
        // work, and an older build answers neither key — which the client reads as "this build
        // cannot" rather than inventing a yes.
        o.put("mms", true);
        o.put("mmsFetch", true);
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
        /* WHETHER THE MODERN ASK IS EVEN AVAILABLE. `requestSms` prefers
         * `RoleManager.createRequestRoleIntent(ROLE_SMS)` and falls back to the legacy
         * ACTION_CHANGE_DEFAULT picker; which one it takes is decided by `isRoleAvailable`, and if
         * the role is not available the modern intent is never built and the legacy picker may find
         * nothing to offer. That is the shape of "can't check the setting box" with no message: an
         * ask that starts an activity which finishes immediately. Reported so the next reading says
         * which path was taken instead of me inferring it. */
        try {
            cap.put("roleAvailable", android.os.Build.VERSION.SDK_INT >= 29
                    && ((android.app.role.RoleManager) ctx.getSystemService(Context.ROLE_SERVICE))
                           .isRoleAvailable(android.app.role.RoleManager.ROLE_SMS));
        } catch (Throwable t) { cap.put("roleAvailable", "threw"); }
        // And whether this build could hold it at all, by the platform's own four-component rule.
        cap.put("canBeSms", place.poster.app.home.HomeRoles.canBeSms(ctx));
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

    /**
     * The newest messages, or everything after a timestamp — what the archive publishes.
     *
     * `refused` RIDES WITH EVERY READ, and it is the whole difference between the two ways this
     * screen can be empty. Without it the caller gets `[]` for "this phone has no texts" AND for
     * "the provider would not let me look", which is how "i see 0 of my sms messages in Text" drew
     * an empty list over a full inbox and offered nothing to do about it. `diagnose` could already
     * report this; the READ path could not, so the only surface that showed it was a panel nobody
     * opens until they have already decided the app is broken.
     *
     * Read immediately after the query, on this thread, because it describes the read that just
     * happened — a later read would overwrite it.
     */
    @PluginMethod
    public void list(PluginCall call) {
        long since = call.getLong("since", 0L);
        long before = call.getLong("before", 0L);
        int limit = call.getInt("limit", 500);
        /* BOTH PROVIDERS, INTERLEAVED — see Messages. A conversation is texts AND pictures and has
         * always been read as one thing; two lists is not a smaller version of that, it is a thread
         * with holes in it. */
        List<SmsMsg> rows = before > 0
                ? Messages.before(getContext(), before, limit)
                : (since > 0 ? Messages.since(getContext(), since, limit)
                             : Messages.recent(getContext(), limit));
        // READ IMMEDIATELY AFTER, on this thread, and SEPARATELY — the two tables fail
        // independently and folding them into one flag either blames a working half or hides a
        // refusal. Whichever is read second would otherwise overwrite the other's answer.
        boolean refused = SmsStore.refused();
        boolean mmsRefused = MmsStore.refused();
        // AND WHETHER THE PICTURE TABLE WAS TRUNCATED RATHER THAN EXHAUSTED. Read on this thread
        // with the other two and for the same reason -- see MmsStore.capped(). Without it a store
        // larger than the ceiling is indistinguishable from one that fitted, so the archive walks
        // the newest 2,000 picture messages, finds nothing left to do, and reports that it has
        // copied the phone.
        boolean mmsCapped = MmsStore.capped();
        JSObject o = new JSObject();
        o.put("messages", toJson(rows));
        o.put("refused", refused);
        o.put("mmsRefused", mmsRefused);
        o.put("mmsCapped", mmsCapped);
        call.resolve(o);
    }

    @PluginMethod
    public void threads(PluginCall call) {
        JSONArray arr = new JSONArray();
        List<SmsStore.Thread> found = Messages.threads(getContext(), call.getInt("limit", 500), true);
        boolean refused = SmsStore.refused();          // see list(): describes the read just made
        boolean mmsRefused = MmsStore.refused();
        for (SmsStore.Thread t : found) {
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
        out.put("refused", refused);
        out.put("mmsRefused", mmsRefused);
        call.resolve(out);
    }

    @PluginMethod
    public void thread(PluginCall call) {
        long id = call.getLong("id", 0L);
        JSObject o = new JSObject();
        o.put("messages", toJson(Messages.thread(getContext(), id, call.getInt("limit", 500))));
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
        String outbox = call.getString("outbox", "");
        if (!outbox.isEmpty() && !SmsOutbox.claim(getContext(), outbox)) {
            JSObject o = new JSObject();
            o.put("ok", false);
            o.put("claimed", false);
            o.put("error", "outbox request is already being handled");
            call.resolve(o);
            return;
        }
        SmsSender.Result r = SmsSender.send(getContext(), to, body);
        JSObject o = new JSObject();
        o.put("claimed", true);
        o.put("ok", r.ok);
        o.put("error", r.error);
        o.put("parts", r.parts);
        o.put("sentAt", r.sentAt);
        // FALSE means the radio was asked and the phone's OWN messages app has no copy — we lack the
        // role to write its store. The caller keeps its own copy and says so; it is not a failure.
        o.put("stored", r.stored);
        o.put("row", r.row == null ? "" : r.row.toString());
        call.resolve(o);
    }

    /** Send an image as a carrier MMS. The caller supplies plaintext only at this final phone hop. */
    @PluginMethod
    public void sendMms(PluginCall call) {
        String to = call.getString("to", "");
        String body = call.getString("body", "");
        String b64 = call.getString("data", "");
        String outbox = call.getString("outbox", "");
        JSObject o = new JSObject();
        if (to.trim().isEmpty() || b64.isEmpty()) {
            o.put("ok", false); o.put("error", "missing recipient or attachment"); call.resolve(o); return;
        }
        if (!outbox.isEmpty() && !SmsOutbox.claim(getContext(), outbox)) {
            o.put("ok", false); o.put("claimed", false);
            o.put("error", "outbox request is already being handled"); call.resolve(o); return;
        }
        try {
            byte[] raw = Base64.decode(b64, Base64.DEFAULT);
            SmsSender.Result r = MmsSender.send(getContext(), to, body, raw);
            o.put("claimed", true); o.put("ok", r.ok); o.put("error", r.error);
            o.put("sentAt", r.sentAt);
        } catch (Throwable t) {
            o.put("ok", false);
            o.put("error", t.getMessage() == null ? "could not send picture message" : t.getMessage());
        }
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
        long[] arr = ids(call.getArray("ids"));
        long[] mms = ids(call.getArray("mmsIds"));
        JSObject o = new JSObject();
        // TWO URIs, NOT ONE. A picture message is `content://mms/<id>`; handed to SmsStore it
        // deletes nothing and REPORTS nothing, which the client reads as a provider refusal — so
        // the message stays on the phone AND in the archive and the delete quietly did not happen.
        o.put("deleted", SmsStore.delete(getContext(), arr) + MmsStore.delete(getContext(), mms));
        call.resolve(o);
    }

    @PluginMethod
    public void deleteThread(PluginCall call) {
        JSObject o = new JSObject();
        o.put("deleted", SmsStore.deleteThread(getContext(), call.getLong("id", 0L)));
        call.resolve(o);
    }

    /**
     * HOW BIG AN MMS THIS CARRIER WILL ACTUALLY CARRY — asked, not guessed.
     *
     * Every published figure for this is folklore: 300KB, 600KB, 1MB, "about a megabyte". The real
     * number is per-carrier and the platform knows it, because the MMS stack has to. It sits in the
     * carrier config the same code path uses to send, so this is the same answer the transport will
     * apply -- rather than a constant compiled into an app that has never met this SIM.
     *
     * It matters because of what failure looks like. An oversized MMS is not refused with an error a
     * person can act on: the carrier's MMSC re-compresses it into mush, or accepts it and delivers
     * nothing, or the transaction times out minutes later with the message sitting in the thread
     * looking sent. Knowing the ceiling BEFORE sending is what lets the client offer a link instead,
     * which is a thing that works, rather than a photo that silently does not arrive.
     *
     * `measured` rides with the number for the usual reason: 300KB because AOSP says so and 300KB
     * because THIS carrier says so are the same integer and different facts, and only one of them is
     * worth overriding a person's choice with.
     */
    @PluginMethod
    public void mmsLimit(PluginCall call) {
        JSObject o = new JSObject();
        int bytes = 0;
        try {
            Bundle cfg = SmsManager.getDefault().getCarrierConfigValues();
            // "maxMessageSize" — SmsManager.MMS_CONFIG_MAX_MESSAGE_SIZE, which is @hide on some
            // builds, so the documented string constant is used rather than the symbol.
            if (cfg != null) bytes = cfg.getInt("maxMessageSize", 0);
        } catch (Throwable ignored) {
            // No SIM, no telephony, a tablet, an OEM that guards the config: all "we could not ask".
        }
        o.put("bytes", bytes > 0 ? bytes : DEFAULT_MMS_MAX);
        o.put("measured", bytes > 0);
        call.resolve(o);
    }

    /** Who a number belongs to, from the phone's whole address book — never a second contact store. */
    @PluginMethod
    public void nameFor(PluginCall call) {
        JSObject o = new JSObject();
        o.put("name", PhoneBook.nameOf(getContext(), call.getString("number", "")));
        call.resolve(o);
    }

    /**
     * ONE ATTACHMENT'S BYTES, base64, fetched when something is about to show it.
     *
     * THE THREE ANSWERS ARE DIFFERENT SENTENCES and the caller gets to say which: `data` present is
     * the picture; `tooBig` is an attachment that exists and will not fit through a JSON bridge;
     * anything else is an attachment the provider would not hand over. Collapsed into "no data" the
     * screen shows a broken image for all three, which is the drive-check rule again — "could not
     * ask" is never "there is nothing there".
     *
     * The cap is on the RAW bytes. Base64 is a third larger again, and the WebView holds a copy of
     * the string on top of that, so a generous-looking limit here is three times itself by the time
     * anything is drawn.
     */
    private static final int MAX_ATTACHMENT = 12 * 1024 * 1024;

    /**
     * What AOSP's own MmsConfig uses when nothing else says otherwise. A FLOOR TO FALL BACK ON, not
     * a belief about this network -- see mmsLimit(), which reports whether it had to be used.
     */
    private static final int DEFAULT_MMS_MAX = 300 * 1024;
    private static final int ATTACHMENT_CHUNK = 768 * 1024;

    @PluginMethod
    public void attachment(PluginCall call) {
        long id = call.getLong("part", 0L);
        long offset = call.getLong("offset", -1L);
        if (offset >= 0) {
            int want = Math.max(1, Math.min(call.getInt("max", ATTACHMENT_CHUNK), ATTACHMENT_CHUNK));
            JSObject o = new JSObject();
            o.put("part", id);
            o.put("offset", offset);
            byte[] b = id > 0 ? MmsStore.partChunk(getContext(), id, offset, want) : null;
            long total = id > 0 ? MmsStore.sizeOf(getContext(), id) : -1L;
            if (b == null) {
                o.put("data", ""); o.put("error", "provider refused attachment");
                o.put("total", total); o.put("done", false); call.resolve(o); return;
            }
            o.put("data", android.util.Base64.encodeToString(b, android.util.Base64.NO_WRAP));
            o.put("bytes", b.length);
            o.put("total", total);
            o.put("done", b.length < want || (total >= 0 && offset + b.length >= total));
            call.resolve(o);
            return;
        }
        int max = Math.min(call.getInt("max", MAX_ATTACHMENT), MAX_ATTACHMENT);
        JSObject o = new JSObject();
        o.put("part", id);
        byte[] b = id > 0 ? MmsStore.partBytes(getContext(), id, max) : null;
        if (b == null) {
            o.put("data", "");
            // WHICH KIND OF NOTHING. `partBytes` returns null both for "over the cap" and for "the
            // provider refused", and only one of those is worth offering a way around.
            o.put("tooBig", id > 0 && MmsStore.sizeOver(getContext(), id, max));
            call.resolve(o);
            return;
        }
        o.put("data", android.util.Base64.encodeToString(b, android.util.Base64.NO_WRAP));
        o.put("bytes", b.length);
        call.resolve(o);
    }

    /** Row ids out of a JS array, tolerating strings — a JS number over 2^53 arrives as one. */
    private long[] ids(JSArray a) {
        try {
            if (a == null) return new long[0];
            List<Object> raw = a.toList();
            long[] out = new long[raw.size()];
            for (int i = 0; i < raw.size(); i++) out[i] = Long.parseLong(String.valueOf(raw.get(i)));
            return out;
        } catch (Throwable ignored) {
            return new long[0];
        }
    }

    private JSONArray toJson(List<SmsMsg> rows) {
        JSONArray arr = new JSONArray();
        for (SmsMsg m : rows) {
            JSObject o = new JSObject();
            o.put("id", m.id);
            o.put("thread", m.threadId);
            o.put("address", m.address);
            /* WHO THAT NUMBER IS, RESOLVED HERE BECAUSE ONLY HERE CAN IT BE.
             *
             * The archive's own comment has promised this for a while -- "the contact's name is
             * resolved on the phone against the phone's OWN address book and carried, so a laptop,
             * which has no phone book, shows a name instead of a number" -- and the client's
             * fromRow duly reads `r.name`. Nothing ever put one here, so it read `undefined` on
             * every row and every message published from this handset reached every other device
             * carrying a bare number. The promise was kept in prose and in the reader, and broken
             * in the one place that had the answer.
             *
             * `nameOf`, not `label`: an unknown number must come back EMPTY so the client falls
             * back to its own formatting, where `label` would hand it the digits as though a person
             * were called that -- and those digits then travel into the archive as a name.
             * PhoneLookup is a cross-process query, so PhoneBook caches it per number; a thread
             * list resolves the same twenty numbers on every draw. */
            o.put("name", PhoneBook.nameOf(getContext(), m.address));
            o.put("body", m.body);
            o.put("date", m.date);
            o.put("type", m.type);
            o.put("incoming", m.incoming());
            o.put("read", m.read);
            o.put("mms", m.mms);
            // WHAT WAS ATTACHED — metadata only. The bytes come one at a time from `attachment`,
            // when something is actually about to show one: a thread of picture messages handed
            // over as base64 in a single JSON reply is tens of megabytes through the bridge.
            JSONArray parts = new JSONArray();
            for (SmsPart p : m.parts) {
                JSObject q = new JSObject();
                q.put("id", p.id);
                q.put("ct", p.ct);
                q.put("name", p.name);
                q.put("bytes", p.bytes);
                parts.put(q);
            }
            o.put("parts", parts);
            // The archive's address for this message, computed HERE so the phone and every other
            // device derive it from the same rule rather than from two copies of it.
            o.put("doc", m.docId());
            arr.put(o);
        }
        return arr;
    }
}
