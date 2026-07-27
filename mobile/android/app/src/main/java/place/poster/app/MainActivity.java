package place.poster.app;

import android.content.Intent;
import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

import place.poster.app.nip55.Nip55Plugin;
import place.poster.app.screenshare.ScreenSharePlugin;
import place.poster.app.share.ShareTargetPlugin;
import place.poster.app.tor.OrbotPlugin;

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
        if (isSend(getIntent())) shareNonce++;   // cold-started BY a share
        super.onCreate(savedInstanceState);
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
