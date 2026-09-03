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
import place.poster.app.sms.SmsArchive;
import place.poster.app.sms.SmsOutbox;
import place.poster.app.sms.SmsSweep;
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
    private static final String SMS_RECEIPTS = "poster_sms_outbox_receipts";

    public static final String ACTION_START = "place.poster.app.SIGNER_START";
    public static final String ACTION_STOP = "place.poster.app.SIGNER_STOP";
    public static final String ACTION_RELOAD = "place.poster.app.SIGNER_RELOAD";
    public static final String ACTION_SMS_ARCHIVE = "place.poster.app.SMS_ARCHIVE";
    /** Back-fill the phone's OWN history — what the launcher's Texts app asks for. */
    public static final String ACTION_SMS_SWEEP = "place.poster.app.SMS_SWEEP";

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
        /* DEFAULT priority, NOT BACKGROUND, and the difference is not a nice value.
         *
         * THREAD_PRIORITY_BACKGROUND moves the thread into Android's background CGROUP, which is
         * capped at a small share of one core and on most devices confined to the little cluster.
         * This thread does the only CPU-heavy work in the app: four secp256k1 point multiplications
         * per NIP-46 request (ECDH + sign for the event, ECDH + sign for the reply), in pure-Java
         * BigInteger. Inside that cap they take an order of magnitude longer, and the whole of it
         * lands on the one number a person can feel — how long the other device waits before its
         * note is published. Reported as "this signer is slower than amber, waiting over a min".
         *
         * Getting the work OFF the main thread was right and stays; taking it out of the foreground
         * scheduler with it was not, and was never the point. The thread is idle between requests,
         * so default priority costs nothing when nothing is being signed. */
        HandlerThread t = new HandlerThread("pc-signer", android.os.Process.THREAD_PRIORITY_DEFAULT);
        t.start();
        return t;
    }
    private final Map<String, Nip46Core.Session> sessions = new LinkedHashMap<>();
    private final Map<String, WebSocket> socks = new HashMap<>();
    private final Map<String, Integer> failures = new HashMap<>();
    /** When each socket last delivered anything. See {@link #redialStale}. */
    private final Map<String, Long> lastRx = new HashMap<>();
    /* How long a socket may be silent before the app being OPENED redials it.
     *
     * A relay sends nothing between requests, so silence is not evidence of death — which is why
     * this is not a timer and never fires on its own. It is only consulted when a human has just
     * opened the app, i.e. at the one moment the cost of a redial (one handshake) is worth paying
     * for the chance that the socket is a zombie. */
    private static final long STALE_MS = 90_000L;
    /** …and no more often than this per relay, however many reloads arrive. See redialStale. */
    private static final long REDIAL_EVERY_MS = 300_000L;
    private final Map<String, Long> lastRedial = new HashMap<>();
    private OkHttpClient http;
    private String subId;
    /** The SMS outbox's own subscription id, so its events are told apart from signer traffic. */
    private String smsSubId;
    private boolean stopping = false;
    private static final java.util.concurrent.ConcurrentLinkedQueue<String[]> smsArchive =
            new java.util.concurrent.ConcurrentLinkedQueue<>();
    private static final java.util.concurrent.ConcurrentLinkedQueue<String> smsArchiveDeletes =
            new java.util.concurrent.ConcurrentLinkedQueue<>();

    /** Called by the SMS_DELIVER receiver; the queue survives a cold service start in this process. */
    public static void archiveIncoming(Context ctx, String from, String body, long when) {
        smsArchive.add(new String[]{from == null ? "" : from, body == null ? "" : body,
                Long.toString(when)});
        if (wanted(ctx)) kick(ctx, ACTION_SMS_ARCHIVE);
    }

    /** Remove the encrypted mirror too; deleting only Telephony leaves old media on desktop. */
    public static void archiveDelete(Context ctx, String doc) {
        if (doc == null || doc.isEmpty()) return;
        smsArchiveDeletes.add(doc);
        if (wanted(ctx)) kick(ctx, ACTION_SMS_ARCHIVE);
    }

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

    /**
     * ARCHIVE THE PHONE'S OWN HISTORY, with no WebView anywhere.
     *
     * The launcher's Texts app is native and reads the provider directly, so opening it used to
     * back up nothing at all — the mirror lived in JavaScript that only ran while somebody had
     * PosterChan → Texts on screen. This is the door for that screen, and it is deliberately just
     * a nudge: the service decides whether a relay is connected, and the sweep bounds itself.
     */
    public static void sweepSms(Context ctx) {
        if (wanted(ctx)) { kick(ctx, ACTION_SMS_SWEEP); return; }
        /* SAY SO RATHER THAN DO NOTHING. This service holds the relay sockets, so with it switched
         * off there is no publish path and the back-fill genuinely cannot run — but a screen that
         * archives nothing and reports nothing is the failure this whole feature was reported as.
         * Turning the service on from here would be a surprising side effect of opening Texts, so
         * the answer is written where Texts can show it instead. */
        SmsArchive.note(ctx, "not archiving: the background signer is switched off, so this phone "
                + "has no relay connection to publish through");
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
            sec = null;
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
        handler.post(this::publishSmsArchive);
        if (ACTION_SMS_SWEEP.equals(action)) handler.post(this::sweepSmsHistory);
        return START_STICKY;   // a signer that stops answering because of memory pressure is the bug
    }

    /** Re-read the published pairings and make the sockets match them. */
    private void reload() {
        sec = null;                       // re-read: the key may have just been armed or cleared
        myPubHex = null;
        if (sec() == null) {
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
        redialStale();
        for (String url : want) if (!socks.containsKey(url)) open(url);
        note();
    }

    /* THE CRYPTO POOL. Bounded at three and idle-reaped to zero, so a phone that is not signing
     * anything holds no threads at all. Created lazily for the same reason. */
    private java.util.concurrent.ThreadPoolExecutor cryptoPool;
    private synchronized java.util.concurrent.ThreadPoolExecutor pool() {
        if (cryptoPool == null) {
            cryptoPool = new java.util.concurrent.ThreadPoolExecutor(
                    0, 3, 30L, TimeUnit.SECONDS, new java.util.concurrent.LinkedBlockingQueue<>(),
                    r -> {
                        Thread t = new Thread(r, "pc-signer-crypto");
                        // DEFAULT priority, for the reason written on the work thread: the
                        // background cgroup would put this straight back where it started.
                        t.setPriority(Thread.NORM_PRIORITY);
                        return t;
                    });
        }
        return cryptoPool;
    }

    /* THE UNSEALED KEY, HELD ONCE.
     *
     * `SignerKey.load()` opens the AndroidKeyStore provider and does a hardware-backed AES-GCM
     * decrypt — tens to hundreds of milliseconds on a TEE device — and it was called once PER
     * REQUEST, on the socket thread, before any of the actual work. Measured across four clients:
     * 1.3 answered requests a second in total, which is not what libsecp256k1 costs.
     *
     * Cleared when the service stops and re-read on every reload, so turning the key off and on
     * again takes effect. The seal protects the key AT REST; a running foreground signer holds it in
     * memory for the same reason every other signer does. */
    private volatile byte[] sec;
    private byte[] sec() {
        byte[] k = sec;
        if (k == null) { k = SignerKey.load(this); sec = k; }
        return k;
    }

    /** Our own x-only pubkey, derived once per process. See send(). */
    private volatile String myPubHex;
    private String myPub(byte[] sec) {
        String p = myPubHex;
        if (p == null) { p = Nostr.hex(Nostr.pubkey(sec)); myPubHex = p; }
        return p;
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
        if (smsSubId == null) smsSubId = "sms" + Long.toHexString(System.nanoTime() & 0xffffffL);
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
                    /* AND THE SMS OUTBOX, on the socket that is already here.
                     *
                     * A text this phone was asked to send by another device is a document on the
                     * same relay, and the drain for it lived only in the client's JavaScript --
                     * which runs when the app is VISIBLE. So a request sat unperformed until
                     * somebody opened PosterChan on the handset, which is not what "send from my
                     * laptop" means. Reported as "it should not have to be visible".
                     *
                     * A second REQ rather than a second socket: this one is open, authenticated by
                     * nothing (the relay carries both kinds for the same key), and already
                     * redialled and watched for staleness by everything below. */
                    try {
                        s.send(new JSONArray().put("REQ").put(smsSubId)
                                .put(SmsOutbox.filter(me)).toString());
                    } catch (Throwable ignored2) { }
                    flushSmsReceipts(s);
                } catch (Throwable ignored) { }
                handler.post(() -> { lastRx.put(url, System.currentTimeMillis());
                                     connected = socks.size(); note(); publishSmsArchive(); });
            }
            @Override public void onMessage(WebSocket s, String text) {
                handler.post(() -> { lastRx.put(url, System.currentTimeMillis()); recv(url, text); });
            }
            /* `socks.get(url) == s` — a death report is only about the socket that is CURRENT.
             *
             * redialStale() closes a socket and dials the same relay again in the same pass, so the
             * old one's onClosed arrives after the replacement is already in the map. Acted on
             * blindly it removes the NEW socket and schedules a backoff redial for a relay that is
             * connected, which leaves the service holding nothing while reporting a redial in
             * progress — the failure this whole file exists to make impossible. */
            @Override public void onFailure(WebSocket s, Throwable t, Response r) {
                handler.post(() -> { if (socks.get(url) == s)
                        dropped(url, t == null ? "socket failed" : String.valueOf(t.getMessage())); });
            }
            @Override public void onClosed(WebSocket s, int code, String reason) {
                handler.post(() -> { if (socks.get(url) == s) dropped(url, "closed"); });
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

    /* THE ONE THING "OPEN THE APP AND TRY AGAIN" COULD NOT FIX, AND IT IS WHAT PEOPLE DO FIRST.
     *
     * `reload()` only ever opened relays it had NO socket for — so a socket that was dead-but-open
     * was left exactly as it was. That is the state a phone comes back from doze in (the carrier
     * unmapped the TCP connection without a FIN; nothing is delivered and nothing is reported), and
     * the ping only catches it at the next four-minute beat. Meanwhile a kind-24133 is EPHEMERAL:
     * the relay fans it out to whoever is subscribed at that instant and stores nothing, so every
     * request sent into that window is destroyed, not delayed. Reported as a desktop stuck on
     * "waiting for your signer…", and bringing this app to the foreground changed nothing at all.
     *
     * Consulted ONLY from reload(), which is what an app-open triggers, so this costs one handshake
     * at the moment somebody is standing there waiting for it — never on a timer, and never while
     * the phone is asleep. */
    private void redialStale() {
        long now = System.currentTimeMillis();
        for (String url : new java.util.ArrayList<>(socks.keySet())) {
            Long rx = lastRx.get(url);
            if (rx != null && now - rx <= STALE_MS) continue;
            /* AND NOT MORE THAN ONCE EVERY FIVE MINUTES PER RELAY.
             *
             * A relay sends NOTHING between requests and OkHttp answers its own pings internally, so
             * `lastRx` is stale on a perfectly healthy idle socket — which means the rule above is
             * true on nearly every reload(), and reload() runs whenever the app is opened or the
             * page re-publishes its pairings. Unguarded, that reconnects the socket on every one of
             * them, and a kind-24133 published during those ~1s windows is DESTROYED rather than
             * delayed (the relay fans ephemeral events out to whoever is subscribed at that instant
             * and stores nothing). A fix for a deaf socket must not become a way to miss requests. */
            Long last = lastRedial.get(url);
            if (last != null && now - last < REDIAL_EVERY_MS) continue;
            lastRedial.put(url, now);
            closeOne(url);          // the caller's open-what-is-missing loop dials it straight back
        }
    }

    private void closeOne(String url) {
        WebSocket ws = socks.remove(url);
        lastRx.remove(url);
        if (ws != null) try { ws.close(1000, "done"); } catch (Throwable ignored) { }
    }

    private void closeAll() {
        for (String url : new java.util.ArrayList<>(socks.keySet())) closeOne(url);
        socks.clear();
        lastRx.clear();
        connected = 0;
    }

    // ---- the request loop -----------------------------------------------------------------------

    /* PER-APP TALLY — which paired app is asking, how often, and how often it REPEATS itself.
     * A device stuck re-asking the same decrypt every ~20s (measured on the relay for hours) is
     * invisible from here one request at a time; these are what let the pairings screen name it.
     * STATIC, like MusicPlugin's counters, so a service the OS recycled still answers for the
     * process's lifetime. Written only on the owner thread (the handler.post below). */
    static final java.util.Map<String, long[]> perApp = new java.util.HashMap<>();      // pk -> {n, dup}
    static final java.util.Map<String, String> perAppFp = new java.util.HashMap<>();    // pk -> last fingerprint
    static final java.util.Map<String, String> perAppMethod = new java.util.HashMap<>();

    /* A SEND ASKED FOR BY ANOTHER DEVICE. Off the main thread, like every other piece of crypto
     * here: this decrypts, hands the radio a message and signs a reply.
     *
     * SmsOutbox refuses while the app is on screen, because the client's own drain owns it then --
     * two readers of one request with no agreement between them send somebody's text twice, and a
     * sent text cannot be recalled. */
    private void smsOutbox(String url, JSONObject ev) {
        if (ev == null) return;
        if (!SmsOutbox.isRequest(ev)) return;
        pool().execute(() -> {
            JSONObject done = SmsOutbox.perform(SignerRelayService.this, ev);
            if (done == null) return;
            /* Download/decrypt can outlive a relay socket. Publishing through the socket captured
             * before that work loses the receipt after a reconnect and leaves every client saying
             * "waiting for phone" forever, while the durable radio claim forbids another attempt.
             * Persist first, then flush through whichever socket is current. */
            queueSmsReceipt(done);
            handler.post(() -> flushSmsReceipts(socks.get(url)));
        });
    }

    private void queueSmsReceipt(JSONObject done) {
        String id = done == null ? "" : done.optString("id", "");
        if (id.isEmpty()) return;
        getSharedPreferences(SMS_RECEIPTS, MODE_PRIVATE).edit().putString(id, done.toString()).commit();
    }

    private void flushSmsReceipts(WebSocket ws) {
        if (ws == null) return;
        android.content.SharedPreferences p = getSharedPreferences(SMS_RECEIPTS, MODE_PRIVATE);
        for (java.util.Map.Entry<String, ?> e : p.getAll().entrySet()) {
            Object value = e.getValue();
            if (!(value instanceof String)) continue;
            try {
                JSONObject done = new JSONObject((String) value);
                String wire = new JSONArray().put("EVENT").put(done).toString();
                if (ws.send(wire)) p.edit().remove(e.getKey()).apply();
            } catch (Throwable bad) {
                p.edit().remove(e.getKey()).apply();
            }
        }
    }

    private void recv(String url, String raw) {
        JSONArray m;
        try { m = new JSONArray(raw); } catch (Throwable t) { return; }
        if (m.length() >= 2 && "AUTH".equals(m.optString(0, ""))) {
            /* NIP-78 (2026-09-03): the SMS archive is private app data, so this connection must
             * authenticate as the same key before its kind-30078 REQ/EVENT traffic is accepted.
             * AUTH and the replay are written in order on the same OkHttp socket. */
            try {
                byte[] key = sec();
                WebSocket socket = socks.get(url);
                if (key == null || socket == null) return;
                String pubHex = Nostr.hex(Nostr.pubkey(key));
                java.util.List<java.util.List<String>> tags = new java.util.ArrayList<>();
                tags.add(java.util.Arrays.asList("relay", url));
                tags.add(java.util.Arrays.asList("challenge", m.optString(1, "")));
                JSONObject auth = SmsOutbox.signed(key, pubHex,
                        System.currentTimeMillis() / 1000L, 22242, tags, "");
                socket.send(new JSONArray().put("AUTH").put(auth).toString());
                socket.send(new JSONArray().put("REQ").put(smsSubId)
                        .put(SmsOutbox.filter(pubHex)).toString());
                publishSmsArchive();
            } catch (Throwable t) { lastError = "relay AUTH failed"; }
            return;
        }
        if (m.length() < 3 || !"EVENT".equals(m.optString(0, ""))) return;
        String sub = m.optString(1, "");
        if (String.valueOf(smsSubId).equals(sub)) { smsOutbox(url, m.optJSONObject(2)); return; }
        if (!String.valueOf(subId).equals(sub)) return;
        JSONObject ev = m.optJSONObject(2);
        if (ev == null || ev.optInt("kind", 0) != 24133) return;

        String from = ev.optString("pubkey", "").toLowerCase();
        Nip46Core.Session sess = sessions.get(from);
        if (sess == null) return;                       // not an app this phone signs for

        final byte[] sec = sec();
        if (sec == null) return;

        /* EVERYTHING FROM HERE IS CRYPTO, AND IT GOES TO A POOL.
         *
         * One NIP-46 request costs FOUR secp256k1 point multiplications: an ECDH to read it, an ECDH
         * for whatever it asked (a decrypt is another one), an ECDH to encrypt the reply, and a
         * signature over the reply event. All of that ran on the single work thread, strictly one
         * request after another — MEASURED on the relay at 1.5 answered requests per second per
         * client, against about 11 for the WebView this service replaced. A DM restore is two
         * requests per message, so a 400-message history took nine minutes and the app was reported,
         * fairly, as "way too slow, nobody will use it".
         *
         * The maps stay thread-confined: the session lookup, the socket lookup and every field this
         * touches are read HERE, on the work thread, and the only things the pool sends back are
         * posted to it. So the pool never reads or writes `sessions`, `socks` or `failures`, and the
         * confinement that made this file safe is intact — it is the arithmetic that moved, not the
         * bookkeeping.
         *
         * Three threads, idle-reaped: enough to overlap a request with the next one on any phone
         * made this decade, far short of anything that would heat one up. */
        final String peer = from;
        final String content = ev.optString("content", "");
        final String encNow = sess.enc, permsNow = sess.perms, peerPk = sess.pk;
        final WebSocket ws = socks.get(sess.relay);
        final Nip46Core.Session sref = sess;
        pool().execute(() -> {
            String[] learned = new String[1];
            String plain = decode(sec, peer, content, encNow, learned);
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

            String result = null, error = null;
            if (!Nip46Core.allowed(permsNow, method, kindOf(method, params))) {
                error = "not permitted: " + method + " was not in what this app asked for";
            } else {
                try { result = handle(sec, method, params); }
                catch (Throwable t) { error = String.valueOf(t.getMessage()); }
            }

            boolean sent = false;
            try {
                JSONObject out = new JSONObject();
                out.put("id", id);
                out.put("result", error != null ? "" : (result == null ? "" : result));
                if (error != null) out.put("error", error);
                send(sec, peerPk, learned[0] != null ? learned[0] : encNow, ws, out.toString());
                sent = true;
            } catch (Throwable t) {
                lastError = "could not answer";
            }
            // Back to the owner thread for every piece of shared state, including the counters the
            // panel reads — those are what tell a phone that answered from one that only tried.
            final boolean ok = sent;
            final String enc = learned[0];
            final String fp = method + "|" + params.length() + "|"
                    + params.toString().substring(0, Math.min(64, params.toString().length()));
            final String methodF = method;
            handler.post(() -> {
                if (enc != null) sref.enc = enc;
                sref.last = System.currentTimeMillis() / 1000;
                lastRequestAt = sref.last;
                if (ok) requestsAnswered++;
                long[] t = perApp.get(peer);
                if (t == null) { t = new long[]{0, 0}; perApp.put(peer, t); }
                t[0]++;
                if (fp.equals(perAppFp.get(peer))) t[1]++;
                perAppFp.put(peer, fp);
                perAppMethod.put(peer, methodF);
                note();
            });
        });
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

    /** Publish receiver-captured texts on the signer's already-connected relays, with no WebView. */
    private void publishSmsArchive() {
        if (socks.isEmpty()) return;                 // onOpen calls us again once a relay exists
        final String[] row = smsArchive.poll();
        final String deletedDoc = row == null ? smsArchiveDeletes.poll() : null;
        if (row == null && deletedDoc == null) return;
        final java.util.List<WebSocket> targets = new java.util.ArrayList<>(socks.values());
        pool().execute(() -> {
            final java.util.List<JSONObject> events = new java.util.ArrayList<>();
            if (row != null) {
                JSONObject ev = SmsOutbox.archiveIncoming(SignerRelayService.this, row[0], row[1],
                        Long.parseLong(row[2]));
                if (ev != null) events.add(ev);
            } else events.addAll(SmsOutbox.archiveDelete(SignerRelayService.this, deletedDoc));
            handler.post(() -> {
                if (events.isEmpty()) {
                    if (row != null) smsArchive.add(row); else smsArchiveDeletes.add(deletedDoc);
                }
                else {
                    for (JSONObject ev : events) {
                        String wire = new JSONArray().put("EVENT").put(ev).toString();
                        for (WebSocket ws : targets) if (ws != null) ws.send(wire);
                    }
                }
                publishSmsArchive();
            });
        });
    }

    /**
     * One bounded back-fill pass, published on the sockets this service already owns.
     *
     * THE MARK MOVES LAST, AND ONLY IF SOMETHING CARRIED THE EVENTS. A sweep that advanced its own
     * mark with no relay connected would throw that window of history away silently, and the next
     * pass would start after messages nobody ever archived — which is the shape of every "it says
     * it synced and the messages are not there" report this feature has had.
     */
    private void sweepSmsHistory() {
        if (socks.isEmpty()) return;                 // onOpen calls us again once a relay exists
        final java.util.List<WebSocket> targets = new java.util.ArrayList<>(socks.values());
        pool().execute(() -> {
            final SmsSweep.Report rep;
            try {
                rep = SmsArchive.sweep(SignerRelayService.this, SmsArchive.ROWS_PER_PASS);
            } catch (Throwable t) {
                /* The reason is kept where a phone nobody can query will still say it: SmsArchive
                 * records every pass under its own prefs, which is what Texts → Details reads. */
                lastError = "sms archive: " + t;
                return;
            }
            if (rep == null || rep.events.isEmpty()) return;
            handler.post(() -> {
                int sent = 0;
                for (JSONObject ev : rep.events) {
                    String wire = new JSONArray().put("EVENT").put(ev).toString();
                    for (WebSocket ws : targets) if (ws != null && ws.send(wire)) sent++;
                }
                if (sent > 0) SmsArchive.commit(SignerRelayService.this, rep);
                /* MORE HISTORY BEHIND THIS ONE, so come back for it — but through the service door,
                 * not a loop: the pass is bounded because this phone is in somebody's hand, and
                 * "encrypting and copying messages to blossom makes it glitchy" is what an
                 * unbounded one feels like. */
                if (sent > 0 && rep.more) handler.postDelayed(this::sweepSmsHistory, 4000L);
            });
        });
    }

    /** Try both schemes, ordered by the payload's own marker, and remember which one worked. */
    /** `learned[0]` reports the scheme that worked; the CALLER writes it to the session, on the work
     *  thread. This runs on the crypto pool and must not touch shared state. */
    private String decode(byte[] sec, String peerHex, String ct, String encNow, String[] learned) {
        byte[] peer;
        try { peer = Nostr.unhex(peerHex); } catch (Throwable t) { return null; }
        boolean fourFirst = Nip46Core.nip04First(ct);
        for (int i = 0; i < 2; i++) {
            boolean four = (i == 0) == fourFirst;
            try {
                String pt = four ? Crypt.nip04Decrypt(sec, peer, ct)
                                 : Crypt.nip44Decrypt(Crypt.conversationKey(sec, peer), ct);
                if (pt != null) { learned[0] = four ? "nip04" : "nip44"; return pt; }
            } catch (Throwable ignored) { }
        }
        return null;
    }

    /** Encrypt, sign and publish the reply on the socket that carries this session. */
    private void send(byte[] sec, String peerPk, String enc, WebSocket ws, String payload)
            throws Exception {
        byte[] peer = Nostr.unhex(peerPk);
        String ct = Nip46Core.replyWithNip04(enc)
                ? Crypt.nip04Encrypt(sec, peer, payload)
                : Crypt.nip44Encrypt(Crypt.conversationKey(sec, peer), payload, null);

        /* OUR OWN PUBKEY, ONCE. It is one more point multiplication and it derives from a key that
         * does not change — computing it per reply was a fifth of the work of answering. */
        String pub = myPub(sec);
        long now = System.currentTimeMillis() / 1000;
        String tags = Nostr.tagsJson(java.util.Collections.singletonList(
                          java.util.Arrays.asList("p", peerPk)));
        String eid = Nostr.eventId(pub, now, 24133, tags, ct);

        JSONObject ev = new JSONObject();
        ev.put("id", eid);
        ev.put("pubkey", pub);
        ev.put("created_at", now);
        ev.put("kind", 24133);
        ev.put("tags", new JSONArray(tags));
        ev.put("content", ct);
        ev.put("sig", Nostr.hex(Nostr.sign(Nostr.unhex(eid), sec, null)));

        // The socket was looked up on the work thread and handed in; OkHttp's send() is thread-safe.
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
                                           Nostr.tagsJson(tagList(ev.getJSONArray("tags"))),
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
        if (RunningNote.othersRunning(RunningNote.SIGNER)) {
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
        sec = null;
        handler.post(() -> { closeAll(); thread.quitSafely(); });
        if (cryptoPool != null) try { cryptoPool.shutdownNow(); } catch (Throwable ignored) { }
        running = false;
        paired = 0;
        /* Same as the other half: killed rather than switched off, so nothing has redrawn the
         * shared notification and it would go on naming a signer that has gone. */
        if (RunningNote.othersRunning(RunningNote.SIGNER)) RunningNote.refresh(this);
        super.onDestroy();
    }

    /** org.json → plain lists, so the id is serialized by {@link Nostr#tagsJson} and never by a
     *  JSON library that escapes a forward slash. See the comment there. */
    private static java.util.List<java.util.List<String>> tagList(JSONArray tags) {
        java.util.List<java.util.List<String>> out = new java.util.ArrayList<>();
        for (int i = 0; i < tags.length(); i++) {
            JSONArray t = tags.optJSONArray(i);
            if (t == null) continue;
            java.util.List<String> one = new java.util.ArrayList<>();
            for (int j = 0; j < t.length(); j++) one.add(t.optString(j, ""));
            out.add(one);
        }
        return out;
    }

}
