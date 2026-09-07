package place.poster.app.signer;

import android.content.Context;
import android.content.ContextWrapper;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.HandlerThread;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import okhttp3.OkHttpClient;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Real Android Handler + real OkHttp close handshake, subscriptions and encrypted NIP-46 replies. */
@RunWith(AndroidJUnit4.class)
public class SignerRelayCloseDeviceTest {
    private static final class Service extends SignerRelayService {
        Service(Context context) { attachBaseContext(context); }
    }

    private static Object field(Object object, String name) throws Exception {
        for (Class<?> type=object.getClass(); type!=null; type=type.getSuperclass()) {
            try { Field f=type.getDeclaredField(name); f.setAccessible(true); return f.get(object); }
            catch (NoSuchFieldException ignored) { }
        }
        throw new NoSuchFieldException(name);
    }

    private static void invoke(Service service, String name, Class<?>[] types, Object... args) throws Exception {
        Method m=SignerRelayService.class.getDeclaredMethod(name,types); m.setAccessible(true); m.invoke(service,args);
    }

    private static void owner(Service service, Runnable action) throws Exception {
        CountDownLatch done=new CountDownLatch(1);
        AtomicReference<Throwable> failure=new AtomicReference<>();
        ((Handler)field(service,"handler")).post(()->{
            try { action.run(); } catch(Throwable t) { failure.set(t); } finally { done.countDown(); }
        });
        assertTrue("Signer owner thread stalled",done.await(10,TimeUnit.SECONDS));
        if(failure.get()!=null)throw new AssertionError(failure.get());
    }

    private static final class Relay extends WebSocketListener {
        final LinkedBlockingQueue<String> messages=new LinkedBlockingQueue<>();
        final CountDownLatch closed=new CountDownLatch(1);
        volatile WebSocket socket;
        @Override public void onOpen(WebSocket socket,Response response) { this.socket=socket; }
        @Override public void onMessage(WebSocket socket,String message) { messages.add(message); }
        @Override public void onClosed(WebSocket socket,int code,String reason) { closed.countDown(); }
        JSONArray message() throws Exception {
            String wire=messages.poll(12,TimeUnit.SECONDS);
            assertNotNull("Relay did not receive a subscription or signer answer",wire);
            return new JSONArray(wire);
        }
        String subscriptions(String pubkey) throws Exception {
            String signer=null; boolean sms=false;
            for(int i=0;i<2;i++) {
                JSONArray request=message(); assertEquals("REQ",request.getString(0));
                JSONObject filter=request.getJSONObject(2);
                if(filter.getJSONArray("kinds").getInt(0)==24133) {
                    assertEquals(pubkey,filter.getJSONArray("#p").getString(0));
                    signer=request.getString(1);
                } else {
                    assertEquals(pubkey,filter.getJSONArray("authors").getString(0));
                    sms=true;
                }
            }
            assertNotNull("Signer subscription missing",signer);
            assertTrue("SMS subscription missing",sms); return signer;
        }
    }

    @Test public void restartClose1001ReconnectsResubscribesAndAnswersWithoutAppReload() throws Exception {
        Context context=new ContextWrapper(ApplicationProvider.getApplicationContext()) {
            @Override public SharedPreferences getSharedPreferences(String name,int mode) {
                return super.getSharedPreferences("signer-close-device-test-"+name,mode);
            }
        };
        // Public deterministic test keys, sealed in isolated test preferences, never user keys.
        byte[] phone=new byte[32],peer=new byte[32];phone[31]=11;peer[31]=12;
        String phonePub=SignerKey.store(context,phone),peerPub=Nostr.hex(Nostr.pubkey(peer));
        MockWebServer server=new MockWebServer();
        Relay first=new Relay(),second=new Relay();
        server.enqueue(new MockResponse().withWebSocketUpgrade(first));
        server.enqueue(new MockResponse().withWebSocketUpgrade(second));
        server.start();
        Service service=new Service(context);
        String url=server.url("/relay").toString().replace("http://","ws://");
        try {
            owner(service,()->{
                try {
                    @SuppressWarnings("unchecked") Map<String,Nip46Core.Session> sessions=(Map<String,Nip46Core.Session>)field(service,"sessions");
                    sessions.put(peerPub,new Nip46Core.Session(peerPub,url,"test client","get_public_key","nip04",0));
                    invoke(service,"open",new Class<?>[]{String.class},url);
                } catch(Exception e) { throw new AssertionError(e); }
            });
            first.subscriptions(phonePub);
            AtomicReference<WebSocket> old=new AtomicReference<>();
            owner(service,()->{try {old.set((WebSocket)((Map<?,?>)field(service,"socks")).get(url));}
                catch(Exception e){throw new AssertionError(e);}});
            WebSocketListener oldListener=(WebSocketListener)field(old.get(),"listener");
            assertTrue(first.socket.close(1001,"server restarting"));
            String subscription=second.subscriptions(phonePub);
            assertTrue("Client must acknowledge the server close",first.closed.await(5,TimeUnit.SECONDS));

            AtomicReference<WebSocket> replacement=new AtomicReference<>();
            owner(service,()->{try {replacement.set((WebSocket)((Map<?,?>)field(service,"socks")).get(url));}
                catch(Exception e){throw new AssertionError(e);}});
            assertNotSame(old.get(),replacement.get());assertNotNull(replacement.get());
            // Deliberately delayed callbacks from the retired socket cannot evict the new one.
            oldListener.onClosing(old.get(),1001,"late close");
            // Empty close frames surface as1005; echoing that reserved code makes real OkHttp
            // throw even for an already retired socket. It must normalize to a legal reply.
            oldListener.onClosing(old.get(),1005,"");
            oldListener.onClosed(old.get(),1001,"late closed");
            oldListener.onFailure(old.get(),new java.io.IOException("late failure"),null);
            owner(service,()->{try {assertSame(replacement.get(),((Map<?,?>)field(service,"socks")).get(url));}
                catch(Exception e){throw new AssertionError(e);}});

            JSONObject request=new JSONObject().put("id","after-restart").put("method","get_public_key").put("params",new JSONArray());
            JSONObject event=place.poster.app.sms.SmsOutbox.signed(peer,peerPub,System.currentTimeMillis()/1000,
                24133,java.util.Collections.singletonList(java.util.Arrays.asList("p",phonePub)),
                Crypt.nip04Encrypt(peer,Nostr.unhex(phonePub),request.toString()));
            assertTrue(second.socket.send(new JSONArray().put("EVENT").put(subscription).put(event).toString()));
            JSONArray response=second.message(); assertEquals("EVENT",response.getString(0));
            JSONObject signed=response.getJSONObject(1);
            assertEquals(phonePub,signed.getString("pubkey"));
            assertTrue(Nostr.verify(Nostr.unhex(signed.getString("id")),Nostr.unhex(phonePub),
                Nostr.unhex(signed.getString("sig"))));
            JSONObject answer=new JSONObject(Crypt.nip04Decrypt(peer,Nostr.unhex(phonePub),signed.getString("content")));
            assertEquals("after-restart",answer.getString("id"));
            assertEquals(phonePub,answer.getString("result"));
            assertFalse(answer.has("error"));
        } finally {
            owner(service,()->{try {invoke(service,"closeAll",new Class<?>[0]);}
                catch(Exception e){throw new AssertionError(e);}});
            ((Handler)field(service,"handler")).removeCallbacksAndMessages(null);
            ((HandlerThread)field(service,"thread")).quitSafely();
            Object pool=field(service,"cryptoPool");
            if(pool!=null)((java.util.concurrent.ExecutorService)pool).shutdownNow();
            OkHttpClient client=(OkHttpClient)field(service,"http");
            if(client!=null){client.dispatcher().cancelAll();client.dispatcher().executorService().shutdownNow();client.connectionPool().evictAll();}
            server.shutdown();SignerKey.clear(context);
        }
    }
}
