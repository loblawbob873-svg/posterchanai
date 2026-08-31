package place.poster.app.push;

import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import androidx.core.app.ServiceCompat;

import org.json.JSONObject;

import java.net.URI;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

import place.poster.app.RunningNote;

/**
 * PosterChan Direct: one authenticated native WebSocket that can notify a closed app.
 *
 * No distributor, Firebase SDK or WebView is involved. Android only permits a reliable app-owned
 * socket while a foreground service is visible, so the shared low-priority PosterChan background
 * notification is an intentional part of the contract. The token is sealed by {@link DirectPushStore}
 * and is never placed in a URL, Intent, notification, log message or JavaScript response.
 */
public class DirectPushService extends Service {
    public static final String ACTION_START = "place.poster.app.DIRECT_PUSH_START";
    public static final String ACTION_STOP = "place.poster.app.DIRECT_PUSH_STOP";

    public static volatile boolean running = false;
    public static volatile boolean connected = false;
    public static volatile String lastError = "";

    private static final long MAX_MESSAGE_BYTES = 64L * 1024L;
    private static final long MAX_BACKOFF_MS = 5L * 60L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private OkHttpClient client;
    private WebSocket socket;
    private int generation = 0;
    private int failures = 0;

    public static boolean configured(Context context) {
        return DirectPushStore.load(context) != null;
    }

    /** Start from an Activity, boot exemption or package-replaced exemption. */
    public static void kick(Context context) {
        Intent intent = new Intent(context, DirectPushService.class).setAction(ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent);
        else context.startService(intent);
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            DirectPushStore.clear(this);
            stopDirect();
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!configured(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }

        // register() can rotate the server token while this service is already connected. A start
        // command is an explicit reload: retire the old socket so the newly sealed token takes
        // effect immediately instead of waiting for an unrelated network failure.
        if (socket != null) {
            generation++;
            WebSocket old = socket;
            socket = null;
            connected = false;
            try { old.close(1000, "credentials refreshed"); } catch (Throwable ignored) { }
        }

        RunningNote.ensureChannel(this);
        running = true; // RunningNote.build reads this, so it must precede startForeground.
        try {
            int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
                    ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE : 0;
            ServiceCompat.startForeground(this, RunningNote.ID, RunningNote.build(this), type);
        } catch (Throwable failure) {
            running = false;
            lastError = "foreground service refused";
            stopSelf();
            return START_NOT_STICKY;
        }

        if (client == null) {
            client = new OkHttpClient.Builder()
                    .pingInterval(30, TimeUnit.SECONDS)
                    .connectTimeout(20, TimeUnit.SECONDS)
                    .readTimeout(0, TimeUnit.MILLISECONDS)
                    .retryOnConnectionFailure(false)
                    .build();
        }
        connectNow();
        return START_STICKY;
    }

    static boolean safeSocketUrl(String value) {
        try {
            URI uri = new URI(value);
            if ("wss".equalsIgnoreCase(uri.getScheme())) return uri.getHost() != null;
            if (!"ws".equalsIgnoreCase(uri.getScheme())) return false;
            String host = uri.getHost();
            return "localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host)
                    || "::1".equals(host);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void connectNow() {
        if (!running || socket != null) return;
        DirectPushStore.Credentials credentials = DirectPushStore.load(this);
        if (credentials == null) {
            lastError = "notification credentials unavailable";
            stopSelf();
            return;
        }
        if (!safeSocketUrl(credentials.socketUrl)) {
            lastError = "notification socket must use wss";
            DirectPushStore.clear(this);
            stopSelf();
            return;
        }
        final int mine = ++generation;
        Request request;
        try {
            // Authentication happens in the first frame. Keeping the token out of the URL prevents
            // it appearing in proxy/access logs and in WebSocket diagnostics.
            request = new Request.Builder().url(credentials.socketUrl).build();
        } catch (Throwable invalid) {
            lastError = "invalid notification socket";
            DirectPushStore.clear(this);
            stopSelf();
            return;
        }
        socket = client.newWebSocket(request, new WebSocketListener() {
            @Override public void onOpen(WebSocket webSocket, Response response) {
                if (mine != generation || !running) { webSocket.close(1000, "stale"); return; }
                failures = 0;
                connected = true;
                lastError = "";
                try {
                    JSONObject auth = new JSONObject();
                    auth.put("type", "auth");
                    auth.put("token", credentials.token);
                    if (!webSocket.send(auth.toString())) throw new IllegalStateException("auth send refused");
                } catch (Throwable failure) {
                    lastError = "could not authenticate notification socket";
                    webSocket.close(1008, "auth failed");
                }
                RunningNote.refresh(DirectPushService.this);
            }

            @Override public void onMessage(WebSocket webSocket, String text) {
                if (mine != generation || text == null || text.length() > MAX_MESSAGE_BYTES) return;
                try {
                    JSONObject message = new JSONObject(text);
                    String type = message.optString("type", "");
                    if ("ping".equals(type)) {
                        webSocket.send("{\"type\":\"pong\"}");
                        return;
                    }
                    if ("pong".equals(type) || "ready".equals(type) || "auth-ok".equals(type)
                            || "ack".equals(type)) return;
                    // Fail closed for future/unknown protocol frames. Only an explicit notification
                    // envelope may reach Android's visible notification renderer.
                    if (!"notification".equals(type) || message.optJSONObject("payload") == null) return;
                    String deliveryId = message.optString("id", "").trim();
                    if (!deliveryId.isEmpty() && deliveryId.length() <= 256
                            && DirectPushStore.wasDelivered(DirectPushService.this, deliveryId)) {
                        sendAck(webSocket, deliveryId); // replay: ACK, but never draw it twice
                        return;
                    }
                    boolean displayed = PushEventService.deliver(DirectPushService.this, text);
                    if (displayed && !deliveryId.isEmpty()
                            && DirectPushStore.markDelivered(DirectPushService.this, deliveryId)) {
                        sendAck(webSocket, deliveryId);
                    }
                } catch (Throwable ignored) {
                    // An unparseable network frame is not a user notification.
                }
            }

            @Override public void onClosing(WebSocket webSocket, int code, String reason) {
                webSocket.close(code, null);
            }

            @Override public void onClosed(WebSocket webSocket, int code, String reason) {
                if (code == 1008 || code == 4001 || code == 4003) {
                    // A rejected/revoked token cannot recover through retries. Forget it so the UI
                    // reports notifications off and can perform a fresh signed registration.
                    DirectPushStore.clear(DirectPushService.this);
                    lastError = "notification authorization expired";
                    running = false;
                    connected = false;
                    socket = null;
                    dropNotification();
                    stopSelf();
                    return;
                }
                failed(mine, webSocket, code == 1000 ? "closed" : "connection closed");
            }

            @Override public void onFailure(WebSocket webSocket, Throwable throwable, Response response) {
                failed(mine, webSocket, "connection failed");
            }
        });
    }

    private static void sendAck(WebSocket webSocket, String id) {
        try {
            long numericId = Long.parseLong(id);
            if (numericId <= 0) return;
            JSONObject ack = new JSONObject();
            ack.put("type", "ack");
            // The server deliberately accepts an integer only. Sending this as a JSON string leaves
            // the durable queue untouched and replays an already displayed card after reconnect.
            ack.put("id", numericId);
            webSocket.send(ack.toString());
        } catch (Throwable ignored) { }
    }

    private void failed(int mine, WebSocket webSocket, String reason) {
        if (mine != generation) return;
        if (socket == webSocket) socket = null;
        connected = false;
        lastError = reason;
        RunningNote.refresh(this);
        if (!running || !configured(this)) return;
        int exponent = Math.min(8, failures++);
        long delay = Math.min(MAX_BACKOFF_MS, 1000L << exponent);
        // A small deterministic spread prevents every phone reconnecting in the same millisecond
        // after an instance comes back, without making behavior impossible to test.
        delay += Math.abs(DirectPushStore.deviceId(this).hashCode() % 750);
        handler.removeCallbacksAndMessages(null);
        handler.postDelayed(this::connectNow, delay);
    }

    private void stopDirect() {
        running = false;
        connected = false;
        generation++;
        handler.removeCallbacksAndMessages(null);
        WebSocket old = socket;
        socket = null;
        if (old != null) try { old.close(1000, "disabled"); } catch (Throwable ignored) { }
        dropNotification();
    }

    private void dropNotification() {
        if (RunningNote.othersRunning(RunningNote.DIRECT)) {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH);
            RunningNote.refresh(this);
        } else {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        }
    }

    @Override public void onDestroy() {
        stopDirect();
        if (client != null) {
            try { client.dispatcher().cancelAll(); } catch (Throwable ignored) { }
            try { client.connectionPool().evictAll(); } catch (Throwable ignored) { }
        }
        super.onDestroy();
    }
}
