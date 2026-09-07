package place.poster.app.push;

import android.app.Notification;
import android.app.NotificationManager;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.ParcelFileDescriptor;
import android.service.notification.StatusBarNotification;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.InputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import okhttp3.OkHttpClient;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Real Keystore, OkHttp, Handler, renderer and ACK protocol; no external server or user messages.
 * The service is attached to isolated preferences, not started by Android. FGS/Doze lifecycle and
 * carrier SMS/cellular calls require separate device checks and are not established by this test.
 */
@RunWith(AndroidJUnit4.class)
public class DirectPushDeliveryDeviceTest {
    private static final class Service extends DirectPushService {
        Service(Context context) { attachBaseContext(context); }
    }
    private static Field field(String name) throws Exception {
        Field field = DirectPushService.class.getDeclaredField(name);
        field.setAccessible(true);
        return field;
    }
    private static final class Peer extends WebSocketListener {
        final LinkedBlockingQueue<String> frames = new LinkedBlockingQueue<>();
        volatile WebSocket socket;
        @Override public void onOpen(WebSocket ws, Response response) { socket = ws; }
        @Override public void onMessage(WebSocket ws, String frame) { frames.add(frame); }
        @Override public void onClosing(WebSocket ws, int code, String reason) { ws.close(code, null); }
        JSONObject next() throws Exception {
            String frame = frames.poll(10, TimeUnit.SECONDS);
            assertNotNull("Native socket did not send the expected protocol frame", frame);
            return new JSONObject(frame);
        }
        void ready() { assertTrue(socket.send("{\"type\":\"ready\"}")); }
        void notify(long id, String type, String event) throws Exception {
            JSONObject payload = new JSONObject().put("type", type).put("title", "Direct device test")
                    .put("body", "Synthetic loopback notification").put("eid", event);
            assertTrue(socket.send(new JSONObject().put("type", "notification").put("id", id)
                    .put("payload", payload).toString()));
        }
        void ack(long id) throws Exception {
            JSONObject ack = next();
            assertEquals("ack", ack.optString("type"));
            assertTrue("ACK id must be numeric, not a JSON string", ack.get("id") instanceof Number);
            assertEquals(id, ack.getLong("id"));
        }
    }
    private static StatusBarNotification card(NotificationManager manager, String event) throws Exception {
        for (int attempt = 0; attempt < 50; attempt++) {
            for (StatusBarNotification item : manager.getActiveNotifications()) {
                if (("nostr-" + event).equals(item.getTag())) return item;
            }
            Thread.sleep(100);
        }
        throw new AssertionError("ACKed notification never reached Android's active notifications");
    }
    @Test public void authenticatesRendersAndAcksThenReconnectsWithoutDuplicateCards() throws Exception {
        org.junit.Assume.assumeTrue("Channel assertions require Android 8+", android.os.Build.VERSION.SDK_INT >= 26);
        org.junit.Assume.assumeTrue("Do not interrupt an existing real notification service", !DirectPushService.running);
        Context target = InstrumentationRegistry.getInstrumentation().getTargetContext();
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            try (InputStream stream = new ParcelFileDescriptor.AutoCloseInputStream(
                    InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand(
                            "pm grant " + target.getPackageName() + " android.permission.POST_NOTIFICATIONS"))) {
                byte[] bytes = new byte[1024]; while (stream.read(bytes) != -1) { }
            }
        }
        org.junit.Assume.assumeTrue("Android message notifications unavailable", PushEventService.canNotify(target, false));
        org.junit.Assume.assumeTrue("Android call notifications unavailable", PushEventService.canNotify(target, true));
        String prefix = "direct-device-test-" + System.nanoTime() + "-";
        Context context = new ContextWrapper(target) {
            @Override public SharedPreferences getSharedPreferences(String name, int mode) {
                return super.getSharedPreferences(prefix + name, mode);
            }
        };
        MockWebServer server = new MockWebServer();
        Peer first = new Peer(), second = new Peer();
        server.enqueue(new MockResponse().withWebSocketUpgrade(first));
        server.enqueue(new MockResponse().withWebSocketUpgrade(second));
        server.start();
        Service service = new Service(context);
        OkHttpClient client = new OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build();
        NotificationManager manager = (NotificationManager) target.getSystemService(Context.NOTIFICATION_SERVICE);
        String event = prefix + "message", call = prefix + "call";
        try {
            String url = server.url("/api/push/direct/ws").toString().replace("http://", "ws://");
            DirectPushStore.save(context, url, "synthetic-device-token", DirectPushStore.deviceId(context));
            field("client").set(service, client);
            DirectPushService.running = true;
            DirectPushService.connected = false;
            Method connect = DirectPushService.class.getDeclaredMethod("connectNow");
            connect.setAccessible(true);
            connect.invoke(service);
            JSONObject auth = first.next();
            assertEquals("auth", auth.optString("type"));
            assertEquals("synthetic-device-token", auth.optString("token"));
            assertFalse("HTTP upgrade alone must not report authenticated", DirectPushService.connected);
            first.ready();
            first.notify(771001L, "test", event); first.ack(771001L);
            assertTrue(DirectPushService.connected);
            StatusBarNotification message = card(manager, event);
            assertEquals("pcai_messages", message.getNotification().getChannelId());
            first.notify(771002L, "call", call); first.ack(771002L);
            Notification incoming = card(manager, call).getNotification();
            assertEquals("pcai_calls", incoming.getChannelId());
            assertEquals(Notification.CATEGORY_CALL, incoming.category);
            assertNotNull("Incoming internet call lost its full-screen intent", incoming.fullScreenIntent);
            assertTrue(first.socket.close(1001, "synthetic server restart"));
            assertEquals("auth", second.next().optString("type"));
            second.ready(); second.notify(771001L, "test", event); second.ack(771001L);
            // notify() is asynchronous; an immediate shade read alone can miss a duplicate redraw.
            for (int attempt = 0; attempt < 10; attempt++) {
                Thread.sleep(100);
                assertEquals("Replay redrew a card instead of using its persisted receipt",
                        message.getPostTime(), card(manager, event).getPostTime());
            }
        } finally {
            DirectPushService.running = false;
            DirectPushService.connected = false;
            field("generation").setInt(service, field("generation").getInt(service) + 1);
            ((Handler) field("handler").get(service)).removeCallbacksAndMessages(null);
            WebSocket active = (WebSocket) field("socket").get(service);
            if (active != null) active.cancel();
            client.dispatcher().cancelAll(); client.connectionPool().evictAll();
            client.dispatcher().executorService().shutdownNow();
            server.shutdown();
            manager.cancel("nostr-" + event, 1002); manager.cancel("nostr-" + call, 1001);
            if (place.poster.app.RunningNote.othersRunning(place.poster.app.RunningNote.DIRECT)) {
                place.poster.app.RunningNote.refresh(target);
            } else manager.cancel(place.poster.app.RunningNote.ID);
            context.getSharedPreferences(PushEventService.PREFS, Context.MODE_PRIVATE).edit().clear().commit();
        }
    }
}
