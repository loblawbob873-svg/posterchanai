package place.poster.app.scan;

import android.content.Intent;

import androidx.activity.result.ActivityResult;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.zxing.client.android.Intents;
import com.journeyapps.barcodescanner.CaptureActivity;

/**
 * A NATIVE QR scanner, because ours measurably cannot read the codes people actually point it at.
 *
 * THE MEASUREMENT, not a hunch. `scripts/check_qr_scan.py` drives the shipped in-app scanner against
 * a primal.net-shaped payload (QR version 19, 93x93 modules): it reads at 55% frame fill, reads at
 * 45%, and fails at 35% — two pixels per module, which no decoder recovers. Reported four builds
 * running as "primal qr still don't scan", and the person reporting it could read the same code with
 * their phone's own camera app every single time.
 *
 * WHY OURS LOSES, stated plainly so nobody tries to tune it again: the in-app scanner is jsQR, pure
 * JavaScript, decoding a frame that has been drawn into a canvas and scaled down to a fixed pixel
 * budget — because jsQR's cost scales with the frame, so handing it the whole sensor makes it slower
 * and it catches fewer frames. Amber reads these codes off the same screen every time because Amber
 * is native. `_qrDetector`'s own comment has said so for a while; this is acting on it.
 *
 * ZXING, NOT ML KIT. ML Kit's scanner is the obvious choice and it needs Play Services — on a
 * self-hosted, de-Googled app that is the wrong dependency, and the in-app scanner's own comment
 * praises jsQR for needing "nothing from Play Services". zxing-android-embedded bundles the decoder
 * and its own capture Activity, works offline, and asks for the camera permission itself.
 *
 * THE WEB SCANNER STAYS. This is an ADDITION, not a replacement: the client tries the plugin first
 * and falls back to the camera modal, so a browser, the desktop build, and an APK whose scan was
 * cancelled all behave exactly as before. `scan()` resolving with an empty string means "the user
 * backed out", which is not an error and must not be reported as one.
 */
@CapacitorPlugin(name = "QrScan")
public class QrScanPlugin extends Plugin {

    @PluginMethod
    public void scan(PluginCall call) {
        Intent i = new Intent(getContext(), CaptureActivity.class);
        i.setAction(Intents.Scan.ACTION);
        // QR only. The default set includes every 1D format, which costs decode time per frame on
        // exactly the dense codes this exists for.
        i.putExtra(Intents.Scan.FORMATS, "QR_CODE");
        i.putExtra(Intents.Scan.BEEP_ENABLED, false);
        // Portrait-locked would be wrong: a laptop screen is landscape and people turn the phone.
        i.putExtra(Intents.Scan.ORIENTATION_LOCKED, false);
        i.putExtra(Intents.Scan.PROMPT_MESSAGE, "Point at the QR code on the other device");
        startActivityForResult(call, i, "scanResult");
    }

    @ActivityCallback
    private void scanResult(PluginCall call, ActivityResult result) {
        if (call == null) return;                       // the call was already released
        String text = "";
        Intent data = result.getData();
        if (data != null) {
            String r = data.getStringExtra(Intents.Scan.RESULT);
            if (r != null) text = r;
        }
        JSObject out = new JSObject();
        // Empty = backed out. The caller reopens its own scanner rather than showing a failure, so
        // pressing back cannot strand somebody on a screen with no way to scan.
        out.put("text", text);
        call.resolve(out);
    }
}
