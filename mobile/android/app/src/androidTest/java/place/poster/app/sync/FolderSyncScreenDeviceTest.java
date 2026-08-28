package place.poster.app.sync;

import static org.junit.Assert.assertTrue;

import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import place.poster.app.MainActivity;

/** Opens the shipped Folder Sync screen in Chromium with an older large-folder cache. */
@RunWith(AndroidJUnit4.class)
public final class FolderSyncScreenDeviceTest {
    @Test public void openingEstablishedLargeFolderUsesCacheAndKeepsRendererAlive() throws Exception {
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            WebView web = null;
            for (int i = 0; i < 80 && web == null; i++) {
                scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
                web = ref.get(); if (web == null) SystemClock.sleep(100);
            }
            assertTrue("MainActivity never created its WebView", web != null);
            String ready = "";
            for (int i = 0; i < 150; i++) {
                ready = eval(web, "document.readyState+'|'+!!window.PCSync+'|'+!!window.__PC");
                if (ready.contains("complete|true|true")) break;
                SystemClock.sleep(100);
            }
            assertTrue("bundled Folder Sync client never became ready: " + ready,
                    ready.contains("complete|true|true"));

            // `fullAt` is deliberately absent: this is the cache shape already-installed APKs have.
            // Before the regression fix, merely painting this established folder called stateS.load,
            // which converted that old cache into a full network read and decrypted every record on
            // Chromium's UI thread. Twelve thousand realistic entries is the reported failure class.
            eval(web, "(()=>{window.__fsDevice='seeding';window.__fsStatePosts=0;"
                    + "const oldFetch=window.fetch;window.fetch=function(i,o){const u=typeof i==='string'?i:(i&&i.url)||'';"
                    + "if(u.indexOf('/client/sync-state')>=0)window.__fsStatePosts++;return oldFetch(i,o);};"
                    + "const me=(__PC.me&&__PC.me())||null,suffix=(me&&me.pubkey)||'anon',key='DevicePictures';"
                    + "localStorage.setItem('pc_sync_folders_'+suffix,JSON.stringify([{id:'content://device/large',"
                    + "key:key,dir:'Pictures',excludes:[],prefs:{paused:true},lastSyncAt:1,lastScanOkAt:1}]));"
                    + "const entries={};for(let n=0;n<12000;n++)entries['DCIM/Camera/IMG_'+String(n).padStart(5,'0')+'.jpg']="
                    + "{sha:String(n).padStart(64,'0').slice(-64),size:3145728,mtime:1700000000000+n};"
                    + "const rq=indexedDB.open('pcsync',1);rq.onupgradeneeded=()=>{if(!rq.result.objectStoreNames.contains('base'))"
                    + "rq.result.createObjectStore('base');};rq.onerror=()=>window.__fsDevice='error:'+rq.error;rq.onsuccess=()=>{"
                    + "const tx=rq.result.transaction('base','readwrite');tx.objectStore('base').put({era:'old',cursor:99,entries:entries,d2p:{}},'state:'+key);"
                    + "tx.oncomplete=()=>{const prior=__PC.switchView._osIn;__PC.switchView._osIn=1;"
                    + "try{__PC.switchView('sync');window.__fsDevice='opened';}catch(e){window.__fsDevice='error:'+e;}"
                    + "finally{__PC.switchView._osIn=prior;}};tx.onerror=()=>window.__fsDevice='error:'+tx.error;};return true;})()");

            String result = "";
            for (int i = 0; i < 150; i++) {
                result = eval(web, "(()=>{if(String(window.__fsDevice||'').indexOf('error:')===0)return window.__fsDevice;"
                        + "return JSON.stringify({opened:window.__fsDevice==='opened',posts:window.__fsStatePosts,"
                        + "view:__PC.isView('sync'),card:!!document.querySelector('.sync-card'),alive:true});})()");
                if (result.contains("error:") || (result.contains("\\\"opened\\\":true")
                        && result.contains("\\\"view\\\":true") && result.contains("\\\"card\\\":true"))) break;
                SystemClock.sleep(100);
            }
            assertTrue("Folder Sync did not survive opening the older large cache: " + result,
                    result.contains("\\\"alive\\\":true") && result.contains("\\\"view\\\":true")
                    && result.contains("\\\"card\\\":true") && result.contains("\\\"posts\\\":0"));

            // Opening is not the end of the failure window. paint() coalesces another pass and
            // startAll() schedules its startup nudge; the reported APK appeared briefly and then
            // Chromium died. Keep the real renderer alive through both delayed turns and prove the
            // screen still did not promote this UI-only cache read into a network/full-state read.
            SystemClock.sleep(2500);
            String settled = eval(web, "(()=>JSON.stringify({alive:true,view:__PC.isView('sync'),"
                    + "card:!!document.querySelector('.sync-card'),posts:window.__fsStatePosts,"
                    + "marker:window.__fsDevice}))()");
            assertTrue("Folder Sync crashed after its delayed paint/startup work: " + settled,
                    settled.contains("\\\"alive\\\":true") && settled.contains("\\\"view\\\":true")
                    && settled.contains("\\\"card\\\":true") && settled.contains("\\\"posts\\\":0"));
        } finally { scenario.close(); }
    }

    private static WebView findWebView(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (!(view instanceof ViewGroup)) return null;
        ViewGroup group = (ViewGroup) view;
        for (int i = 0; i < group.getChildCount(); i++) {
            WebView found = findWebView(group.getChildAt(i)); if (found != null) return found;
        }
        return null;
    }

    private static String eval(WebView web, String js) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<String>("null");
        web.post(() -> web.evaluateJavascript(js, value -> { result.set(value); done.countDown(); }));
        assertTrue("WebView renderer stopped answering JavaScript", done.await(15, TimeUnit.SECONDS));
        return result.get();
    }
}
