package place.poster.app.sms;

import android.content.Context;
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
        // A TABLET IS NOT AN SMS APP THAT LOST AN ARGUMENT. With no telephony there is no default
        // messages app to be, and telling somebody to set one in Settings is advice they cannot take.
        boolean tel = false;
        try {
            tel = ctx.getPackageManager().hasSystemFeature(
                    android.content.pm.PackageManager.FEATURE_TELEPHONY);
        } catch (Throwable ignored) { }
        o.put("telephony", tel);
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
