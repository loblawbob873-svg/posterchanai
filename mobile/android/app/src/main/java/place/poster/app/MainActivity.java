package place.poster.app;

import android.content.Intent;
import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

import place.poster.app.screenshare.ScreenSharePlugin;
import place.poster.app.share.ShareTargetPlugin;

public class MainActivity extends BridgeActivity {
    // A plugin that lives IN this app (rather than in an npm package) is not auto-registered by Capacitor —
    // it has to be declared here, before super.onCreate() builds the bridge, or JS never sees it.
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(ScreenSharePlugin.class);
        registerPlugin(ShareTargetPlugin.class);
        super.onCreate(savedInstanceState);
    }

    // A share to an already-running (singleTask) app arrives via onNewIntent. Capacitor does NOT replace the
    // activity's intent, and the send-intent plugin reads getIntent() — so without this the plugin keeps
    // seeing the stale launch intent and a warm share is silently dropped (app foregrounds, nothing happens).
    // setIntent() makes getIntent() return the new SEND intent so the JS foreground re-check can process it.
    @Override
    public void onNewIntent(Intent intent) {
        setIntent(intent);
        super.onNewIntent(intent);
    }
}
