package place.poster.app;

import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebView;
import android.widget.Toast;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;

import place.poster.app.nip55.Nip55Plugin;
import place.poster.app.screenshare.ScreenSharePlugin;
import place.poster.app.share.ShareTargetPlugin;
import place.poster.app.tor.OrbotPlugin;
import place.poster.app.vault.VaultAutofillPlugin;

public class MainActivity extends BridgeActivity {
    // Increments on every distinct incoming SEND intent (cold-start launch + each warm onNewIntent). The JS
    // share handler dedups on THIS instead of the payload — so sharing the SAME image again is a NEW share
    // (new nonce), not a duplicate, while a mere resume/re-read keeps the same nonce. ShareTargetPlugin
    // exposes it. Without this, re-sharing the same file was silently swallowed ("worked only once").
    public static int shareNonce = 0;

    private static boolean isSend(Intent i) {
        String a = i == null ? null : i.getAction();
        return Intent.ACTION_SEND.equals(a) || Intent.ACTION_SEND_MULTIPLE.equals(a);
    }

    // A plugin that lives IN this app (rather than in an npm package) is not auto-registered by Capacitor —
    // it has to be declared here, before super.onCreate() builds the bridge, or JS never sees it.
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(ScreenSharePlugin.class);
        registerPlugin(ShareTargetPlugin.class);
        registerPlugin(Nip55Plugin.class);
        registerPlugin(OrbotPlugin.class);
        registerPlugin(VaultAutofillPlugin.class);
        registerPlugin(place.poster.app.push.PushPlugin.class);
        registerPlugin(place.poster.app.music.MusicPlugin.class);
        registerPlugin(place.poster.app.call.CallPlugin.class);
        registerPlugin(place.poster.app.calendar.CalendarPlugin.class);
        registerPlugin(place.poster.app.sync.FolderSyncPlugin.class);
        registerPlugin(place.poster.app.contacts.ContactSyncPlugin.class);
        if (isSend(getIntent())) shareNonce++;   // cold-started BY a share
        super.onCreate(savedInstanceState);
        surviveRenderProcessDeath();
    }

    // THE "app just closes, no error" BUG. The UI runs in the WebView's RENDER process, which Android
    // hosts separately and is free to kill under memory pressure (or which can crash on its own). When
    // that happens the framework calls onRenderProcessGone, and whoever handles it must say so by
    // returning true. Capacitor's BridgeWebViewClient asks its WebViewListeners and returns false when
    // none of them handled it — and false means "I did not handle this", so the system kills OUR process
    // too. No exception, no ANR, no "app has stopped" dialog: the app is simply gone, mid-scroll. That is
    // exactly the reported symptom, and it is unrecoverable by anything on the JS side.
    //
    // A WebView whose renderer is gone can never render again, so recovery is not "reload the page" — the
    // dead view has to go and a fresh one take its place. recreate() does exactly that: a new Activity
    // instance re-runs onCreate above, so Capacitor builds a new bridge and a new WebView, and the old one
    // is destroyed on the way out through Bridge.onDetachedFromWindow().
    //
    // Deliberately NOT the removeView()+destroy() from Android's own onRenderProcessGone sample: that
    // sample owns its WebView, and here Capacitor does. Destroying it by hand leaves bridge.webView
    // pointing at a destroyed object that the teardown we are about to trigger still calls onPause() /
    // handleDestroy() on — trading a silent disappearance for a real crash. Letting the framework destroy
    // it exactly once, on its own path, is both safer and less code. The render process being gone does
    // not invalidate the WebView OBJECT; only its renderer, and lifecycle calls against it are no-ops.
    //
    // And we SAY so. Silently reappearing at the login screen reads like one more mysterious restart; the
    // toast plus the log line are what turn a recurrence into something diagnosable (`adb logcat -s
    // PosterChan`). didCrash() separates a renderer crash from an out-of-memory reclaim — different causes
    // with different fixes, and until now there was no way to tell which one users were hitting.
    private void surviveRenderProcessDeath() {
        if (bridge == null) return;
        bridge.addWebViewListener(
            new WebViewListener() {
                @Override
                public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                    // didCrash() is API 26 and minSdk is 23. The callback itself never fires below 26, but
                    // the SDK_INT guard is what lint's NewApi check reads, and NewApi is fatal-severity —
                    // without it lintVitalRelease fails the CI release build rather than the code failing
                    // on a device.
                    boolean crashed = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && detail != null && detail.didCrash();
                    Log.w("PosterChan", "WebView render process gone (crashed=" + crashed + ") — recovering");
                    // Off the callback stack: recreate() while the framework is still inside the
                    // WebViewClient call is asking for trouble. Returning true first is what stops the
                    // process being killed out from under the restart.
                    new Handler(Looper.getMainLooper())
                        .post(
                            () -> {
                                Toast
                                    .makeText(
                                        MainActivity.this,
                                        crashed ? "PosterChan hit a display error — reloading" : "PosterChan ran low on memory — reloading",
                                        Toast.LENGTH_LONG
                                    )
                                    .show();
                                recreate();
                            }
                        );
                    return true;
                }
            }
        );
    }

    // A share to an already-running (singleTask) app arrives via onNewIntent. Capacitor does NOT replace the
    // activity's intent, and the send-intent plugin reads getIntent() — so without this the plugin keeps
    // seeing the stale launch intent and a warm share is silently dropped (app foregrounds, nothing happens).
    // setIntent() makes getIntent() return the new SEND intent so the JS foreground re-check can process it.
    @Override
    public void onNewIntent(Intent intent) {
        setIntent(intent);
        if (isSend(intent)) shareNonce++;   // a genuinely new share → new nonce
        super.onNewIntent(intent);
    }
}
