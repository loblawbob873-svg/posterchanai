package place.poster.app.sms;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import place.poster.app.signer.Crypt;
import place.poster.app.signer.Nostr;
import place.poster.app.sync.Json;

/**
 * ✨ A SUGGESTED REPLY TO THE LAST MESSAGE — the phone half.
 *
 * The node's own model drafts one text (POST /api/texts/ai-reply, the same endpoint the web Texts
 * screen uses) and ThreadActivity puts it IN THE COMPOSER, unsent. Nothing here sends a text.
 *
 * HOW THE PHONE PROVES WHO IT IS: this screen is a native Activity with no app session, but it has
 * the account key sealed in the keystore — the one SmsArchive and SmsOutbox already sign with. So
 * the request carries a kind-27235 event whose content is {@link #PURPOSE}, exactly the shape
 * /client/files-index and /client/sync-manifest accept from SyncNet; the server checks it with its
 * own verify_self_auth and then asks the AI gate (nip05_access) about that key.
 *
 * WHAT LEAVES THE PHONE IS THEIR LAST MESSAGE AND NOTHING ELSE — one {me:false, text}. No number, no
 * contact name, no dates, no earlier messages (the same rule as sms.js aiContext).
 *
 * Android-free (no android.* import), so tests/test_android_sms_ai_reply.py compiles and RUNS it
 * with javac against a real HTTP server and feeds what it sent to the real endpoint.
 */
public final class SmsAiReply {
    private SmsAiReply() { }

    public static final String PATH = "/api/texts/ai-reply";
    public static final String PURPOSE = "texts-ai-reply";
    public static final int MAX_CHARS_EACH = 1000;
    public static final int TIMEOUT_MS = 90000;    // the model may have to load after an idle spell

    /** What the call came back with. `refused` = the server said this account may not (hide ✨). */
    public static final class Result {
        public boolean ok, refused;
        public Boolean allowed;           // probe only: null = could not ask
        public String text = "", error = "";
    }

    // ------------------------------------------------------------------ pure: what is sent

    /**
     * The bounded tail, oldest first: [{me, text}]. A picture with no caption is still something
     * the other person SAID, so it is named rather than dropped; a row with nothing at all is not a
     * turn of the conversation. Reactions never reach here — ThreadActivity hands over
     * SmsReactionThread.visible, which has already folded them into chips.
     */
    public static List<Map<String, Object>> context(List<SmsMsg> rows) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        if (rows == null) return out;
        for (SmsMsg m : rows) {
            if (m == null) continue;
            String text = m.body == null ? "" : m.body.replaceAll("\\s+", " ").trim();
            if (text.isEmpty() && (m.mms || !m.parts.isEmpty())) {
                boolean video = false;
                for (SmsPart p : m.parts) if (p.ct != null && p.ct.startsWith("video/")) video = true;
                text = video ? "[a video]" : "[a picture]";
            }
            if (text.isEmpty()) continue;
            if (text.length() > MAX_CHARS_EACH) text = text.substring(0, MAX_CHARS_EACH);
            Map<String, Object> row = new LinkedHashMap<String, Object>();
            row.put("me", Boolean.valueOf(!m.incoming()));
            row.put("text", text);
            out.add(row);
        }
        // THEIR LAST MESSAGE, AND ONLY THAT ("make sure ai for text message only generates a reply from the
        // last message") -- the same rule as sms.js aiContext. Nothing from them = nothing to answer.
        for (int i = out.size() - 1; i >= 0; i--) {
            if (!Boolean.TRUE.equals(out.get(i).get("me"))) {
                List<Map<String, Object>> one = new ArrayList<Map<String, Object>>();
                one.add(out.get(i));
                return one;
            }
        }
        return new ArrayList<Map<String, Object>>();
    }

    /** The request body. `context` null = a probe ("may this account use AI?"). */
    public static String body(List<Map<String, Object>> context, String pubkey, String auth) {
        Map<String, Object> b = new LinkedHashMap<String, Object>();
        if (context == null) b.put("probe", Boolean.TRUE);
        else b.put("messages", context);
        b.put("pubkey", pubkey);
        b.put("auth", auth);
        return Json.write(b);
    }

    /** Should the ✨ be on screen? Only with a server, a key to sign with, and a "yes" from it. */
    public static boolean visible(String apiBase, boolean haveKey, Boolean allowed) {
        return apiBase != null && !apiBase.trim().isEmpty() && haveKey && Boolean.TRUE.equals(allowed);
    }

    /** Read an answer. Never throws; every failure is a sentence a person can read. */
    public static Result parse(int code, String text, boolean probe) {
        Result r = new Result();
        Map<String, Object> j = null;
        try { j = Json.obj(Json.parse(text == null ? "" : text)); } catch (RuntimeException ignored) { }
        String error = j == null ? "" : Json.str(j.get("error"), Json.str(j.get("detail"), ""));
        if (code == 401 || code == 403) {
            r.refused = true;
            if (probe) r.allowed = Boolean.FALSE;
            r.error = error.isEmpty() ? "AI replies are not available for this account." : error;
            return r;
        }
        if (code < 200 || code >= 300 || j == null || !Json.bool(j.get("ok"), false)) {
            r.error = error.isEmpty() ? "The AI could not draft a reply — try again." : error;
            return r;
        }
        if (probe) {
            Object a = j.get("allowed");
            r.allowed = a instanceof Boolean ? (Boolean) a : null;
            r.ok = r.allowed != null;
            if (Boolean.FALSE.equals(r.allowed)) r.refused = true;
            return r;
        }
        String t = Json.str(j.get("content"), "").trim();
        if (t.isEmpty()) { r.error = "The AI did not come up with a reply — try again."; return r; }
        r.ok = true;
        r.text = t;
        return r;
    }

    // --------------------------------------------------------------- per-process probe cache

    /** How long an answer stands. Access can be granted while the app runs; ten minutes is the
     *  longest anyone waits for a newly granted ✨, and one probe per ten minutes is free. */
    public static final long CACHE_MS = 10 * 60 * 1000L;
    private static String cachedFor = "";
    private static Boolean cachedAllowed = null;
    private static long cachedAt;

    /** The last probe's answer for this key, or null when it has not been asked, the key moved, or
     *  the answer is older than CACHE_MS. */
    public static synchronized Boolean cached(String pubkey) {
        if (pubkey == null || !pubkey.equals(cachedFor)) return null;
        if (System.currentTimeMillis() - cachedAt > CACHE_MS) return null;
        return cachedAllowed;
    }

    public static synchronized void remember(String pubkey, Boolean allowed) {
        if (pubkey == null || allowed == null) return;   // "could not ask" is never remembered
        cachedFor = pubkey;
        cachedAllowed = allowed;
        cachedAt = System.currentTimeMillis();
    }

    // ---------------------------------------------------------------------- the one call

    /** POST to the node, signed with `sec`. `context` null = a probe. Blocking; call off the main thread. */
    public static Result call(String apiBase, byte[] sec, List<Map<String, Object>> context) {
        boolean probe = context == null;
        if (apiBase == null || apiBase.trim().isEmpty()) {
            Result r = new Result(); r.error = "This phone is not connected to a PosterChan server."; return r;
        }
        if (sec == null) {
            Result r = new Result();
            r.error = "Open PosterChan and sign in once, so this phone can ask for AI replies.";
            return r;
        }
        if (!probe && context.isEmpty()) {
            Result r = new Result(); r.error = "There is no message to reply to yet."; return r;
        }
        String base = apiBase.trim();
        while (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        HttpURLConnection c = null;
        try {
            String pub = Nostr.hex(Nostr.pubkey(sec));
            String auth = Crypt.b64(signedEvent(sec, pub).getBytes("UTF-8"));
            byte[] payload = body(context, pub, auth).getBytes("UTF-8");
            c = (HttpURLConnection) new URL(base + PATH).openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(20000);
            c.setReadTimeout(TIMEOUT_MS);
            c.setUseCaches(false);
            c.setDoOutput(true);
            c.setFixedLengthStreamingMode(payload.length);
            c.setRequestProperty("Content-Type", "application/json");
            OutputStream os = c.getOutputStream();
            os.write(payload);
            os.close();
            int code = c.getResponseCode();
            InputStream in = (code >= 200 && code < 300) ? c.getInputStream() : c.getErrorStream();
            String text = in == null ? "" : new String(drain(in), "UTF-8");
            return parse(code, text, probe);
        } catch (Exception e) {
            Result r = new Result();
            r.error = "Could not reach the server for a suggested reply.";
            return r;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** A kind-27235 bound to PURPOSE — built by hand for SyncNet.signedEvent's reason (org.json
     *  escapes '/', which changes the bytes and so the id). */
    static String signedEvent(byte[] sec, String pub) {
        long now = System.currentTimeMillis() / 1000L;
        List<List<String>> tags = new ArrayList<List<String>>();
        tags.add(new ArrayList<String>(Arrays.asList("p", pub)));
        String tagsJson = Nostr.tagsJson(tags);
        String id = Nostr.eventId(pub, now, 27235, tagsJson, PURPOSE);
        String sig = Nostr.hex(Nostr.sign(Nostr.unhex(id), sec, null));
        return "{\"id\":\"" + id + "\",\"pubkey\":\"" + pub + "\",\"created_at\":" + now
                + ",\"kind\":27235,\"tags\":" + tagsJson
                + ",\"content\":\"" + Nostr.escape(PURPOSE) + "\",\"sig\":\"" + sig + "\"}";
    }

    private static byte[] drain(InputStream in) throws java.io.IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[8192]; int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        in.close();
        return bo.toByteArray();
    }
}
