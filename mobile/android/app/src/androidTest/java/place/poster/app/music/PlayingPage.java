package place.poster.app.music;

import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.platform.app.InstrumentationRegistry;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import place.poster.app.MainActivity;

/**
 * A TRACK PLAYING IN THE SHIPPED PLAYER, for the tests that press music controls OUTSIDE the page (the
 * notification, the home-screen widget, the launcher's now-playing strip).
 *
 * The same harness MusicCarButtonsDeviceTest uses: a generated WAV through the client's own MusicPlayer,
 * so the state reaches MusicService and its MediaSession by the real path (MusicPlayer -> MusicControls
 * plugin -> MusicService). What a press DID is read off the page's audio element, which is the only
 * thing that makes sound -- never off the service's idea of it. next/prev are counted rather than run:
 * the real ones would fetch and decrypt a library track this device does not have.
 */
public final class PlayingPage {
    private ActivityScenario<MainActivity> scenario;
    private WebView web;

    public static PlayingPage start() throws Exception {
        PlayingPage p = new PlayingPage();
        p.begin();
        return p;
    }

    private void begin() throws Exception {
        scenario = ActivityScenario.launch(MainActivity.class);
        AtomicReference<WebView> ref = new AtomicReference<WebView>();
        for (int i = 0; i < 80 && ref.get() == null; i++) {
            scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
            if (ref.get() == null) SystemClock.sleep(100);
        }
        if (ref.get() == null) throw new AssertionError("MainActivity never created its WebView");
        web = ref.get();
        waitForClientPage();
        String start = "(()=>{const n=240000,b=new ArrayBuffer(44+n),v=new DataView(b);"
                + "const s=(o,x)=>{for(let i=0;i<x.length;i++)v.setUint8(o+i,x.charCodeAt(i))};"
                + "s(0,'RIFF');v.setUint32(4,36+n,true);s(8,'WAVEfmt ');v.setUint32(16,16,true);"
                + "v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);"
                + "v.setUint32(28,8000,true);v.setUint16(32,1,true);v.setUint16(34,8,true);"
                + "s(36,'data');v.setUint32(40,n,true);for(let i=44;i<44+n;i++)"
                + "v.setUint8(i,128+Math.round(32*Math.sin(2*Math.PI*440*(i-44)/8000)));"
                + "const A=window.Audio;window.Audio=function(...x){const a=new A(...x);"
                + "window.__pcBackgroundAudio=a;return a};"
                + "const p=__PC.MusicPlayer,idx=__PC.filesIdx();p.ensure();window.Audio=A;"
                + "idx.data.files['device-test-track']={name:'tap-device-test.wav',"
                + "folder:'Music',enc:true,mime:'audio/wav'};p.cur='device-test-track';"
                + "p.queue=['device-test-track'];"
                + "window.__pcNext=0;window.__pcPrev=0;"
                + "p.next=function(){window.__pcNext++};p.prev=function(){window.__pcPrev++};"
                + "const a=window.__pcBackgroundAudio;"
                + "a.src=URL.createObjectURL(new Blob([b],{type:'audio/wav'}));a.loop=true;"
                + "return a.play().then(()=>{p._media();return a.currentTime})})()";
        eval(start);
        expect("the injected track began playing", "window.__pcBackgroundAudio.currentTime>0.15");
        long end = SystemClock.uptimeMillis() + 5000;
        while (SystemClock.uptimeMillis() < end) {
            MusicService s = MusicService.INSTANCE;
            if (s != null && s.sessionActive() && MusicService.nowPlaying()) return;
            SystemClock.sleep(100);
        }
        throw new AssertionError("the PosterChanMusic session never became active for the playing track");
    }

    public boolean paused() throws Exception { return "true".equals(eval("window.__pcBackgroundAudio.paused===true")); }

    /** Polls the page until `js` is true -- a press is asynchronous by nature (service -> WebView). */
    public void expect(String what, String js) throws Exception {
        long end = SystemClock.uptimeMillis() + 8000;
        while (SystemClock.uptimeMillis() < end) {
            if ("true".equals(eval(js))) return;
            SystemClock.sleep(100);
        }
        throw new AssertionError(what + " -- the page's music never got there (" + js + "). service="
                + (MusicService.INSTANCE != null) + " note=" + MusicService.note
                + " presses=" + PressLog.recent());
    }

    /** Closes the page FIRST and the service after, then waits for the service to be gone: stopped
     *  first, a page still playing pushes its next state and starts a fresh service behind the test. */
    public void close() {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        try { eval("(()=>{try{__PC.MusicPlayer.close&&__PC.MusicPlayer.close()}catch(e){}"
                + "try{window.__pcBackgroundAudio.pause()}catch(e){}return 1})()"); } catch (Throwable ignored) { }
        try { if (scenario != null) scenario.close(); } catch (Throwable ignored) { }
        try { ctx.startService(MusicService.intent(ctx, MusicService.ACTION_STOP)); }
        catch (Throwable ignored) { }
        long end = SystemClock.uptimeMillis() + 10_000;
        while (MusicService.INSTANCE != null && SystemClock.uptimeMillis() < end) SystemClock.sleep(100);
    }

    public String eval(String js) throws Exception {
        for (int attempt = 0; ; attempt++) {
            CountDownLatch done = new CountDownLatch(1);
            AtomicReference<String> result = new AtomicReference<String>("null");
            web.post(() -> web.evaluateJavascript(js, value -> { result.set(value); done.countDown(); }));
            if (done.await(15, TimeUnit.SECONDS)) return result.get();
            if (attempt >= 1) throw new AssertionError("WebView did not answer JavaScript (twice, 15s each)");
        }
    }

    private void waitForClientPage() throws Exception {
        String prior = "";
        for (int i = 0; i < 120; i++) {
            String state = eval("location.href+'|'+document.readyState");
            boolean client = state.contains("https://localhost/") || state.contains("capacitor://localhost/");
            if (client && state.contains("|complete") && state.equals(prior)) return;
            prior = state;
            SystemClock.sleep(100);
        }
        throw new AssertionError("bundled client page never became stable: " + prior);
    }

    private static WebView findWebView(View v) {
        if (v instanceof WebView) return (WebView) v;
        if (!(v instanceof ViewGroup)) return null;
        ViewGroup g = (ViewGroup) v;
        for (int i = 0; i < g.getChildCount(); i++) {
            WebView found = findWebView(g.getChildAt(i));
            if (found != null) return found;
        }
        return null;
    }
}
