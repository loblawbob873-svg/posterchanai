package place.poster.app.sms;

import org.json.JSONObject;

import place.poster.app.sync.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * THE ARCHIVE SWEEP, WITHOUT A WEBVIEW.
 *
 * The handset has always had two ways into the Nostr archive and only one of them could back
 * anything up. `SmsOutbox.archiveIncoming` seals a text that arrives while the app is asleep — one
 * message, live, no attachments — and everything else went through `sms.js:mirror()`, which is
 * JavaScript running inside the client's WebView. So the phone's own history reached the relay
 * ONLY while somebody had PosterChan → Texts open on screen.
 *
 * That is not how this phone is used. The launcher's Texts app (ThreadListActivity/ThreadActivity)
 * is native: it reads the provider directly and draws its own screens, and opening it archived
 * nothing, ever. Reported as "should not have to open PosterChan → Texts when we have an android
 * launcher app called Texts", which is exactly right — from outside, the app that shows your
 * messages simply does not sync them, with nothing in any log to say why.
 *
 * A hidden WebView is NOT the way to close that. Chromium throttles a backgrounded one to a stop,
 * which is the whole reason folder sync grew NativeSweep; this is the same answer for the same
 * reason, built out of pieces that already exist — Messages/MmsStore read the provider, SyncCrypto
 * writes drive-format blobs (12-byte IV + AES-GCM under the master key, byte-identical to
 * app.js:_masterEncrypt, so the web client reads them back through encFileUrl with no index entry),
 * SyncNet.putBlob stores them, and SignerRelayService already owns connected relay sockets.
 *
 * WHAT IT REFUSES TO DO, and each of these is a failure this feature has already had:
 *
 *   * A refused attachment MUST NOT COST ITS MESSAGE. This threw in JavaScript, which failed the
 *     row, which froze the high-water mark at it — and the provider answers `since` OLDEST FIRST,
 *     so ten permanent refusals at the old end of the store stood in front of everything newer.
 *     Measured on the reporting handset: 213 rows read, 10 attachments refused, `published: 0`,
 *     the mark unchanged sweep after sweep. From outside that is indistinguishable from a relay
 *     that stopped accepting. Here the reason is recorded ON the attachment, the document is
 *     published naming it, and the mark moves.
 *
 *   * A CHUNK SIZE IS NOT A FILE SIZE. `MmsStore.partBytes` answers null for anything over its
 *     cap, and the native UI hands it caps a camera photo clears easily; read as "no bytes" that
 *     silently drops every large picture. `readWhole` asks the provider how big it is and pages it.
 *
 *   * NOTHING IS PUBLISHED FROM THIS CLASS. It builds signed events and hands them back, so it can
 *     be run off a phone (tests/test_android_sms_sweep.py) and so the caller — which is the only
 *     thing that knows whether a relay is actually connected — decides when the mark moves.
 */
public final class SmsSweep {

    /** Everything the sweep cannot do itself. Kept small so a test can be a whole world. */
    public interface Io {
        /** Provider rows newer than `dateMs`, OLDEST FIRST, attachments filled in. */
        List<SmsMsg> since(long dateMs, int limit) throws Exception;

        /** One attachment's bytes, or a throw carrying what the provider said. */
        byte[] partBytes(SmsPart part) throws Exception;

        /** Seal these bytes under the drive key, store them, and answer the ciphertext hash. */
        String putBlob(byte[] plain, String mime, String name) throws Exception;

        /** NIP-44 to self, signed, kind 30078 at `d=doc` — SmsOutbox's shape exactly. */
        JSONObject seal(String doc, String bodyJson) throws Exception;

        long mark();
        void mark(long dateMs);
    }

    /** What one pass did, in the phone's own words. */
    public static final class Report {
        public int rows;                 // provider rows read
        public int published;            // documents built (the caller sends them)
        public int attachments;          // attachments stored in the encrypted drive
        public int refused;              // attachments the provider would not hand over
        public long mark;                // the mark this pass EARNED — see commit()
        public String error = "";
        public final List<JSONObject> events = new ArrayList<JSONObject>();
        public boolean more;             // the window filled: there is more history behind this
    }

    private SmsSweep() { }

    /**
     * Read one window of history and turn it into signed archive events.
     *
     * `maxRows` bounds a pass because this runs on somebody's phone while they are using it: the
     * complaint that shipped alongside this work was "encrypting and copying messages to blossom
     * makes it glitchy", and an unbounded sweep is how that happens.
     */
    public static Report run(Io io, int maxRows) {
        Report rep = new Report();
        long mark = io.mark();
        rep.mark = mark;
        List<SmsMsg> rows;
        try {
            rows = io.since(mark, maxRows);
        } catch (Throwable t) {
            rep.error = why(t);
            return rep;
        }
        if (rows == null || rows.isEmpty()) return rep;
        rep.rows = rows.size();
        rep.more = rows.size() >= maxRows;

        for (SmsMsg m : rows) {
            JSONObject ev;
            try {
                ev = one(io, m, rep);
            } catch (Throwable t) {
                /* THE ROW IS WHAT FAILED, NOT THE PASS. Stopping here would leave the mark behind
                 * this row for ever and every later message with it — the frozen-mark bug, in a
                 * second language. Nothing after the seal can be retried usefully anyway: a seal
                 * that throws has no key, which the next row will discover for itself. */
                rep.error = why(t);
                break;
            }
            if (ev == null) continue;
            rep.events.add(ev);
            rep.published++;
            /* The mark is a DATE, and it only ever moves forward. Rows arrive oldest-first, but a
             * provider that hands them back out of order must not be able to strand the newest. */
            if (m.date > rep.mark) rep.mark = m.date;
        }
        return rep;
    }

    /**
     * Move the mark, once the caller has actually put the events on a socket.
     *
     * Two-phase on purpose: a sweep that advanced its own mark while no relay was connected would
     * throw away the only copy of that window's work, silently, and the next pass would start after
     * messages nobody ever archived.
     */
    public static void commit(Io io, Report rep) {
        if (rep != null && rep.mark > io.mark()) io.mark(rep.mark);
    }

    /** One provider row as a signed archive document, or null if it cannot be addressed at all. */
    private static JSONObject one(Io io, SmsMsg m, Report rep) throws Exception {
        String addr = m.address == null ? "" : m.address;
        if (addr.isEmpty()) return null;              // an unaddressed row has no stable identity
        String doc = m.docId();

        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("address", addr);
        body.put("body", m.body == null ? "" : m.body);
        body.put("date", m.date);
        body.put("incoming", m.incoming());
        body.put("name", "");
        /* Carried rather than inferred from the attachment list: a picture message whose pictures
         * could not be read is still a picture message, and every reader counts on saying so. */
        if (m.mms) body.put("mms", Boolean.TRUE);
        if (m.failed()) body.put("failed", Boolean.TRUE);
        if (m.pending()) body.put("pending", Boolean.TRUE);
        if (m.error != null && !m.error.isEmpty()) body.put("error", m.error);

        if (!m.parts.isEmpty()) {
            List<Object> att = new ArrayList<Object>();
            for (SmsPart p : m.parts) {
                String sha = "", err = "";
                try {
                    byte[] bytes = io.partBytes(p);
                    if (bytes == null || bytes.length == 0) {
                        throw new Exception("the attachment is empty");
                    }
                    sha = io.putBlob(bytes, p.ct, p.name);
                    rep.attachments++;
                } catch (Throwable t) {
                    /* SAY WHAT THE PROVIDER SAID. An empty bubble on every other device is
                     * indistinguishable from a message that never had a picture; "Photo · <reason>"
                     * is the truth and is also the only way anyone can count how many are
                     * affected. */
                    err = why(t);
                    rep.refused++;
                }
                Map<String, Object> a = new LinkedHashMap<String, Object>();
                a.put("ct", p.ct == null ? "" : p.ct);
                a.put("name", p.name == null ? "" : p.name);
                a.put("bytes", p.bytes);
                a.put("sha", sha);
                a.put("thumb", "");
                /* No thumbnail is produced here — a phone that is decoding and re-encoding every
                 * picture in the background is the "glitchy" report. The reader already draws the
                 * full picture when there is no preview; `nt` says the absence is deliberate, so no
                 * later sweep treats it as work left undone. */
                a.put("nt", 1L);
                if (sha.isEmpty() && !err.isEmpty()) a.put("err", err);
                att.add(a);
            }
            body.put("att", att);
        }
        return io.seal(doc, Json.write(body));
    }

    /* ------------------------------------------------------------------ reading an attachment */

    /** The three provider reads this needs, so the paging below can be run without a phone. */
    public interface Parts {
        /** Null when the part is larger than `maxBytes` — a CAP, never a measurement. */
        byte[] bytes(long partId, int maxBytes);
        byte[] chunk(long partId, long offset, int maxBytes);
        /** The part's real length, or -1 when the provider will not say. */
        long size(long partId);
    }

    /** Whole-file reads are bounded — a video in a text message must not become the phone's RAM. */
    public static final int WHOLE_BYTES = 12 * 1024 * 1024;
    private static final int CHUNK = 512 * 1024;

    /**
     * One attachment's bytes, however large, or a throw naming the reason.
     *
     * A NULL FROM `bytes()` MEANS "BIGGER THAN THE CAP YOU GAVE ME", and reading it as "no bytes"
     * is how every camera photo quietly stopped being archived: the JavaScript side hit exactly
     * this, passing a chunk size where a file size belonged, and the messages came out flagged
     * `mms:true` carrying no attachment — 1,284 of one account's 1,964 archived messages.
     */
    public static byte[] readWhole(Parts parts, long partId) throws Exception {
        byte[] small = parts.bytes(partId, CHUNK);
        if (small != null) return small;

        long size = parts.size(partId);
        if (size < 0) throw new Exception("the provider would not say how large this attachment is");
        if (size > WHOLE_BYTES) {
            throw new Exception("the attachment is " + (size / (1024 * 1024)) + " MB, over the "
                    + (WHOLE_BYTES / (1024 * 1024)) + " MB this phone will copy in one piece");
        }
        byte[] out = new byte[(int) size];
        int have = 0;
        while (have < size) {
            byte[] c = parts.chunk(partId, have, Math.min(CHUNK, (int) (size - have)));
            if (c == null || c.length == 0) {
                throw new Exception("the attachment stopped after " + have + " of " + size + " bytes");
            }
            if (have + c.length > size) throw new Exception("the attachment read past its own length");
            System.arraycopy(c, 0, out, have, c.length);
            have += c.length;
        }
        return out;
    }

    /** A reason a person can act on, bounded so it cannot bloat a document. */
    static String why(Throwable t) {
        String s = t == null ? "" : String.valueOf(t.getMessage());
        if (s == null || s.isEmpty() || "null".equals(s)) {
            s = t == null ? "unknown" : t.getClass().getSimpleName();
        }
        return s.length() > 160 ? s.substring(0, 160) : s;
    }
}
