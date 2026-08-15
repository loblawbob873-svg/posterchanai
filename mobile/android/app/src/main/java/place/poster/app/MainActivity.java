package place.poster.app;

import android.content.Intent;
import android.app.DownloadManager;
import android.net.Uri;
import android.os.Environment;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebView;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.widget.Toast;
import org.json.JSONObject;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;

import place.poster.app.gamepad.GamepadPlugin;
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
        registerPlugin(place.poster.app.gamepad.GamepadPlugin.class);
        registerPlugin(place.poster.app.contacts.ContactSyncPlugin.class);
        registerPlugin(place.poster.app.signer.SignerPlugin.class);
        // The native QR scanner. A plugin that lives in this app is NOT auto-discovered, so without
        // this line Capacitor.Plugins.QrScan is simply absent, the client's guarded lookup falls
        // through to the jsQR camera modal, and the fix looks like it shipped and did nothing.
        registerPlugin(place.poster.app.scan.QrScanPlugin.class);
        if (isSend(getIntent())) shareNonce++;   // cold-started BY a share
        super.onCreate(savedInstanceState);
        allowMediaWithoutAGesture();
        catchWebViewDownloads();
        openPopupsInARealBrowser();
        surviveRenderProcessDeath();
    }

    /**
     * A LINK IN AN EMAIL DID NOTHING IN THE APK, and the web client was already doing this right.
     *
     * Mail renders untrusted HTML in a sandboxed iframe, which is why it also injects
     * `<base target="_blank">` and grants `allow-popups allow-popups-to-escape-sandbox` — a comment
     * there records that with a bare `sandbox` a click did nothing at all. That fix is correct in a
     * browser and inert in a WebView: `target="_blank"` asks for a NEW WINDOW, and a WebView refuses
     * to make one unless `setSupportMultipleWindows(true)` is set AND `onCreateWindow` is handled.
     * Neither was, so Chromium dropped the request on the floor — no navigation, no error, no log
     * line. Exactly "can't click email links", and only in the packaged app.
     *
     * The URL is not knowable from `onCreateWindow` itself, so the standard trick applies: hand the
     * platform a throwaway WebView, let it start the navigation there, and read the target out of the
     * first `shouldOverrideUrlLoading`. Then it goes to the system browser as an ordinary Intent,
     * which is what a link out of an email should do — a mail attachment or a tracking link must not
     * open inside the app's own privileged origin.
     *
     * SUBCLASSED from Capacitor's own chrome client rather than replacing it. `BridgeWebChromeClient`
     * is what serves the file chooser, the camera and microphone permission prompts, and JS dialogs;
     * setting a plain `WebChromeClient` here would fix the link and silently break the file picker,
     * which is the kind of trade that gets discovered weeks later.
     */
    private void openPopupsInARealBrowser() {
        try {
            final WebView host = getBridge().getWebView();
            host.getSettings().setSupportMultipleWindows(true);
            host.setWebChromeClient(new com.getcapacitor.BridgeWebChromeClient(getBridge()) {
                @Override
                public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture,
                                              android.os.Message resultMsg) {
                    if (resultMsg == null || !(resultMsg.obj instanceof WebView.WebViewTransport)) {
                        return false;
                    }
                    final WebView probe = new WebView(view.getContext());
                    probe.setWebViewClient(new android.webkit.WebViewClient() {
                        @Override
                        public boolean shouldOverrideUrlLoading(WebView v,
                                android.webkit.WebResourceRequest request) {
                            hand(v, request == null ? null : request.getUrl());
                            return true;
                        }

                        @Override
                        public boolean shouldOverrideUrlLoading(WebView v, String url) {
                            hand(v, url == null ? null : Uri.parse(url));
                            return true;
                        }

                        private void hand(final WebView v, Uri target) {
                            openExternally(target);
                            /* Destroyed on the NEXT loop turn, never inside the callback: tearing a
                             * WebView down while it is dispatching to you is how a native crash gets
                             * introduced by a fix for a dead link. */
                            v.post(new Runnable() {
                                @Override public void run() {
                                    try { v.destroy(); } catch (Throwable ignored) { }
                                }
                            });
                        }
                    });
                    ((WebView.WebViewTransport) resultMsg.obj).setWebView(probe);
                    resultMsg.sendToTarget();
                    return true;
                }
            });
        } catch (Throwable ignored) {
            // Worst case is the behaviour that shipped before this method existed: the link does
            // nothing. It must never be worse than that, and it must never stop the app starting.
        }
    }

    /** Send a URL to whatever the phone uses for it — a browser, or a mail app for `mailto:`. */
    private void openExternally(Uri target) {
        if (target == null) return;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, target)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (Throwable t) {
            // A phone with nothing registered for the scheme (no mail app for a mailto:, say). Say
            // so, because silence here is the very bug this method exists to fix.
            try {
                Toast.makeText(this, "Nothing on this phone can open that link",
                               Toast.LENGTH_SHORT).show();
            } catch (Throwable ignored) { }
        }
    }

    /**
     * A WEBVIEW WITH NO DownloadListener DROPS EVERY DOWNLOAD IT STARTS, SILENTLY.
     *
     * This app never had one. That is not a corner case: the native video controls carry their own
     * ⋮ → Download, and on a post's video it is the obvious thing to press. Pressed, the WebView asks
     * its listener what to do, finds none, and does nothing at all — no file, no error, no toast.
     * Reported exactly that way: "I try to download video from post and nothing happened."
     *
     * The client's own save buttons were never affected and are not changed here — they go through
     * `saveBlobAs`, which writes the bytes itself and opens the share sheet, because a bare
     * `<a download>` is ignored by this same WebView. That workaround is why the gap stayed hidden:
     * everything WE render already routed around the missing listener, so only the controls Android
     * draws for us were broken.
     *
     * http(s) goes to DownloadManager, which is the platform's own downloader: it survives the app
     * being backgrounded, retries, writes into the public Downloads folder and posts its own
     * notification. `blob:` and `data:` cannot — they are WebView-internal and DownloadManager has no
     * way to read them — so those are handed back to the page, which already knows how to save bytes
     * it holds. Falling through silently is what this method exists to stop.
     */
    private void catchWebViewDownloads() {
        try {
            getBridge().getWebView().setDownloadListener(new DownloadListener() {
                @Override
                public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                            String mimeType, long contentLength) {
                    if (url == null) return;
                    String low = url.toLowerCase();
                    if (low.startsWith("blob:") || low.startsWith("data:")) {
                        // The page holds these bytes; the platform does not. Ask it to save them the
                        // way every button in the client already does.
                        final String js = "window.dispatchEvent(new CustomEvent('pcNativeDownload',"
                                + "{detail:{url:" + JSONObject.quote(url) + "}}))";
                        getBridge().getWebView().post(new Runnable() {
                            @Override public void run() {
                                try { getBridge().getWebView().evaluateJavascript(js, null); }
                                catch (Throwable ignored) { }
                            }
                        });
                        return;
                    }
                    try {
                        String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                        DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                        req.setMimeType(mimeType);
                        if (userAgent != null) req.addRequestHeader("User-Agent", userAgent);
                        req.setNotificationVisibility(
                                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                        req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                        DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                        if (dm == null) return;
                        dm.enqueue(req);
                        Toast.makeText(MainActivity.this, "Downloading " + name,
                                       Toast.LENGTH_SHORT).show();
                    } catch (Throwable t) {
                        // SAY SO. The entire bug this fixes was a failure that produced no sign of
                        // itself; replacing it with a different silent one would be no fix at all.
                        Toast.makeText(MainActivity.this, "Could not start that download",
                                       Toast.LENGTH_SHORT).show();
                    }
                }
            });
        } catch (Throwable ignored) {
            // Never fatal: worst case is the behaviour that shipped before this method existed.
        }
    }

    /**
     * A WEBVIEW REFUSES audio.play() THAT NO TAP ASKED FOR, and that is what broke Bluetooth autoplay.
     *
     * `setMediaPlaybackRequiresUserGesture` defaults to TRUE, and nothing here had ever set it. Every
     * other way this app starts audio follows a touch, so it never mattered — until autoplay, whose
     * entire job is to start a song when NOBODY is touching the phone. Measured in a car: the track
     * was selected and loaded, play() returned a rejected promise, and the player sat there looking
     * frozen. The rejection was swallowed (`r.catch(()=>{})` in _resumeOrPlay), so there was nothing
     * on screen and nothing in any log — the app had done everything right and then been told no.
     *
     * The clue was already in the codebase: `_narrateAudio.play().catch(… // autoplay blocked …)`.
     *
     * Safe here because this WebView loads exactly one page — the bundled client. There is no third
     * party that could use it to make noise; the "user gesture" rule exists to stop web pages doing
     * that, and this is the app's own player being asked by the app's own service.
     */
    private void allowMediaWithoutAGesture() {
        try {
            // After super.onCreate(): the bridge and its WebView do not exist before it.
            getBridge().getWebView().getSettings().setMediaPlaybackRequiresUserGesture(false);
        } catch (Throwable ignored) {
            // Never fatal. Worst case is the behaviour that shipped before this line existed.
        }
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

    /* THE CONTROLLER'S WAY IN, and the reason it needs one: a webxdc game driven by a Bluetooth pad
     * works in Firefox on the same tablet and does nothing in this app. The Gamepad API's platform
     * data on Android is fed from generic-motion and key events routed through the content view, and
     * those events arrive HERE first — at the Activity — whether or not the WebView ever makes
     * anything of them. Forwarding is therefore the one thing this side can do that settles it.
     *
     * Both overrides observe and then fall through to super, so nothing that already worked changes:
     * if the WebView does deliver gamepads to the page, the page prefers the real ones and these
     * events are simply counted. The exception is a CONSUMED key — GamepadPlugin.onKey returns true
     * only for a controller button, and swallowing those is deliberate: an unconsumed KEYCODE_DPAD_*
     * moves Android's focus between views, so pressing "up" in a game would walk the focus ring
     * around the page instead. Ordinary keyboard input is never claimed, because it never matches. */
    @Override
    public boolean dispatchGenericMotionEvent(MotionEvent ev) {
        try { GamepadPlugin.onMotion(ev); } catch (Throwable t) { /* never break input */ }
        return super.dispatchGenericMotionEvent(ev);
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent ev) {
        try { if (GamepadPlugin.onKey(ev)) return true; } catch (Throwable t) { /* never break input */ }
        return super.dispatchKeyEvent(ev);
    }
}
