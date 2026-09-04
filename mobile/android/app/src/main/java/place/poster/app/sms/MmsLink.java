package place.poster.app.sms;

import java.net.URI;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

import place.poster.app.sync.Json;
import place.poster.app.sync.SyncCrypto;

/**
 * A FILE TOO BIG FOR A PICTURE MESSAGE GOES AS A LINK — the launcher's Texts app's half of it.
 *
 * The client has done this since the share-link page shipped (`sms.js:sendAsLink`): over the
 * carrier's ceiling the file is encrypted under a FRESH RANDOM KEY, the ciphertext is uploaded to
 * the account's Blossom, and what leaves is an ordinary text carrying a link whose FRAGMENT is the
 * key. A fragment is never transmitted, so this node stores bytes it cannot read, and `/f/<sha>`
 * decrypts in the recipient's own browser — which is what makes it work for somebody who has never
 * heard of this app.
 *
 * THE NATIVE TEXTS APP HAD NO SUCH ROUTE, and that is the bug this file closes. The launcher's
 * Texts app (HomeTiles → ThreadListActivity → ThreadActivity) is a plain Activity, not the WebView,
 * so none of sms.js is reachable from it: an oversized video was refused at the picker with
 * `MmsAttachment.tooLargeMessage` — "larger than your carrier's 300 KB MMS limit" — and there was
 * nothing to press. Reported as "it is supposed to send files as a link over blossom if the
 * attachment is too large, but i get error message about it being over carriers limit instead".
 *
 * WHERE THE LINE IS, is measured and not chosen here: `required()` takes the ceilings and the
 * caller reads them from the same platform carrier config the transport itself applies
 * (`MmsSender.videoLimit()` → `SmsManager.getCarrierConfigValues()`), so a SIM that will carry a
 * megabyte is not sent down this path for a 400 KB clip.
 *
 * Android-free, like SyncCrypto and SmsSweep and for the same reason: the link format has to match
 * a page written in JavaScript, byte for byte, or the recipient gets a card that says the link is
 * damaged. `tests/test_android_mms_link.py` therefore RUNS this and decrypts what it produced the
 * way `templates/sharelink.html` does, rather than asserting a description of the format.
 */
final class MmsLink {

    /** The marker `app.js:uploadSharedEnc` writes and `sharelink.html` looks for. */
    static final String MARK = "#pcenc1=";

    private static final SecureRandom RNG = new SecureRandom();

    private MmsLink() { }

    /** Everything only a phone can answer, so the decision and the link format stay testable. */
    interface Io {
        /** This instance — where `/f/<sha>` is served. Empty when nobody has signed in here. */
        String apiBase();
        /** The Blossom server that will hold the ciphertext. Empty when unknown. */
        String mediaBase();
        /** Store one already-encrypted blob; answer its sha256. Throwing means nothing was stored. */
        String upload(byte[] blob) throws Exception;
        /** Put this text on the radio. "" when the radio accepted it, otherwise why it did not. */
        String sendText(String body);
    }

    static final class Result {
        boolean ok;
        String error = "";
        String link = "";
    }

    /** A refusal the caller already knows about, in the shape every other answer here has. */
    static Result refused(String why) {
        Result r = new Result();
        r.error = why;
        return r;
    }

    /**
     * MUST THIS ATTACHMENT GO AS A LINK? True exactly where the MMS transport REFUSES, and nowhere
     * else.
     *
     * A PHOTO IS NOT ON THIS PATH. mmslib resizes an image for the carrier, so an ordinary camera
     * picture still arrives as a real picture message in the recipient's own messaging app, which is
     * what everybody expects of one; turning that into a link would be a regression dressed as a
     * feature. A VIDEO cannot be transcoded by that library at all — it is refused synchronously
     * above the SIM's ceiling (MmsSender.send) precisely because the alternative is an MMSC that
     * silently drops it — and that refusal was the whole dead end.
     *
     * The second case is size for its own sake: above what this phone will stage, no transport of
     * ours can carry it whatever it is.
     */
    static boolean required(String mime, long bytes, int carrierVideoCeiling, int stagingCeiling) {
        if (bytes <= 0) return false;
        if (bytes > stagingCeiling) return true;
        /* The mime is already the normalised one — every caller runs MmsSender.normalizedMime
         * before it stages anything, because a picker hands back "application/octet-stream" for a
         * video as often as not. Re-deriving it here would put a second copy of that table in the
         * one class that must stay Android-free. */
        String type = mime == null ? "" : mime.split(";", 2)[0].trim().toLowerCase(java.util.Locale.ROOT);
        return type.startsWith("video/") && bytes > carrierVideoCeiling;
    }

    /**
     * Encrypt, upload, and text the link.
     *
     * ORDER MATTERS AND IT IS THE SAME RULE AS EVERY OTHER SEND IN THIS APP: nothing is claimed
     * until it happened. The upload comes first, because a text naming a blob that was never stored
     * is worse than no text — the recipient gets a link, opens it, and is told the file is gone.
     */
    static Result send(Io io, String body, byte[] plain, String mime, String name) {
        Result r = new Result();
        if (plain == null || plain.length == 0) { r.error = "missing attachment"; return r; }
        String api = trim(io.apiBase());
        String media = trim(io.mediaBase());
        /* An instance is not optional here and saying so is the honest failure. `/f/<sha>` is a page
         * served by a node, so with none there is no address to send anybody — and a link built on
         * an empty base is a relative one, which in a text message is not a link at all. */
        if (api.isEmpty() || media.isEmpty()) {
            r.error = "This is too big for a picture message. Sign in to your instance once, so this "
                    + "phone knows where to put the file, and it will send as a private link instead.";
            return r;
        }
        String sha;
        byte[] key = new byte[32];
        try {
            RNG.nextBytes(key);
            /* A RANDOM IV, matching app.js's uploadSharedEnc rather than the drive's content-derived
             * one: dedup is meaningless under a per-file key, and there is nothing to gain from two
             * sends of the same clip sharing an address a third party could recognise. */
            byte[] iv = new byte[SyncCrypto.IV_LEN];
            RNG.nextBytes(iv);
            sha = io.upload(SyncCrypto.encrypt(key, plain, iv));
        } catch (Throwable t) {
            r.error = "Could not upload it: " + (t.getMessage() == null ? t.toString() : t.getMessage());
            return r;
        }
        if (sha == null || !sha.matches("[0-9a-fA-F]{64}")) {
            r.error = "The media server did not say where it stored the file.";
            return r;
        }
        r.link = url(api, media, sha.toLowerCase(), key, mime, name);
        String failed = io.sendText(note(body, name, plain.length, r.link));
        if (failed != null && !failed.isEmpty()) { r.error = failed; return r; }
        r.ok = true;
        return r;
    }

    /**
     * The address a stranger opens, with the key on the end of it.
     *
     * `u` IS ONLY ADDED WHEN IT IS NEEDED — when the account's media server is not this node, so the
     * page has nowhere to fetch from — because every character in a text message is one more chance
     * for a linkifier to clip the link. It rides inside the fragment like the key, so it never
     * reaches a server either, and the page refuses one that does not name this same blob.
     */
    static String url(String apiBase, String mediaBase, String sha, byte[] key, String mime, String name) {
        Map<String, Object> meta = new LinkedHashMap<String, Object>();
        meta.put("k", b64u(key));
        meta.put("m", mime == null || mime.isEmpty() ? "application/octet-stream" : mime);
        meta.put("n", name == null ? "" : name);
        if (!sameOrigin(apiBase, mediaBase)) meta.put("u", trim(mediaBase) + "/" + sha);
        /* Json, never org.json: its writer escapes a forward slash, and `u` is a URL. That is on
         * record in this app as the reason quote posts silently never published. */
        return trim(apiBase) + "/f/" + sha + MARK + b64u(SyncCrypto.utf8(Json.write(meta)));
    }

    /**
     * What the recipient reads. Word for word the client's (`sms.js:sendAsLink`), because it is one
     * feature with two implementations and a person receiving the same file from a laptop and from
     * the handset should not be able to tell which sent it.
     */
    static String note(String body, String name, long bytes, String link) {
        StringBuilder out = new StringBuilder();
        if (body != null && !body.trim().isEmpty()) out.append(body).append("\n\n");
        if (name != null && !name.isEmpty()) out.append(name).append(" · ");
        out.append(MmsAttachment.size(bytes))
           .append(" — too big to send as a picture message, so here it is as a private link:\n")
           .append(link);
        return out.toString();
    }

    /** base64url, unpadded — what `b64uDec` on the other end expects. */
    private static String b64u(byte[] raw) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(raw);
    }

    /**
     * Same scheme/host/port, so `/f/<sha>` can serve the bytes itself. An unparseable base counts as
     * different, which only ever adds `u` — the conservative direction, since a `u` the page ignores
     * costs a few characters while a missing one is a file the recipient cannot fetch at all.
     */
    private static boolean sameOrigin(String a, String b) {
        try {
            URI x = new URI(trim(a)), y = new URI(trim(b));
            return x.getScheme() != null && x.getScheme().equalsIgnoreCase(y.getScheme())
                && x.getHost() != null && x.getHost().equalsIgnoreCase(y.getHost())
                && x.getPort() == y.getPort();
        } catch (Throwable t) {
            return false;
        }
    }

    private static String trim(String s) {
        String t = s == null ? "" : s.trim();
        while (t.endsWith("/")) t = t.substring(0, t.length() - 1);
        return t;
    }
}
