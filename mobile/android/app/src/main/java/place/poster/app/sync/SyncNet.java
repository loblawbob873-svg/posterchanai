package place.poster.app.sync;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
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

/**
 * The two servers a sweep talks to, in Java: the Blossom media server that holds the encrypted blobs,
 * and this node's {@code /client/sync-manifest}, which holds the shared agreement about what a folder
 * contains.
 *
 * HttpURLConnection, NOT the OkHttp already on the classpath. OkHttp is here for one thing — the
 * signer's WebSocket — and app/build.gradle says so explicitly, because a second HTTP client is a
 * second set of proxy and timeout rules to keep in step. HttpURLConnection is the platform's own and
 * honours the device's proxy settings; Tor on Android is Orbot's VPN mode, which is transparent at
 * the socket layer, so this inherits it exactly as the WebView does.
 *
 * EVERY REQUEST HAS A CEILING. Nothing in the JavaScript path had one, and a socket that dies without
 * an RST — a phone leaving the house, Wi-Fi to cellular — left a sweep stuck on an await that neither
 * resolved nor rejected until the app was force-closed. A read timeout is per-read, so a stalled
 * transfer fails after one quiet minute rather than at some multiple of the file's size.
 *
 * Android-free (no android.* import), so the request SHAPES — which auth event, which headers, which
 * body — are checked against a real HTTP server in `tests/test_android_native_sync_net.py` instead of
 * on a device.
 */
public final class SyncNet {

    /** One JSON POST. Small, so a slow radio still answers inside it. */
    public static final int POST_TIMEOUT_MS = 20000;
    /** One read of a transfer. Not the whole file — see the class comment. */
    public static final int READ_TIMEOUT_MS = 60000;
    public static final int CONNECT_TIMEOUT_MS = 20000;

    private final String apiBase;      // this node, e.g. https://poster.place
    private final String mediaBase;    // the Blossom server, e.g. https://poster.place/blossom
    private final byte[] sec;          // the account's Nostr secret, from the native signer
    private final String pub;

    public SyncNet(String apiBase, String mediaBase, byte[] sec) {
        this.apiBase = trim(apiBase);
        this.mediaBase = trim(mediaBase);
        this.sec = sec;
        this.pub = Nostr.hex(Nostr.pubkey(sec));
    }

    private static String trim(String s) {
        String t = s == null ? "" : s.trim();
        while (t.endsWith("/")) t = t.substring(0, t.length() - 1);
        return t;
    }

    public String pubkey() { return pub; }

    // ------------------------------------------------------------------------ signed events

    /**
     * A signed event as JSON, built by hand.
     *
     * BY HAND because org.json renders a forward slash as {@code \/} — legal JSON, different bytes,
     * a different id, and a signature over something the server will not recompute. That is on record
     * in this app: it is why quote posts silently never published. A 24242 auth carries no slash
     * today and a 27235 carries a URL tomorrow; there is no version of this worth risking twice.
     */
    String signedEvent(int kind, String content, List<List<String>> tags) {
        long now = System.currentTimeMillis() / 1000L;
        String tagsJson = Nostr.tagsJson(tags);
        String id = Nostr.eventId(pub, now, kind, tagsJson, content);
        String sig = Nostr.hex(Nostr.sign(Nostr.unhex(id), sec, null));
        return "{\"id\":\"" + id + "\",\"pubkey\":\"" + pub + "\",\"created_at\":" + now
                + ",\"kind\":" + kind + ",\"tags\":" + tagsJson
                + ",\"content\":\"" + Nostr.escape(content) + "\",\"sig\":\"" + sig + "\"}";
    }

    private String authHeader(int kind, String content, List<List<String>> tags) {
        return "Nostr " + Crypt.b64(SyncCrypto.utf8(signedEvent(kind, content, tags)));
    }

    private static List<String> tag(String... parts) {
        return new ArrayList<String>(Arrays.asList(parts));
    }

    // ------------------------------------------------------------------------------ Blossom

    /**
     * Does the server already hold this blob — and will it keep it?
     *
     * PRESENT IS NOT ENOUGH. Skipping the upload also skips the server's save path, and that save is
     * what clears an expiry when a blob becomes referenced again. A blob carrying one is scheduled
     * for deletion, so recording a manifest entry against it would point every device at bytes due to
     * vanish. Same rule as the browser's `_blobAlreadyStored`, and it must stay the same rule.
     */
    public boolean blobExists(String sha) {
        HttpURLConnection c = null;
        try {
            c = open(mediaBase + "/" + sha, "HEAD", POST_TIMEOUT_MS);
            int code = c.getResponseCode();
            if (code < 200 || code >= 300) return false;
            return c.getHeaderField("X-Expires-At") == null;
        } catch (Exception e) {
            return false;                      // unknown is "upload it", never "skip it"
        } finally {
            if (c != null) c.disconnect();
        }
    }

    public byte[] getBlob(String sha) throws IOException {
        HttpURLConnection c = open(mediaBase + "/" + sha, "GET", READ_TIMEOUT_MS);
        try {
            int code = c.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new IOException("blob " + shortSha(sha) + " unavailable (" + code + ")");
            }
            return drain(c.getInputStream());
        } finally {
            c.disconnect();
        }
    }

    /** Stream one blob straight to disk, so a big file never has to fit in memory twice. */
    public void getBlobTo(String sha, OutputStream sink) throws IOException {
        HttpURLConnection c = open(mediaBase + "/" + sha, "GET", READ_TIMEOUT_MS);
        try {
            int code = c.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new IOException("blob " + shortSha(sha) + " unavailable (" + code + ")");
            }
            copy(c.getInputStream(), sink);
        } finally {
            c.disconnect();
        }
    }

    /**
     * Store one already-encrypted blob and answer its address.
     *
     * `X-Keep` is what exempts it from the media server's age sweep: the bytes are opaque ciphertext,
     * so the server cannot tell that a folder depends on them and the uploader has to say so.
     * `X-No-Mirror` keeps private content off the public backup path. Neither is optional here — the
     * browser sends both, and a blob uploaded by the phone under different rules is a file that
     * disappears from everyone's folder some weeks later.
     */
    public String putBlob(byte[] blob) throws IOException {
        String sha = SyncCrypto.sha256hex(blob);
        HttpURLConnection c = open(mediaBase + "/upload", "PUT", READ_TIMEOUT_MS);
        try {
            c.setDoOutput(true);
            c.setFixedLengthStreamingMode(blob.length);
            c.setRequestProperty("Content-Type", "application/octet-stream");
            c.setRequestProperty("Authorization", authHeader(24242, "Upload blob",
                    Arrays.asList(tag("t", "upload"), tag("x", sha),
                                  tag("expiration", String.valueOf(System.currentTimeMillis() / 1000L + 3600)))));
            c.setRequestProperty("X-No-Mirror", "1");
            c.setRequestProperty("X-Keep", "1");
            OutputStream os = c.getOutputStream();
            os.write(blob);
            os.flush();
            os.close();
            int code = c.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new IOException("upload refused (" + code + ") " + reason(c));
            }
            String body = new String(drain(c.getInputStream()), "UTF-8");
            String got = shaIn(Json.str(Json.obj(Json.parse(body)).get("url"), ""));
            if (got.isEmpty()) got = shaIn(Json.str(Json.obj(Json.parse(body)).get("sha256"), ""));
            /* THE SERVER'S ANSWER IS THE AUTHORITY, and when it disagrees with what we hashed the
             * upload is a failure rather than a curiosity: recording our sha would put a manifest
             * entry against bytes that are not there, and every other device would fail to download
             * a file this one reports as synced. */
            if (!got.isEmpty() && !got.equalsIgnoreCase(sha)) {
                throw new IOException("the media server stored a different blob (" + shortSha(got)
                                      + " for " + shortSha(sha) + ")");
            }
            return sha;
        } finally {
            c.disconnect();
        }
    }

    private static String shaIn(String s) {
        if (s == null) return "";
        for (int i = 0; i + 64 <= s.length(); i++) {
            String w = s.substring(i, i + 64);
            if (w.matches("[0-9a-fA-F]{64}")) return w.toLowerCase();
        }
        return "";
    }

    private static String shortSha(String sha) {
        return sha == null ? "?" : sha.substring(0, Math.min(8, sha.length()));
    }

    // ----------------------------------------------------------------------------- manifest

    /**
     * One {@code /client/sync-manifest} call. `manifest` null reads, non-null writes.
     *
     * A 409 with `collapse` is the server refusing a write that would shrink the folder — deliberately
     * not an ordinary error, because it is the one failure the caller has to be able to answer rather
     * than retry. It comes back as a {@link Collapse}.
     */
    public Map<String, Object> manifest(String folder, Map<String, Object> manifest, boolean force)
            throws IOException, Collapse {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("pubkey", pub);
        body.put("auth", Crypt.b64(SyncCrypto.utf8(
                signedEvent(27235, "sync-manifest", Arrays.asList(tag("p", pub))))));
        body.put("folder", folder);
        if (manifest != null) body.put("manifest", manifest);
        if (force) body.put("force", Boolean.TRUE);

        HttpURLConnection c = open(apiBase + "/client/sync-manifest", "POST", POST_TIMEOUT_MS);
        try {
            byte[] payload = SyncCrypto.utf8(Json.write(body));
            c.setDoOutput(true);
            c.setFixedLengthStreamingMode(payload.length);
            c.setRequestProperty("Content-Type", "application/json");
            OutputStream os = c.getOutputStream();
            os.write(payload);
            os.flush();
            os.close();
            int code = c.getResponseCode();
            InputStream in = (code >= 200 && code < 300) ? c.getInputStream() : c.getErrorStream();
            String text = in == null ? "" : new String(drain(in), "UTF-8");
            Map<String, Object> j;
            try {
                j = Json.obj(Json.parse(text));
            } catch (RuntimeException e) {
                throw new IOException("the server answered something that is not JSON (" + code + ")");
            }
            if (code == 409 && j.get("collapse") != null) {
                Map<String, Object> col = Json.obj(j.get("collapse"));
                throw new Collapse(Json.num(col.get("old"), 0), Json.num(col.get("new"), 0),
                                   Json.str(j.get("error"), "refused"));
            }
            if (code < 200 || code >= 300 || !Json.bool(j.get("ok"), false)) {
                throw new IOException(Json.str(j.get("error"), "manifest " + code));
            }
            return j;
        } finally {
            c.disconnect();
        }
    }

    /** The server refused a write that would shrink the folder. Answerable, not retryable. */
    public static final class Collapse extends Exception {
        public final long oldCount, newCount;

        Collapse(long oldCount, long newCount, String message) {
            super(message);
            this.oldCount = oldCount;
            this.newCount = newCount;
        }

        public long shrink() { return Math.max(0, oldCount - newCount); }
    }

    // ------------------------------------------------------------------------------ plumbing

    private HttpURLConnection open(String url, String method, int readTimeout) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(CONNECT_TIMEOUT_MS);
        c.setReadTimeout(readTimeout);
        c.setInstanceFollowRedirects(true);
        c.setUseCaches(false);
        return c;
    }

    private static String reason(HttpURLConnection c) {
        String r = c.getHeaderField("x-reason");
        if (r != null && !r.isEmpty()) return r;
        try {
            InputStream err = c.getErrorStream();
            if (err != null) {
                String t = new String(drain(err), "UTF-8").trim();
                if (t.length() > 300) t = t.substring(0, 300);
                return t;
            }
        } catch (Exception ignored) { }
        return "";
    }

    private static byte[] drain(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        copy(in, out);
        return out.toByteArray();
    }

    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[64 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        out.flush();
    }
}
