package place.poster.app.signer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.text.TextUtils;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * NIP-55: this app signing for OTHER apps on the phone. The thing Amber is for.
 *
 * A Nostr app that needs a signature fires `ACTION_VIEW` on a `nostrsigner:` URI. Android offers it
 * to whoever declares that scheme; we answer with `setResult` and finish. THAT IS THE WHOLE
 * ARCHITECTURE, and it is why this is efficient: there is no service, no socket, no wakelock and no
 * WebView. Between requests this app costs exactly nothing, because it is not running. The OS starts
 * it when somebody asks and the process goes away again afterwards.
 *
 * WHICH IS THE POINT OF DOING IT NATIVELY. The previous signer answered over a relay from inside the
 * WebView, so being available meant keeping a browser engine resident — a foreground service, a
 * permanent notification, and the battery to match. Same capability, categorically different cost.
 *
 * THE APPROVAL IS PER CALLING PACKAGE, ASKED ONCE. Being asked to confirm every signature is what
 * makes people turn a signer off; never being asked is not a signer, it is a key on a shelf. A
 * refusal is remembered too, because an app that was told no must not get a fresh dialog on every
 * retry — that is a loop the user can only escape by uninstalling something.
 *
 * WHAT IT DELIBERATELY DOES NOT DO: read the request's contents to decide anything. The kind and the
 * calling package are shown; the plaintext of a `nip44_decrypt` is not inspected, and nothing here
 * logs a request. A signer that reads what it signs is a different product.
 */
public class SignerActivity extends Activity {

    private static final String SCHEME = "nostrsigner:";

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        Intent in = getIntent();
        if (in == null || in.getData() == null) { deny("no request"); return; }

        final String type = str(in.getStringExtra("type"));
        final String id = str(in.getStringExtra("id"));
        // The caller's package. `getCallingPackage()` is only populated for startActivityForResult,
        // which is exactly how NIP-55 clients invoke this; anything else is treated as unknown and
        // still has to be approved, it simply cannot be REMEMBERED (there is no stable name to
        // remember it under, and inventing one would let any app inherit another's grant).
        final String pkg = getCallingPackage();

        byte[] sec = SignerKey.load(this);
        if (sec == null) {
            Toast.makeText(this, "Open PosterChan and turn on \"Sign for other apps\" first",
                           Toast.LENGTH_LONG).show();
            deny("no key on this device");
            return;
        }

        String content = in.getData().toString();
        if (content.startsWith(SCHEME)) content = content.substring(SCHEME.length());
        try { content = Uri.decode(content); } catch (Throwable ignored) { }

        final String body = content;
        String grant = SignerKey.grant(this, pkg);
        if ("never".equals(grant)) { deny("refused"); return; }
        if ("always".equals(grant)) { answer(type, id, body, in, sec); return; }
        ask(type, id, body, in, sec, pkg);
    }

    private void ask(final String type, final String id, final String body, final Intent in,
                     final byte[] sec, final String pkg) {
        String what = describe(type, body);
        String who = pkg == null ? "An app" : pkg;
        new AlertDialog.Builder(this)
                .setTitle("Sign with your Nostr key?")
                .setMessage(who + "\n\n" + what)
                .setCancelable(false)
                .setPositiveButton("Allow", (d, w) -> answer(type, id, body, in, sec))
                .setNeutralButton("Always allow", (d, w) -> {
                    SignerKey.setGrant(this, pkg, "always");
                    answer(type, id, body, in, sec);
                })
                .setNegativeButton("Deny", (d, w) -> {
                    SignerKey.setGrant(this, pkg, "never");
                    deny("denied");
                })
                .show();
    }

    /** A short, honest description. The KIND is the part that matters — 1 posts, 5 deletes. */
    private String describe(String type, String body) {
        if ("get_public_key".equals(type) || TextUtils.isEmpty(type)) return "Read your public key";
        if ("sign_event".equals(type)) {
            try {
                int kind = new JSONObject(body).optInt("kind", -1);
                if (kind == 1) return "Publish a note (kind 1)";
                if (kind == 5) return "Publish a DELETION (kind 5)";
                if (kind == 3) return "Replace your contact list (kind 3)";
                if (kind >= 0) return "Sign an event of kind " + kind;
            } catch (Throwable ignored) { }
            return "Sign an event";
        }
        if (type.startsWith("nip04") || type.startsWith("nip44")) {
            return type.endsWith("_encrypt") ? "Encrypt a message to someone"
                                             : "Decrypt a message sent to you";
        }
        return "Perform: " + type;
    }

    private void answer(String type, String id, String body, Intent in, byte[] sec) {
        try {
            Intent out = new Intent();
            String pub = Nostr.hex(Nostr.pubkey(sec));
            if (id != null) out.putExtra("id", id);
            out.putExtra("package", getPackageName());

            if (TextUtils.isEmpty(type) || "get_public_key".equals(type)) {
                out.putExtra("signature", pub);
                out.putExtra("result", pub);
            } else if ("sign_event".equals(type)) {
                JSONObject ev = new JSONObject(body);
                ev.put("pubkey", pub);
                if (!ev.has("created_at")) ev.put("created_at", System.currentTimeMillis() / 1000);
                if (!ev.has("tags")) ev.put("tags", new JSONArray());
                if (!ev.has("content")) ev.put("content", "");
                String eid = Nostr.eventId(pub, ev.getLong("created_at"), ev.getInt("kind"),
                                           ev.getJSONArray("tags").toString(),
                                           ev.optString("content", ""));
                String sig = Nostr.hex(Nostr.sign(Nostr.unhex(eid), sec, null));
                ev.put("id", eid);
                ev.put("sig", sig);
                // Both keys: the spec says `event`, and clients in the wild read `signature`.
                out.putExtra("event", ev.toString());
                out.putExtra("result", ev.toString());
                out.putExtra("signature", sig);
            } else {
                String peer = str(in.getStringExtra("pubKey"));
                if (peer.isEmpty()) peer = str(in.getStringExtra("current_user"));
                byte[] peerPk = Nostr.unhex(peer);
                String res;
                if ("nip04_encrypt".equals(type)) res = Crypt.nip04Encrypt(sec, peerPk, body);
                else if ("nip04_decrypt".equals(type)) res = Crypt.nip04Decrypt(sec, peerPk, body);
                else if ("nip44_encrypt".equals(type))
                    res = Crypt.nip44Encrypt(Crypt.conversationKey(sec, peerPk), body, null);
                else if ("nip44_decrypt".equals(type))
                    res = Crypt.nip44Decrypt(Crypt.conversationKey(sec, peerPk), body);
                else { deny("unsupported: " + type); return; }
                out.putExtra("signature", res);
                out.putExtra("result", res);
            }
            setResult(RESULT_OK, out);
            finish();
        } catch (Throwable t) {
            // NEVER answer RESULT_OK with nothing in it: a client reads that as a successful empty
            // signature and publishes an unsigned event, or stores an empty decryption over a real
            // message. Failing loudly is the only safe direction here.
            deny("could not sign");
        }
    }

    private void deny(String why) {
        setResult(RESULT_CANCELED, new Intent().putExtra("error", why));
        finish();
    }

    private static String str(String s) { return s == null ? "" : s; }
}
