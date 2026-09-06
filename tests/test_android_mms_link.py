"""A FILE TOO BIG FOR A PICTURE MESSAGE GOES AS A LINK — from the NATIVE Texts app too.

The client has done this since the share-link page shipped (`sms.js:sendAsLink`): over the carrier's
ceiling the file is encrypted under a fresh random key, the ciphertext is uploaded to Blossom, and
what leaves is a text carrying `/f/<sha>#pcenc1=<key>` — a page that decrypts in the recipient's own
browser, so it works for somebody who has never heard of this app.

THE LAUNCHER'S TEXTS APP IS NOT THAT CLIENT. `HomeTiles.VIEW_TEXTS` opens ThreadListActivity /
ThreadActivity, plain Activities with no WebView in them, so none of sms.js is reachable from that
screen: an oversized video was refused in the PICKER with `MmsAttachment.tooLargeMessage` — "larger
than your carrier's 300 KB MMS limit" — and there was nothing to press. Reported as "it is supposed
to send files as a link over blossom if the attachment is too large, but i get error message about
it being over carriers limit instead".

So this file checks the two halves that can fail silently:

  * THE FORMAT, by RUNNING the shipped Java and then decrypting what it produced exactly the way
    `templates/sharelink.html` does — base64url the fragment, JSON the descriptor, AES-GCM with the
    first twelve bytes of the blob as the IV. A wrong IV slice, a padded base64 or an org.json
    forward-slash escape all produce a link that looks perfect in a text message and a card that
    says "This link is damaged" on the recipient's phone, which nothing on the sending side can see.
  * THE ROUTE, because a correct MmsLink that nothing calls is the bug it was written to fix.

Run: venv-unified/bin/python -m pytest tests/test_android_mms_link.py -q
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app")
SMS = os.path.join(JAVA, "sms")
THREAD = os.path.join(SMS, "ThreadActivity.java")

# MmsLink is Android-free on purpose, so the whole thing runs here. Its dependencies are the same
# ones tests/test_android_native_sync.py compiles for SyncCrypto.
SRC = [
    os.path.join(SMS, "MmsLink.java"),
    os.path.join(SMS, "MmsAttachment.java"),
    os.path.join(JAVA, "sync", "Json.java"),
    os.path.join(JAVA, "sync", "SyncCrypto.java"),
    os.path.join(JAVA, "signer", "Crypt.java"),
    os.path.join(JAVA, "signer", "Nostr.java"),
    os.path.join(JAVA, "signer", "Native.java"),
]

HARNESS = r'''
package place.poster.app.sms;

import java.util.ArrayList;
import java.util.Base64;
import java.util.List;

/** One send through the real MmsLink, with the phone replaced by a recording stub. */
public final class MmsLinkHarness {

    static final class Fake implements MmsLink.Io {
        final String api, media;
        final boolean refuseUpload;
        final List<String> did = new ArrayList<String>();
        byte[] blob;
        String text = "";
        Fake(String api, String media, boolean refuseUpload) {
            this.api = api; this.media = media; this.refuseUpload = refuseUpload;
        }
        public String apiBase() { return api; }
        public String mediaBase() { return media; }
        public String upload(byte[] b) throws Exception {
            did.add("upload");
            if (refuseUpload) throw new java.io.IOException("upload refused (413) too big");
            blob = b;
            return place.poster.app.sync.SyncCrypto.sha256hex(b);
        }
        public String sendText(String body) { did.add("send"); text = body; return ""; }
    }

    static String esc(String s) {
        StringBuilder b = new StringBuilder();
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c == '\n') b.append("\\n");
            else if (c < 0x20 || c > 0x7e) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.toString();
    }

    public static void main(String[] args) throws Exception {
        byte[] plain = new byte[3 * 1024 * 1024];
        for (int i = 0; i < plain.length; i++) plain[i] = (byte) (i * 31 + 7);

        Fake same = new Fake("https://poster.place", "https://poster.place/blossom", false);
        MmsLink.Result r = MmsLink.send(same, "look at this", plain, "video/mp4", "beach.mp4");

        Fake other = new Fake("https://poster.place", "https://media.example.org", false);
        MmsLink.Result r2 = MmsLink.send(other, "", plain, "video/mp4", "beach.mp4");

        Fake broken = new Fake("https://poster.place", "https://poster.place/blossom", true);
        MmsLink.Result r3 = MmsLink.send(broken, "hi", plain, "video/mp4", "beach.mp4");

        Fake nowhere = new Fake("", "", false);
        MmsLink.Result r4 = MmsLink.send(nowhere, "hi", plain, "video/mp4", "beach.mp4");

        int carrier = 292 * 1024, staging = 8 * 1024 * 1024;
        StringBuilder out = new StringBuilder();
        out.append("{\"ok\":").append(r.ok)
           .append(",\"link\":\"").append(esc(r.link)).append('"')
           .append(",\"note\":\"").append(esc(same.text)).append('"')
           .append(",\"blob\":\"").append(Base64.getEncoder().encodeToString(same.blob)).append('"')
           .append(",\"plainSha\":\"").append(place.poster.app.sync.SyncCrypto.sha256hex(plain)).append('"')
           .append(",\"remoteLink\":\"").append(esc(r2.link)).append('"')
           .append(",\"uploadFailed\":{\"ok\":").append(r3.ok)
             .append(",\"error\":\"").append(esc(r3.error)).append('"')
             .append(",\"did\":\"").append(esc(broken.did.toString())).append("\"}")
           .append(",\"noInstance\":{\"ok\":").append(r4.ok)
             .append(",\"error\":\"").append(esc(r4.error)).append('"')
             .append(",\"did\":\"").append(esc(nowhere.did.toString())).append("\"}")
           .append(",\"required\":{")
             .append("\"bigVideo\":").append(MmsLink.required("video/mp4", 4L * 1024 * 1024, carrier, staging))
             .append(",\"smallVideo\":").append(MmsLink.required("video/mp4", 120L * 1024, carrier, staging))
             .append(",\"bigPhoto\":").append(MmsLink.required("image/jpeg", 4L * 1024 * 1024, carrier, staging))
             .append(",\"hugePhoto\":").append(MmsLink.required("image/jpeg", 9L * 1024 * 1024, carrier, staging))
             .append(",\"videoAtCeiling\":").append(MmsLink.required("video/mp4", carrier, carrier, staging))
             .append(",\"generousCarrier\":").append(MmsLink.required("video/mp4", 900L * 1024, 2 * 1024 * 1024, staging))
           .append("}}");
        System.out.println(out.toString());
    }
}
'''


def _java():
    return shutil.which("javac") and shutil.which("java")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@unittest.skipIf(not _java(), "no javac/java on this node")
class TheLinkAStrangerCanOpen(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "MmsLinkHarness.java")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(HARNESS)
            built = subprocess.run(["javac", "-nowarn", "-d", tmp] + SRC + [path],
                                   text=True, capture_output=True)
            assert built.returncode == 0, built.stderr[-4000:]
            ran = subprocess.run(["java", "-cp", tmp, "place.poster.app.sms.MmsLinkHarness"],
                                 text=True, capture_output=True)
            assert ran.returncode == 0, ran.stderr[-4000:]
        cls.out = json.loads(ran.stdout.strip().splitlines()[-1])

    def test_the_recipient_can_actually_decrypt_it(self):
        """sharelink.html's own steps, in Python. This is the only assertion that proves the link
        works: every wrong version of it still produces a plausible-looking URL."""
        self.assertTrue(self.out["ok"], self.out)
        link = self.out["link"]
        m = re.match(r"^https://poster\.place/f/([0-9a-f]{64})#pcenc1=([A-Za-z0-9_-]+)$", link)
        self.assertIsNotNone(m, "not the address the share page serves: " + link)
        sha, frag = m.group(1), m.group(2)

        blob = base64.b64decode(self.out["blob"])
        self.assertEqual(sha, hashlib.sha256(blob).hexdigest(),
                         "the link names an address the ciphertext does not have")

        meta = json.loads(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
        self.assertEqual("video/mp4", meta["m"])
        self.assertEqual("beach.mp4", meta["n"])
        self.assertNotIn("u", meta, "a same-origin link carried a media URL it does not need")

        # base64URL, UNPADDED — a 32-byte key is 44 standard-base64 characters ending in "=", and
        # both that and a "+" or "/" from the standard alphabet are what `b64uDec` on the other end
        # is written around. Asserted rather than merely decoded, because most keys survive the
        # wrong alphabet by luck and the ones that do not fail on a stranger's phone.
        self.assertRegex(meta["k"], r"^[A-Za-z0-9_-]{43}$",
                         "the key is not unpadded base64url: " + meta["k"])
        key = base64.urlsafe_b64decode(meta["k"] + "=" * (-len(meta["k"]) % 4))
        self.assertEqual(32, len(key))
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
        self.assertEqual(self.out["plainSha"], hashlib.sha256(plain).hexdigest(),
                         "the file the recipient gets is not the file that was attached")

    def test_a_media_server_that_is_not_this_node_is_named_in_the_fragment(self):
        """With the account pointed at somebody else's Blossom, `/blossom/<sha>` on this node has
        nothing to serve — so the blob's own URL rides in the fragment, where it never reaches a
        server, and only then (every character is one more chance for a linkifier to clip it)."""
        frag = self.out["remoteLink"].split("#pcenc1=")[1]
        meta = json.loads(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
        sha = self.out["remoteLink"].split("/f/")[1].split("#")[0]
        self.assertEqual("https://media.example.org/" + sha, meta.get("u"))
        # The page only honours a `u` that names this same blob (an unchecked one would make it a
        # request-anywhere gadget), so it has to carry the sha.
        self.assertIn(sha, meta["u"])

    def test_the_text_says_what_it_is_and_carries_the_whole_link(self):
        note = self.out["note"]
        self.assertIn("look at this", note, "what the person typed was dropped")
        self.assertIn("beach.mp4", note)
        self.assertIn("3.0 MB", note)
        self.assertTrue(note.rstrip().endswith(self.out["link"]),
                        "the link must end the message — anything after it gets swallowed by "
                        "linkifiers: " + note)
        # ONE FEATURE, TWO IMPLEMENTATIONS. A person receiving the same file from the laptop and
        # from the handset must not be able to tell which sent it.
        self.assertIn("too big to send as a picture message", note)
        self.assertIn("too big to send as a picture message",
                      _read(os.path.join(ROOT, "static", "js", "client", "sms.js")),
                      "the client's wording moved; the phone's copy is now a different feature")

    def test_a_refused_upload_is_never_reported_as_sent(self):
        """A text naming a blob that was never stored is worse than no text: the recipient opens the
        link and is told the file is gone, with the sender's thread showing it delivered."""
        failed = self.out["uploadFailed"]
        self.assertFalse(failed["ok"])
        self.assertIn("upload refused", failed["error"])
        self.assertEqual("[upload]", failed["did"], "a failed upload still went on the radio")

    def test_with_no_instance_it_says_so_instead_of_texting_a_broken_link(self):
        """`/f/<sha>` is a page a node serves. With no node there is no address to send anybody, and
        a link built on an empty base is a relative one — which in a text message is not a link."""
        none = self.out["noInstance"]
        self.assertFalse(none["ok"])
        self.assertIn("Sign in", none["error"])
        self.assertEqual("[]", none["did"], "it uploaded or texted with nowhere to point")

    def test_the_line_is_the_carriers_own_number_and_photos_stay_picture_messages(self):
        """The ceilings are arguments, measured by the caller from this SIM's carrier config — so a
        network that carries 2 MB does not send a 900 KB clip as a link. And a photo is NOT on this
        path: mmslib resizes an image for the carrier, so it still arrives as a real picture message
        in the recipient's own messaging app; only a video (which that library cannot transcode, and
        which MmsSender therefore refuses outright) and a file past what this phone stages have no
        other route."""
        need = self.out["required"]
        self.assertTrue(need["bigVideo"])
        self.assertFalse(need["smallVideo"])
        self.assertFalse(need["bigPhoto"])
        self.assertTrue(need["hugePhoto"])
        self.assertFalse(need["videoAtCeiling"], "exactly at the ceiling still fits")
        self.assertFalse(need["generousCarrier"], "a carrier's own bigger limit was ignored")


class TheNativeTextsAppTakesThatRoute(unittest.TestCase):
    """A correct MmsLink that nothing calls is the bug it was written to fix."""

    def setUp(self):
        self.thread = _read(THREAD)

    def test_the_picker_no_longer_refuses_a_video_at_the_carrier_ceiling(self):
        picker = self.thread[self.thread.index("if (request != PICK_MMS_IMAGE"):
                             self.thread.index("private byte[] readAttachment")]
        self.assertFalse("MmsSender.videoLimit()" in picker,
                         "the picker still turns the carrier ceiling into a refusal, which is the "
                         "dead end this feature exists to remove")
        self.assertFalse("tooLargeMessage" in picker,
                         "the carrier's sentence is still shown for a file the app can send")
        self.assertNotIn("SmsSweep.WHOLE_BYTES", picker)
        self.assertIn("MmsDraft.save(ThreadActivity.this, who, in", picker)
        self.assertIn("new Thread(", picker)

    def test_the_send_asks_for_the_link_route_before_the_carrier_one(self):
        send = self.thread[self.thread.index("private void sendMms(String body)"):
                           self.thread.index("private void call()")]
        self.assertTrue("MmsLink.required(" in send,
                        "sendMms never asks whether this one has to go as a link")
        self.assertLess(send.index("MmsLink.required("), send.index("MmsSender.send("),
                        "the carrier transport is asked first, so an oversized video is refused "
                        "before anything can offer the link")
        self.assertIn("MmsSender.videoLimit()", send,
                      "the threshold must come from this SIM's carrier config, not a constant")

    def test_the_upload_does_not_run_on_the_main_thread(self):
        link = self.thread[self.thread.index("private void sendAsLink("):
                           self.thread.index("private void call()")]
        self.assertIn("new Thread(", link)
        self.assertLess(link.index("new Thread("), link.index("MmsLink.send("),
                        "MmsLink.send uploads — on the UI thread that is a frozen Texts app")
        self.assertIn("main.post(", link, "the result has to come back to the UI thread")
        self.assertIn("MmsDraft.FAILED", link, "a failed link send must stay retryable")


if __name__ == "__main__":
    unittest.main()
