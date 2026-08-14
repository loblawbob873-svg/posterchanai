package place.poster.app.signer;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * How the client hands its key to the native signer, and asks what the phone holds.
 *
 * ONE DIRECTION ONLY: the secret goes IN and never comes back out. There is no `getKey`, and adding
 * one would undo the entire point — the key is here precisely so that a script in the WebView cannot
 * read it. `status()` answers with the PUBLIC key, which is public.
 */
@CapacitorPlugin(name = "Signer")
public class SignerPlugin extends Plugin {

    @PluginMethod
    public void status(PluginCall call) {
        JSObject o = new JSObject();
        o.put("have", SignerKey.have(getContext()));
        o.put("pubkey", SignerKey.pubkey(getContext()));
        call.resolve(o);
    }

    /** `sec` is 32 bytes of hex. Returns the x-only pubkey so the UI can show whose key landed. */
    @PluginMethod
    public void enable(PluginCall call) {
        String sec = call.getString("sec");
        if (sec == null || sec.length() != 64) { call.reject("need a 32-byte hex secret"); return; }
        try {
            String pub = SignerKey.store(getContext(), Nostr.unhex(sec));
            JSObject o = new JSObject();
            o.put("pubkey", pub);
            call.resolve(o);
        } catch (Throwable t) {
            // Say which step failed, without ever echoing the input back.
            call.reject("could not store the key on this device");
        }
    }

    @PluginMethod
    public void disable(PluginCall call) {
        SignerKey.clear(getContext());
        call.resolve();
    }
}
