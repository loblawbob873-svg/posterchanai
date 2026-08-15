package place.poster.app.signer;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

import place.poster.app.MainActivity;
import place.poster.app.R;
import place.poster.app.RunningNote;

/**
 * The remote signer, answered by the PROCESS instead of by the page.
 *
 * THE BUG THIS EXISTS FOR, stated plainly: "I have to wake the phone for events to actually send
 * from desktop." The signer was a WebSocket owned by JavaScript inside the WebView. Chromium
 * throttles a hidden WebView's timers to roughly one a minute and harder still after five, so when
 * that socket dropped — a NAT timeout, a cell handover, a relay restart — the reconnect that would
 * have fixed it was throttled with everything else. Nothing errored. The desktop simply sat on
 * "waiting for the signer to approve" until the screen came on, at which point the page unthrottled,
 * redialled, and the backlog arrived at once, which is exactly what "I have to wake the phone" looks
 * like from the other end.
 *
 * `StayAwakeService` could not fix it and was never meant to: it keeps the PROCESS off the freezer so
 * "the WebView keeps its relay socket" — a promise about the process, not about the renderer's
 * timers. That is why turning it on ("daemon mode") did not help, which was reported and is the clue
 * that dates the diagnosis.
 *
 * AMBER WORKS BECAUSE IT IS NATIVE, and that is the entire difference — not a better relay, not a
 * cleverer subscription. So this is a plain Android foreground service holding its own OkHttp
 * WebSocket, signing with the Keystore-sealed key through the same `Nostr`/`Crypt` the NIP-55
 * activity already uses. No WebView is involved in answering a request, so no renderer policy can
 * stop it. If the app's page is closed, killed for memory, or never opened since boot, the signer
 * still answers.
 *
 * WHAT IT COSTS, because a signer nobody can afford to leave on is not a signer. One socket per
 * distinct relay — shared by every app paired to it, so ten logins on this instance is still one
 * connection — kept alive by OkHttp's own ping rather than a wakelock or a timer, and no wakelock is
 * taken at any point. Between requests it is a parked TCP connection, which is what every messaging
 * app on the phone already has. The work per request is one ECDH, one decrypt, one signature.
 *
 * WHAT IT DOES NOT DO: decide anything about what it signs. The permission gate is the app's own
 * declared `perms` from the pairing, ported verbatim in `Nip46Core.allowed`, and nothing here reads
 * the plaintext of a decrypt request or logs a request's contents.
 *
 * `specialUse`, not `dataSync`: Android 15 caps dataSync at six hours in any twenty-four, which for
 * a signer means it silently stops answering for most of the day — the same reason StayAwakeService
 * chose it, learned the same way.
 */
public class SignerRelayService extends Service {

    public static final String ACTION_START = "place.poster.app.SIGNER_START";
    public static final String ACTION_STOP = "place.poster.app.SIGNER_STOP";
    public static final String ACTION_RELOAD = "place.poster.app.SIGNER_RELOAD";

    public static final String PREFS = "pcsigner_relay";
    private static final String K_SESSIONS = "sessions";
    private static final String K_ON = "on";

    /** Read by the plugin so the panel reports what the SERVICE measured, never what it assumed. */
    public static boolean running = false;
    public static int connected = 0;
    /** Apps this phone signs for. Public because {@link place.poster.app.RunningNote} composes ONE
     *  notification for every background service and cannot reach `sessions`, which belongs to the
     *  work thread. */
    public static volatile int paired = 0;
    public static long lastRequestAt = 0;
    public static long requestsAnswered = 0;
    public static String lastError = "";

    /* THIS SERVICE'S WORK IS NOT UI WORK, AND IT USED TO RUN ON THE UI THREAD ANYWAY.
     *
     * Reported as "buttons laggy, scrolling terrible, the app is unusable" the same day the signer
     * went native and started answering for three apps at once — and nothing in any log, because
     * from the service's side every request SUCCEEDED. `onMessage` posted to the main looper, so
     * every frame a relay sent was parsed on the thread that draws. That alone is constant jank
     * with several relays open (the kind/session filter happens AFTER `new JSONArray(raw)`), and a
     * request that is actually FOR us then does, still on that thread: an Android Keystore load, an
     * ECDH + NIP-44 decrypt, a pure-Java secp256k1 Schnorr signature for the event, then a second
     * ECDH + encrypt + signature for the reply. Pure-BigInteger point multiplication is tens to
     * hundreds of milliseconds a go, so a single signature drops frames and three apps' worth
     * arrives whenever it likes — including mid-scroll.
     *
     * A HandlerThread rather than a pool, because the fix must not introduce the OTHER bug: every
     * mutation of `sessions`/`socks`/`failures` (plain Maps) is already confined to ONE thread, and
     * spreading this across a pool would race them. So the same confinement moves wholesale off the
     * main looper — which is why `reload()` and `closeAll()` are POSTED below rather than called
     * inline. `note()` is safe from here: NotificationManager is thread-safe, and `state()` only
     * reads a size. */
    private final HandlerThread thread = workThread();
    private final Handler handler = new Handler(thread.getLooper());

    private static HandlerThread workThread() {
        HandlerThread t = new HandlerThread("pc-signer", android.os.Process.THREAD_PRIORITY_BACKGROUND);
        t.start();
        return t;
    }
    private final Map<String, Nip46Core.Session> sessions = new LinkedHashMap<>();
    private final Map<String, WebSocket> socks = new HashMap<>();
    private final Map<String, Integer> failures = new HashMap<>();
    private OkHttpClient http;
    private String subId;
    private boolean stopping = false;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    // ---- what the web layer publishes -----------------------------------------------------------

    /**
     * The paired apps, as JSON, written by the web layer and read here.
     *
     * SharedPreferences rather than a bound service or a broadcast, because the two halves are almost
     * never alive at the same time — that is the point of the service. The page writes when a pairing
     * changes; this reads at start and on RELOAD.
     */
    public static void publishSessions(Context ctx, String json) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
           .edit().putString(K_SESSIONS, json == null ? "[]" : json).apply();
    }

    public static String readSessions(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(K_SESSIONS, "[]");
    }

    public static boolean wanted(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(K_ON, false);
    }

    public static void setWanted(Context ctx, boolean on) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(K_ON, on).apply();
    }

    /** Start (or nudge) the service, from the plugin or from BootReceiver. */
    public static void kick(Context ctx, String action) {
        Intent i = new Intent(ctx, SignerRelayService.class).setAction(action);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i);
            else ctx.startService(i);
        } catch (Throwable t) {
            /* Android 12+ refuses a foreground-service start from the background. That is not an
             * error worth crashing over: the service is either already up (the common case, since
             * this is mostly a RELOAD) or it will be started next time the app is opened. */
            try { ctx.startService(i); } catch (Throwable ignored) { }
        }
    }

    // ---- lifecycle ------------------------------------------------------------------------------

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : null;

        if (ACTION_STOP.equals(action)) {
            stopping = true;
            setWanted(this, false);
            handler.post(this::closeAll);      // `socks` belongs to the work thread — see the field
            dropNotification();
            stopSelf();
            return START_NOT_STICKY;
        }

        ensureChannel(this);
        /* BEFORE going foreground, not after: RunningNote composes the shared text from the
         * `running` flags, so a service that sets its own flag afterwards describes an app in which
         * it is not running — on the very first notification of every start. Put back on failure. */
        running = true;
        try {
            int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
                    ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE : 0;
            ServiceCompat.startForeground(this, RunningNote.ID, RunningNote.build(this), type);
        } catch (Throwable t) {
            running = false;
            lastError = "could not go foreground";
            stopSelf();
            return START_NOT_STICKY;
        }

        stopping = false;
        setWanted(this, true);
        /* Posted, not called: `reload()` opens sockets and reads the Keystore, and it mutates the
         * maps `recv` owns. `startForeground` above stays on this thread, where the 5s deadline is. */
        handler.post(this::reload);
        return START_STICKY;   // a signer that stops answering because of memory pressure is the bug
    }

    /** Re-read the published pairings and make the sockets match them. */
    private void reload() {
        if (SignerKey.load(this) == null) {
            // No key on this phone: there is nothing to sign with, so holding sockets open would be
            // pure battery for a service that must refuse every request anyway.
            lastError = "no signing key on this device";
            closeAll();
            note();
            return;
        }
        java.util.List<Nip46Core.Session> incoming = new java.util.ArrayList<>();
        try {
            JSONArray arr = new JSONArray(readSessions(this));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                incoming.add(new Nip46Core.Session(
                        o.optString("pk", ""), o.optString("relay", ""), o.optString("name", ""),
                        o.optString("perms", ""), o.optString("enc", ""), o.optLong("last", 0)));
            }
        } catch (Throwable t) {
            lastError = "could not read the paired apps";
            return;      // keep running on what we already had rather than dropping every session
        }

        Map<String, Nip46Core.Session> next = Nip46Core.merge(sessions, incoming);
        sessions.clear();
        sessions.putAll(next);
        paired = sessions.size();          // what the shared notification says; see RunningNote

        /* NOTHING PAIRED IS NOTHING TO DO. A foreground service with no sockets still costs: it
         * pins the process resident, holds a notification, and is one more thing the platform keeps
         * warm — all to answer requests that cannot arrive, because no app is paired to send them.
         * The switch stays on, so pairing again starts it again; this is the service declining to
         * idle. It matters more than it looks: revoking the last app is exactly when someone expects
         * the battery cost to stop, and a service that lingers is the reason people report that
         * turning a feature off "didn't do anything". */
        if (sessions.isEmpty()) {
            closeAll();
            stopping = true;
            dropNotification();
            stopSelf();
            return;
        }

        java.util.List<String> want = Nip46Core.relays(sessions);
        // Drop sockets nothing needs any more — a revoked pairing must not keep a connection alive.
        for (String url : new java.util.ArrayList<>(socks.keySet())) {
            if (!want.contains(url)) closeOne(url);
        }
        for (String url : want) if (!socks.containsKey(url)) open(url);
        note();
    }

    private OkHttpClient http() {
        if (http == null) {
            http = new OkHttpClient.Builder()
                    /* THE KEEPALIVE, AND IT IS THE ENTIRE BATTERY BUDGET OF THIS SERVICE.
                     *
                     * Between requests the service does exactly two things: hold a parked TCP socket,
                     * which costs nothing, and send this ping, which costs a radio wake-up. So the
                     * interval IS the power draw — nothing else here runs on a timer, and no wakelock
                     * is taken anywhere in this file.
                     *
                     * FOUR MINUTES, chosen against the two real constraints rather than picked round.
                     * Below it: carrier NATs commonly expire an idle TCP mapping at five minutes, and
                     * a socket that has been silently unmapped is dead-but-open — it answers nothing
                     * while looking perfectly healthy, which is the exact bug this service exists to
                     * remove. Above it: every ping is a wake-up, and at the 150s this was first
                     * written with that is 24 an hour for no additional safety. Android's own push
                     * heartbeat sits at 15 minutes on wifi and 28 on cellular, which is the shape of
                     * the trade — they can afford it because one socket serves every app on the
                     * phone. Four minutes is 15 an hour and stays inside the five-minute floor.
                     *
                     * Detection latency is the thing being bought: if the socket has died, a request
                     * cannot arrive until the redial, so this interval is also the worst case before
                     * the signer notices it has gone deaf. Making it much longer to save power would
                     * hand back the delay the whole service was written to remove. */
                    .pingInterval(240, TimeUnit.SECONDS)
                    .retryOnConnectionFailure(true)
                    .build();
        }
        return http;
    }

    private void open(final String url) {
        if (stopping || socks.containsKey(url)) return;
        final String me = SignerKey.pubkey(this);
        if (me == null) return;
        if (subId == null) subId = "ns" + Long.toHexString(System.nanoTime() & 0xffffffL);
        Request req;
        try {
            req = new Request.Builder().url(url).build();
        } catch (Throwable t) {
            lastError = "bad relay url";
            return;                       // a malformed relay must not take the service down
        }
        WebSocket ws = http().newWebSocket(req, new WebSocketListener() {
            @Override public void onOpen(WebSocket s, Response r) {
                failures.remove(url);
                try {
                    JSONObject f = new JSONObject();
                    f.put("kinds", new JSONArray().put(24133));
                    f.put("#p", new JSONArray().put(me));
                    f.put("since", Nip46Core.since(System.currentTimeMillis() / 1000));
                    s.send(new JSONArray().put("REQ").put(subId).put(f).toString());
                } catch (Throwable ignored) { }
                handler.post(() -> { connected = socks.size(); note(); });
            }
            @Override public void onMessage(WebSocket s, String text) {
                handler.post(() -> recv(url, text));
            }
            @Override public void onFailure(WebSocket s, Throwable t, Response r) {
                handler.post(() -> dropped(url, t == null ? "socket failed" : String.valueOf(t.getMessage())));
            }
            @Override public void onClosed(WebSocket s, int code, String reason) {
                handler.post(() -> dropped(url, "closed"));
            }
        });
        socks.put(url, ws);
    }

    /** A socket died. Redial it while something still needs it, backing off but never giving up. */
    private void dropped(String url, String why) {
        socks.remove(url);
        connected = socks.size();
        lastError = why == null ? "" : why;
        note();
        if (stopping || !Nip46Core.relays(sessions).contains(url)) return;
        int n = failures.containsKey(url) ? failures.get(url) + 1 : 1;
        failures.put(url, n);
        handler.postDelayed(() -> {
            if (!stopping && !socks.containsKey(url) && Nip46Core.relays(sessions).contains(url)) {
                open(url);
            }
        }, Nip46Core.backoffMs(n));
    }

    private void closeOne(String url) {
        WebSocket ws = socks.remove(url);
        if (ws != null) try { ws.close(1000, "done"); } catch (Throwable ignored) { }
    }

    private void closeAll() {
        for (String url : new java.util.ArrayList<>(socks.keySet())) closeOne(url);
        socks.clear();
        connected = 0;
    }

    // ---- the request loop -----------------------------------------------------------------------

    private void recv(String url, String raw) {
        JSONArray m;
        try { m = new JSONArray(raw); } catch (Throwable t) { return; }
        if (m.length() < 3 || !"EVENT".equals(m.optString(0, ""))) return;
        if (!String.valueOf(subId).equals(m.optString(1, ""))) return;
        JSONObject ev = m.optJSONObject(2);
        if (ev == null || ev.optInt("kind", 0) != 24133) return;

        String from = ev.optString("pubkey", "").toLowerCase();
        Nip46Core.Session sess = sessions.get(from);
        if (sess == null) return;                       // not an app this phone signs for

        byte[] sec = SignerKey.load(this);
        if (sec == null) return;

        String plain = decode(sec, from, ev.optString("content", ""), sess);
        if (plain == null) return;

        String id, method;
        JSONArray params;
        try {
            JSONObject req = new JSONObject(plain);
            id = req.optString("id", "");
            method = req.optString("method", "");
            params = req.optJSONArray("params");
            if (params == null) params = new JSONArray();
        } catch (Throwable t) { return; }
        if (id.isEmpty() || method.isEmpty()) return;

        sess.last = System.currentTimeMillis() / 1000;
        lastRequestAt = sess.last;

        String result = null, error = null;
        if (!Nip46Core.allowed(sess.perms, method, kindOf(method, params))) {
            error = "not permitted: " + method + " was not in what this app asked for";
        } else {
            try { result = handle(sec, method, params); }
            catch (Throwable t) { error = String.valueOf(t.getMessage()); }
        }

        try {
            JSONObject out = new JSONObject();
            out.put("id", id);
            out.put("result", error != null ? "" : (result == null ? "" : result));
            if (error != null) out.put("error", error);
            send(sec, sess, out.toString());
            requestsAnswered++;
        } catch (Throwable t) {
            lastError = "could not answer";
        }
        note();
    }

    /**
     * The kind being signed, or -1.
     *
     * -1 and never 0 when it cannot be read: kind 0 is profile metadata, so defaulting there would
     * hand an app granted `sign_event:0` every template this failed to parse.
     */
    private static int kindOf(String method, JSONArray params) {
        if (!"sign_event".equals(method) || params == null || params.length() == 0) return -1;
        try {
            Object p = params.get(0);
            JSONObject tpl = (p instanceof JSONObject) ? (JSONObject) p
                                                       : new JSONObject(String.valueOf(p));
            return tpl.optInt("kind", -1);
        } catch (Throwable t) { return -1; }
    }

    /** Try both schemes, ordered by the payload's own marker, and remember which one worked. */
    private String decode(byte[] sec, String peerHex, String ct, Nip46Core.Session sess) {
        byte[] peer;
        try { peer = Nostr.unhex(peerHex); } catch (Throwable t) { return null; }
        boolean fourFirst = Nip46Core.nip04First(ct);
        for (int i = 0; i < 2; i++) {
            boolean four = (i == 0) == fourFirst;
            try {
                String pt = four ? Crypt.nip04Decrypt(sec, peer, ct)
                                 : Crypt.nip44Decrypt(Crypt.conversationKey(sec, peer), ct);
                if (pt != null) { sess.enc = four ? "nip04" : "nip44"; return pt; }
            } catch (Throwable ignored) { }
        }
        return null;
    }

    /** Encrypt, sign and publish the reply on the socket that carries this session. */
    private void send(byte[] sec, Nip46Core.Session sess, String payload) throws Exception {
        byte[] peer = Nostr.unhex(sess.pk);
        String ct = Nip46Core.replyWithNip04(sess.enc)
                ? Crypt.nip04Encrypt(sec, peer, payload)
                : Crypt.nip44Encrypt(Crypt.conversationKey(sec, peer), payload, null);

        String pub = Nostr.hex(Nostr.pubkey(sec));
        long now = System.currentTimeMillis() / 1000;
        String tags = new JSONArray().put(new JSONArray().put("p").put(sess.pk)).toString();
        String eid = Nostr.eventId(pub, now, 24133, tags, ct);

        JSONObject ev = new JSONObject();
        ev.put("id", eid);
        ev.put("pubkey", pub);
        ev.put("created_at", now);
        ev.put("kind", 24133);
        ev.put("tags", new JSONArray(tags));
        ev.put("content", ct);
        ev.put("sig", Nostr.hex(Nostr.sign(Nostr.unhex(eid), sec, null)));

        WebSocket ws = socks.get(sess.relay);
        if (ws != null) ws.send(new JSONArray().put("EVENT").put(ev).toString());
    }

    /**
     * Perform a method. The same set the web signer answers, with the same shapes.
     *
     * `sign_event` returns the whole signed event as a STRING, which is what NIP-46 specifies and
     * what the JS half returns — a client handed a bare signature has no event to publish.
     */
    private String handle(byte[] sec, String method, JSONArray params) throws Exception {
        String pub = Nostr.hex(Nostr.pubkey(sec));
        switch (method) {
            case "connect":        return "ack";
            case "ping":           return "pong";
            case "get_public_key": return pub;
            case "sign_event": {
                Object p = params.get(0);
                JSONObject ev = (p instanceof JSONObject) ? (JSONObject) p
                                                          : new JSONObject(String.valueOf(p));
                ev.put("pubkey", pub);
                if (!ev.has("created_at")) ev.put("created_at", System.currentTimeMillis() / 1000);
                if (!ev.has("tags")) ev.put("tags", new JSONArray());
                if (!ev.has("content")) ev.put("content", "");
                String eid = Nostr.eventId(pub, ev.getLong("created_at"), ev.getInt("kind"),
                                           ev.getJSONArray("tags").toString(),
                                           ev.optString("content", ""));
                ev.put("id", eid);
                ev.put("sig", Nostr.hex(Nostr.sign(Nostr.unhex(eid), sec, null)));
                return ev.toString();
            }
            case "nip04_encrypt":
                return Crypt.nip04Encrypt(sec, Nostr.unhex(params.getString(0)), params.getString(1));
            case "nip04_decrypt":
                return Crypt.nip04Decrypt(sec, Nostr.unhex(params.getString(0)), params.getString(1));
            case "nip44_encrypt":
                return Crypt.nip44Encrypt(
                        Crypt.conversationKey(sec, Nostr.unhex(params.getString(0))),
                        params.getString(1), null);
            case "nip44_decrypt":
                return Crypt.nip44Decrypt(
                        Crypt.conversationKey(sec, Nostr.unhex(params.getString(0))),
                        params.getString(1));
            default:
                throw new IllegalArgumentException("unsupported method: " + method);
        }
    }

    // ---- the notification -----------------------------------------------------------------------

    /**
     * Redraw the notification, but ONLY when it would actually say something different.
     *
     * `note()` is called from the request path, and a busy desktop session is several requests per
     * action — encrypt, wrap, sign. Rebuilding and posting a Notification for each one is real work
     * (a Binder round trip to the system server, and a shade animation on some OEM builds) to
     * redisplay a string that has not changed. The text is the whole state this notification carries,
     * so comparing it is an exact test of whether the post is needed.
     */
    private String shown = null;

    private void note() {
        try {
            if (!running) return;
            String state = RunningNote.text();
            if (state.equals(shown)) return;
            shown = state;
            RunningNote.refresh(this);
        } catch (Throwable ignored) { }
    }

    /** Say what is TRUE, not what was intended: "connected" with no socket is the lie that would
     *  hide the very failure this service exists to make impossible. */
    /* The notification itself belongs to RunningNote now — ONE item in the shade however many of
     * this app's background services are up, because two permanent notifications from one app is
     * the app's problem and not the user's. `paired` is what this service contributes to that text;
     * it is a field rather than `sessions.size()` because the composer runs on whatever thread posts
     * and `sessions` belongs to the work thread. */
    static void ensureChannel(Context ctx) {
        RunningNote.ensureChannel(ctx);
    }

    /**
     * Stand down from the shared notification.
     *
     * REMOVE would delete it out from under "stay connected" if that is still up, leaving a running
     * foreground service with nothing in the shade — the thing the platform requires and the user is
     * owed. So while anything else needs it we DETACH (the item stays, it just stops being ours) and
     * re-post it without us in the text.
     */
    private void dropNotification() {
        running = false;
        paired = 0;
        if (RunningNote.othersRunning(true)) {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH);
            RunningNote.refresh(this);
        } else {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        }
    }

    @Override
    public void onDestroy() {
        stopping = true;
        /* Close on the thread that owns the sockets, then stop that thread — `quitSafely` runs the
         * queued close first, where `quit()` would drop it and leak every open WebSocket. */
        handler.post(() -> { closeAll(); thread.quitSafely(); });
        running = false;
        paired = 0;
        /* Same as the other half: killed rather than switched off, so nothing has redrawn the
         * shared notification and it would go on naming a signer that has gone. */
        if (RunningNote.othersRunning(true)) RunningNote.refresh(this);
        super.onDestroy();
    }
}
