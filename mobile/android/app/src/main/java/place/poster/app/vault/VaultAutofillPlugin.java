package place.poster.app.vault;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.provider.Settings;
import android.view.autofill.AutofillManager;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * The bridge between the vault in the WebView and the autofill service in its own process.
 *
 * vault.js calls put() whenever the vault changes; the service reads what was written the next time
 * some other app shows a login field. Nothing here decrypts anything — the web layer has already
 * done that, and this only re-seals it under a Keystore key so it can sit on disk (see VaultStore).
 */
@CapacitorPlugin(name = "VaultAutofill")
public class VaultAutofillPlugin extends Plugin {

    /** Replace the snapshot. `items` is the JSON array vault.js built. */
    @PluginMethod
    public void put(PluginCall call) {
        String items = call.getString("items", "");
        boolean ok = VaultStore.put(getContext(), items == null ? "" : items);
        JSObject r = new JSObject();
        r.put("ok", ok);
        call.resolve(r);
    }

    /** Signing out, or unpairing: leave nothing behind. */
    @PluginMethod
    public void clear(PluginCall call) {
        VaultStore.clear(getContext());
        call.resolve();
    }

    /**
     * Whether this phone can autofill, and whether WE are the service it uses. The app shows this so
     * "autofill doesn't work" has an answer on screen rather than being a mystery: on most devices
     * the real reason is that another manager (or none) is selected in Settings.
     */
    @PluginMethod
    public void status(PluginCall call) {
        JSObject r = new JSObject();
        boolean supported = VaultStore.autofillSupported();
        r.put("supported", supported);
        boolean enabled = false;
        // The SDK_INT comparison is INLINE, not behind VaultStore.autofillSupported(): lint's NewApi
        // check reads the guard syntactically and cannot see through a helper — and NewApi is
        // fatal-severity here, so it would fail the CI release build rather than the device.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            try {
                AutofillManager am = getContext().getSystemService(AutofillManager.class);
                enabled = am != null && am.hasEnabledAutofillServices();
            } catch (Throwable ignored) {}
        }
        r.put("enabled", enabled);
        call.resolve(r);
    }

    /**
     * Open the OS picker so the user can choose PosterChan. There is no way to set an autofill
     * service programmatically — by design — so the honest thing is to take them to the screen
     * rather than print instructions nobody follows.
     */
    @PluginMethod
    public void requestEnable(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) { call.reject("this Android version has no autofill"); return; }
        Activity act = getActivity();
        if (act == null) { call.reject("no activity"); return; }
        try {
            Intent i = new Intent(Settings.ACTION_REQUEST_SET_AUTOFILL_SERVICE);
            i.setData(android.net.Uri.parse("package:" + getContext().getPackageName()));
            act.startActivity(i);
            call.resolve();
        } catch (Throwable t) {
            // Some OEM builds don't expose that action; fall back to the general settings screen.
            try {
                act.startActivity(new Intent(Settings.ACTION_SETTINGS));
                call.resolve();
            } catch (Throwable t2) {
                call.reject("could not open Android's autofill settings");
            }
        }
    }
}
