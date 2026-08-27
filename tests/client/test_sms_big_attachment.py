"""A FILE TOO BIG FOR A PICTURE MESSAGE GOES AS A LINK.

An oversized MMS does not fail in a way anybody can act on. The carrier's MMSC re-compresses it into
mush, or accepts it and delivers nothing, or the transaction times out minutes later with the message
sitting in the thread looking sent. There is no error to show and nothing to retry — which is why the
size is checked BEFORE sending rather than fallen back to afterwards.

What replaces it is a text message carrying a link. The file is encrypted under a fresh random key,
the ciphertext is uploaded, and the KEY TRAVELS IN THE URL FRAGMENT — never transmitted to a server,
so the node holds bytes it cannot read, and `/f/<sha>` decrypts in the recipient's own browser. That
last part is what makes it work for someone who has never heard of this app; the end-to-end proof
that a stranger's browser can actually open one is scripts/check_sharelink.py.

Plaintext was the obvious alternative and is worse than it looks: Blossom has no read authorization
and `GET /list/<pubkey>` enumerates a sender's blobs, so an unencrypted attachment is not merely
guessable by URL, it is LISTABLE by anyone who knows the sender.

Each check here was verified to fail with its rule removed.

Run: venv-unified/bin/python -m pytest tests/client/test_sms_big_attachment.py
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "sms_sim.js")
NODE = shutil.which("node")

KB = 1024


def run(**opts):
    opts.setdefault("canRead", True)
    r = subprocess.run([NODE, SIM, json.dumps(opts)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def calls_of(res, name):
    return [c for c in res["calls"] if c[0] == name]


def result(res):
    got = calls_of(res, "sendFileResult")
    assert got, "the send never produced a result"
    return got[0]


@unittest.skipIf(not NODE, "no node on this node")
class TooBigForMms(unittest.TestCase):

    def test_a_small_photo_still_goes_as_a_picture_message(self):
        """The fallback must not become the ordinary path. An MMS that fits is what people expect
        and it lands in the recipient's normal messaging app with no link to open."""
        res = run(steps=["sendfile:+15550100:%d" % (40 * KB)], mmsLimit=300 * KB)
        self.assertTrue(calls_of(res, "sendMms"), "a small photo did not go as an MMS")
        self.assertFalse(calls_of(res, "uploadSharedEnc"), "a small photo was uploaded anyway")
        ok, where, link = result(res)[1:]
        self.assertTrue(ok)
        self.assertNotEqual(where, "link")

    def test_an_oversized_photo_goes_as_a_text_with_a_link(self):
        res = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=300 * KB)
        self.assertFalse(calls_of(res, "sendMms"),
                         "an oversized file was still handed to the MMS transport")
        self.assertTrue(calls_of(res, "uploadSharedEnc"), "the file was never uploaded")
        ok, where, link = result(res)[1:]
        self.assertTrue(ok, link)
        self.assertEqual(where, "link")
        self.assertIn("/f/", link)
        self.assertIn("#pcenc1=", link)

    def test_a_completed_phone_link_send_is_not_reported_as_waiting(self):
        """Local `where:link` means the text already crossed this phone's radio."""
        js = open(os.path.join(ROOT, "static", "js", "client", "sms.js"), encoding="utf-8").read()
        start = js.index("PC.toast(r.where === 'link'")
        done = js[start:start + 500]
        self.assertIn("sent as a private Files link", done)
        self.assertIn("r.where === 'phone' ? 'sent'", done)
        self.assertIn("waiting for your phone to send it", done)

    def test_webui_queues_an_oversized_photo_as_an_encrypted_link(self):
        """A browser has no carrier plugin to call locally; the link itself must be queued as the
        SMS command for the phone instead of queueing an oversized MMS attachment."""
        res = run(isPhone=False, telephony=False,
                  steps=["sendfile:+15550100:%d:web caption" % (900 * KB)],
                  mmsLimit=300 * KB)
        self.assertTrue(calls_of(res, "uploadSharedEnc"), "web never uploaded the encrypted file")
        self.assertFalse(calls_of(res, "sendMms"), "web tried a local carrier MMS transport")
        outbox = [e for e in res["relayEvents"] if e["d"].startswith("pcai:smsout:")]
        self.assertEqual(len(outbox), 1)
        self.assertIn("/f/", outbox[0]["content"])
        self.assertIn("#pcenc1=", outbox[0]["content"])
        command = json.loads(outbox[0]["content"].removeprefix("enc:"))
        self.assertIsNone(command.get("attachment"),
                          "the queued command is still an oversized MMS instead of a text link")
        self.assertEqual(result(res)[2], "queued-link")

    def test_the_key_is_in_the_fragment_and_only_in_the_fragment(self):
        """THE WHOLE PRIVACY CLAIM. A fragment is never transmitted to a server; a query string is.
        If the descriptor ever moved in front of the `#`, the node would receive the key on every
        open and the file would be readable by whoever runs it."""
        res = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=300 * KB)
        link = result(res)[3]
        head, _, frag = link.partition("#")
        self.assertNotIn("pcenc1", head, "the key descriptor is in the path or query, not the fragment")
        self.assertTrue(frag.startswith("pcenc1="), frag[:40])
        self.assertNotIn("?", head, "the link grew a query string — nothing here belongs in one")

    def test_the_link_points_at_this_instances_page_not_at_the_raw_blob(self):
        """A raw Blossom URL is ciphertext to anyone who opens it — a download of unreadable bytes.
        `/f/<sha>` is the page that decrypts, and it is the only thing worth texting to somebody."""
        res = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=300 * KB,
                  apiBase="https://node.example", blobHost="https://node.example")
        link = result(res)[3]
        self.assertTrue(link.startswith("https://node.example/f/"), link)
        self.assertNotIn("/blossom/", link, "the recipient was sent the ciphertext instead")

    def test_a_blob_on_another_media_server_carries_its_own_address(self):
        """The account may point at somebody else's Blossom. The page has nowhere to fetch from
        unless told, and the telling must stay inside the fragment — so it never reaches a server."""
        res = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=300 * KB,
                  apiBase="https://node.example", blobHost="https://other.example")
        link = result(res)[3]
        head, _, frag = link.partition("#")
        self.assertTrue(head.startswith("https://node.example/f/"))
        self.assertNotIn("other.example", head,
                         "the other server's address leaked out of the fragment")
        self.assertIn("pcenc1=", frag)

    def test_the_message_still_carries_what_the_person_typed(self):
        """Sending a photo with a caption must not throw the caption away."""
        res = run(steps=["sendfile:+15550100:%d:look at this" % (900 * KB)], mmsLimit=300 * KB)
        sent = calls_of(res, "send")
        self.assertTrue(sent, "no text message was sent")
        self.assertIn("look at this", sent[0][2])
        self.assertIn("/f/", sent[0][2])

    def test_the_ceiling_comes_from_the_carrier_not_from_a_constant(self):
        """The same 900KB file: under a carrier that allows 2MB it is an ordinary MMS, and under one
        that allows 300KB it is a link. A number compiled into the app cannot tell those apart."""
        big = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=2 * 1024 * KB)
        self.assertTrue(calls_of(big, "sendMms"), "a generous carrier was ignored")
        small = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=300 * KB)
        self.assertTrue(calls_of(small, "uploadSharedEnc"), "a strict carrier was ignored")

    def test_an_absurd_carrier_figure_is_not_obeyed(self):
        """Some configs report nonsense. A 4KB "limit" would send every photo as a link, which is a
        worse outcome than one oversized MMS — so there is a floor under it."""
        res = run(steps=["sendfile:+15550100:%d" % (40 * KB)], mmsLimit=4096)
        self.assertTrue(calls_of(res, "sendMms"),
                        "a misreported 4KB ceiling turned an ordinary photo into a link")

    def test_an_older_app_with_no_such_method_still_sends(self):
        """`mmsLimit` is new. On a build without it the phone must fall back to a documented default
        and carry on — never refuse to send because it could not ask a question."""
        res = run(steps=["sendfile:+15550100:%d" % (40 * KB)], noMmsLimit=True)
        ok = result(res)[1]
        self.assertTrue(ok, "an older build could not send at all")
        self.assertTrue(calls_of(res, "sendMms"))

    def test_a_disconnected_file_store_does_not_disable_carrier_mms(self):
        """Blossom and the carrier are independent networks. An ordinary camera photo exceeds the
        conservative MMS threshold, but losing the optional encrypted-link upload must not disable
        the phone radio too. Fall through to the real MMS transport and let it resize or report its
        own carrier error."""
        res = run(steps=["sendfile:+15550100:%d" % (900 * KB)], mmsLimit=300 * KB, uploadFails=True)
        self.assertTrue(calls_of(res, "sendMms"),
                        "an offline file store prevented the carrier MMS attempt")
        self.assertFalse(calls_of(res, "send"), "a link to nothing was texted")
        self.assertTrue(result(res)[1], result(res))


if __name__ == "__main__":
    unittest.main()
