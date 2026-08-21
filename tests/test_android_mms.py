"""PICTURE MESSAGES — the rules that decide things, RUN, and the two halves checked against each other.

`content://mms` is a different table from `content://sms` and almost nothing carries over: its own
columns, its own message-box constants, the sender in a second table, the content in a third, and a
`date` in SECONDS where the text table's is in milliseconds. Every one of those is a silent failure
when it is wrong — timestamps in 1970, every received photo drawn as a sent one, a thread that sorts
backwards, an archive that never publishes a picture at all — and none of them raises anything.

So the decisions live in pure Java (SmsKeys, SmsMsg, Messages.merge) and this compiles and RUNS them.
The parts that only the platform can answer are asserted against the source, by name, with the reason
each one costs a screen written down beside it.

Each check here was verified to fail with its rule removed.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

ROOT = ac.ROOT
SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
SMSJS = os.path.join(ROOT, "static", "js", "client", "sms.js")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")
NODE = shutil.which("node")
JAR = ac.android_jar()

# Messages is compiled against the real android.jar (Context appears in its signatures) and is loaded
# on a plain JVM, which is safe because `merge` touches none of it. MmsStore is deliberately NOT
# here: its static initialiser calls Uri.parse, and android.jar's copy throws "Stub!" on load.
SOURCES = ["SmsKeys.java", "SmsMsg.java", "SmsPart.java", "Messages.java"]

HARNESS = r"""
package place.poster.app.sms;

import java.util.*;

public class MmsHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }

  static SmsPart part(String ct, String name, long bytes) {
    SmsPart p = new SmsPart();
    p.ct = ct; p.name = name; p.bytes = bytes;
    return p;
  }

  static SmsMsg msg(long id, long date, boolean mms) {
    SmsMsg m = new SmsMsg();
    m.id = id; m.date = date; m.mms = mms; m.address = "+15550104477"; m.threadId = 1;
    m.type = 1;
    return m;
  }

  public static void main(String[] a) {
    // A TEXT-ONLY MESSAGE MUST KEEP THE ADDRESS IT ALREADY HAS. Read back through the MMS path
    // with an empty parts key it is the same message; a second address is a duplicate in the thread.
    SmsMsg plain = msg(1, 1700000000123L, false);
    plain.body = "hello";
    say("plain-matches-sms",
        plain.docId().equals(SmsKeys.docId("+15550104477", 1700000000123L, "hello", true)));

    // TWO PHOTOS IN ONE SECOND, NO CAPTION. Without the attachments in the identity these are one
    // document and one of the two is simply gone from every device that is not the handset.
    SmsMsg a1 = msg(2, 1700000000000L, true);
    a1.parts.add(part("image/jpeg", "beach.jpg", 240000));
    SmsMsg a2 = msg(3, 1700000000000L, true);
    a2.parts.add(part("image/jpeg", "dog.jpg", 190000));
    say("two-photos-two-docs", !a1.docId().equals(a2.docId()));

    // The SAME photo read twice is the same document — identity, not a nonce.
    SmsMsg again = msg(9, 1700000000000L, true);
    again.parts.add(part("image/jpeg", "beach.jpg", 240000));
    say("same-photo-same-doc", a1.docId().equals(again.docId()));

    // ORDER IS PART OF THE MESSAGE. `seq` is the same on every device and two attachments swapping
    // places is a different message, so the key must not be sorted.
    SmsMsg ab = msg(4, 1700000001000L, true);
    ab.parts.add(part("image/jpeg", "a.jpg", 10));
    ab.parts.add(part("image/png", "b.png", 20));
    SmsMsg ba = msg(5, 1700000001000L, true);
    ba.parts.add(part("image/png", "b.png", 20));
    ba.parts.add(part("image/jpeg", "a.jpg", 10));
    say("order-counts", !ab.docId().equals(ba.docId()));

    // A SEPARATOR IN A FILENAME MUST NOT SHIFT THE FIELDS AFTER IT.
    say("separators-escaped", SmsKeys.partKey("image/jpeg", "a;b:c", 5));
    // Two different messages must not agree by accident because one of them had a `;` in a name.
    SmsMsg trick1 = msg(6, 1700000002000L, true);
    trick1.parts.add(part("image/jpeg", "a:1;image/png:b", 1));
    SmsMsg trick2 = msg(7, 1700000002000L, true);
    trick2.parts.add(part("image/jpeg", "a", 1));
    trick2.parts.add(part("image/png", "b", 1));
    say("no-accidental-collision", !trick1.docId().equals(trick2.docId()));

    say("parts-key-shape", ab.partsKey());
    // The two ids the JavaScript half has to reproduce byte for byte.
    say("photo-doc", a1.docId());
    say("twoparts-doc", ab.docId());
    say("empty-parts-key", "[" + plain.partsKey() + "]");

    // THE MERGE. A conversation is texts AND pictures, interleaved by date, and the truncation
    // happens after the merge or recent texts are dropped to make room for old pictures.
    List<SmsMsg> texts = new ArrayList<SmsMsg>();
    List<SmsMsg> pics = new ArrayList<SmsMsg>();
    texts.add(msg(10, 5000L, false));
    texts.add(msg(11, 3000L, false));
    texts.add(msg(12, 1000L, false));
    pics.add(msg(20, 4000L, true));
    pics.add(msg(21, 2000L, true));
    StringBuilder order = new StringBuilder();
    for (SmsMsg m : Messages.merge(texts, pics, 100)) order.append(m.id).append(',');
    say("interleaved", order.toString());

    StringBuilder capped = new StringBuilder();
    for (SmsMsg m : Messages.merge(texts, pics, 3)) capped.append(m.id).append(',');
    say("newest-three", capped.toString());

    // A tie must be broken the same way on every read, or a thread reorders when you reopen it.
    List<SmsMsg> tieA = new ArrayList<SmsMsg>();
    List<SmsMsg> tieB = new ArrayList<SmsMsg>();
    tieA.add(msg(30, 7000L, false));
    tieB.add(msg(31, 7000L, true));
    StringBuilder t1 = new StringBuilder(), t2 = new StringBuilder();
    for (SmsMsg m : Messages.merge(tieA, tieB, 10)) t1.append(m.id).append(',');
    for (SmsMsg m : Messages.merge(tieB, tieA, 10)) t2.append(m.id).append(',');
    say("stable-tie", t1.toString().equals(t2.toString()));
  }
}
"""

NODE_HARNESS = r"""
const { webcrypto } = require('crypto');
global.crypto = webcrypto;
global.window = { __PC: { capPlugin: () => null } };
global.document = { addEventListener(){}, querySelector(){ return null; } };
global.localStorage = { getItem(){ return null; }, setItem(){} };
require(process.argv[2]);
(async () => {
  const S = global.window.PCSms;
  const one = [{ ct:'image/jpeg', name:'beach.jpg', bytes:240000 }];
  const two = [{ ct:'image/jpeg', name:'a.jpg', bytes:10 }, { ct:'image/png', name:'b.png', bytes:20 }];
  console.log(JSON.stringify({
    plain:      await S._docId('+15550104477', 1700000000123, 'hello', true),
    plain_empty:await S._docId('+15550104477', 1700000000123, 'hello', true, ''),
    photo:      await S._docId('+15550104477', 1700000000000, '', true, S._partsKey(one)),
    twoParts:   await S._docId('+15550104477', 1700000001000, '', true, S._partsKey(two)),
    partsKey:   S._partsKey(two),
    escaped:    S._partKey('image/jpeg', 'a;b:c', 5),
  }));
})();
"""


def _java():
    tmp = tempfile.mkdtemp()
    pkg = os.path.join(tmp, "src", "place", "poster", "app", "sms")
    os.makedirs(pkg)
    with open(os.path.join(pkg, "MmsHarness.java"), "w") as f:
        f.write(HARNESS)
    src = [os.path.join(SMS, f) for f in SOURCES] + [os.path.join(pkg, "MmsHarness.java")]
    r = subprocess.run([JAVAC, "-nowarn", "-Xlint:-options", "-source", "11", "-target", "11",
                        "-classpath", JAR, "-d", tmp, "-sourcepath", ac.JAVA] + src,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    r = subprocess.run([JAVARUN, "-cp", tmp + os.pathsep + JAR,
                        "place.poster.app.sms.MmsHarness"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-4000:]
    shutil.rmtree(tmp, ignore_errors=True)
    return dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)


def _node():
    with tempfile.TemporaryDirectory() as tmp:
        h = os.path.join(tmp, "h.js")
        with open(h, "w") as f:
            f.write(NODE_HARNESS)
        r = subprocess.run([NODE, h, SMSJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(JAR is None, "no android.jar on this node")
@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class MmsIdentity(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        cls.out = _java()

    def test_a_text_only_message_keeps_the_address_it_already_had(self):
        """The MMS table carries plenty of messages with words and no attachments. Read back with an
        empty parts key they must land on the SAME document as the text path produced, or a message
        already in somebody's archive appears in the thread twice."""
        self.assertEqual(self.out["plain-matches-sms"], "true")
        self.assertEqual(self.out["empty-parts-key"], "[]")

    def test_two_photos_in_one_second_are_two_messages(self):
        """THE REASON THIS RULE EXISTS. A picture message usually has no text at all, so
        who/when/direction/body is the identical string for both — filed at one address, the second
        replaces the first (an addressable event has exactly one newest version) and one of the two
        photos is gone from every device that is not the handset, with the thread still looking
        complete and nothing in any log."""
        self.assertEqual(self.out["two-photos-two-docs"], "true")

    def test_the_same_photo_read_twice_is_the_same_message(self):
        """The other half: an identity, not a nonce. Derived from what the PDU carries — type, the
        name the sender's phone chose, the length — so two devices holding the same message agree,
        and a re-read after a restored backup does not republish the lot."""
        self.assertEqual(self.out["same-photo-same-doc"], "true")

    def test_the_order_of_the_attachments_is_part_of_the_message(self):
        self.assertEqual(self.out["order-counts"], "true")

    def test_a_separator_inside_a_filename_cannot_shift_the_fields(self):
        """`a;b:c` written straight into the key makes one attachment look like three, and two
        different messages then agree by accident — which is the collision this whole rule exists to
        prevent, reintroduced through the back door."""
        self.assertEqual(self.out["separators-escaped"], "image/jpeg:a_b_c:5")
        self.assertEqual(self.out["no-accidental-collision"], "true")

    def test_the_two_reads_interleave_by_date(self):
        """A conversation is texts AND pictures and has always been read as one thing. Two lists is
        not a smaller version of that; it is a thread with holes in it."""
        self.assertEqual(self.out["interleaved"], "10,20,11,21,12,")

    def test_the_cap_is_applied_after_the_merge(self):
        """Truncating either half FIRST drops recent texts to make room for old pictures — a gap in
        the middle of a thread, with nothing anywhere to say a message was left out."""
        self.assertEqual(self.out["newest-three"], "10,20,11,")

    def test_a_tie_breaks_the_same_way_every_time(self):
        """A text and a picture stored in the same millisecond would otherwise swap places between
        two reads of the same store, and a thread whose order changes when you reopen it reads as
        messages appearing and disappearing."""
        self.assertEqual(self.out["stable-tie"], "true")

    @unittest.skipIf(not NODE, "no node on this node")
    def test_the_javascript_files_a_picture_message_at_the_same_address(self):
        """THE VALUE THAT EXISTS IN BOTH LANGUAGES. The client composes documents itself (a message
        sent without the role has no provider row to read back), so a parts key spelled differently
        in JavaScript files the same picture message at two addresses and it appears twice in the
        thread the moment the phone publishes its own copy."""
        js = _node()
        self.assertEqual(js["plain"], js["plain_empty"],
                         "an empty parts key changed the address")
        self.assertEqual(js["photo"], self.out["photo-doc"],
                         "sms.js and SmsKeys.docId disagree about a picture message's address")
        self.assertEqual(js["twoParts"], self.out["twoparts-doc"],
                         "the two halves disagree once there is more than one attachment")
        self.assertEqual(js["partsKey"], self.out["parts-key-shape"],
                         "sms.js and SmsKeys.partsKey disagree about an attachment list")
        self.assertEqual(js["escaped"], self.out["separators-escaped"],
                         "sms.js and SmsKeys.partKey escape differently")
        self.assertNotEqual(js["photo"], js["plain"])
        self.assertNotEqual(js["twoParts"], js["photo"])


@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class MmsProvider(unittest.TestCase):
    """The half only the platform can answer, asserted by name. Each of these fails silently."""

    @staticmethod
    def _code(name):
        src = open(os.path.join(SMS, name), encoding="utf-8").read()
        src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        # `(?<!:)` — a bare `//[^\n]*` eats the rest of the line from inside `content://mms/part`,
        # which made this file assert the absence of a string it had just deleted itself.
        return re.sub(r"(?<!:)//[^\n]*", " ", src)

    def test_the_mms_table_is_read_in_seconds(self):
        """`Telephony.Mms.DATE` IS IN SECONDS and `Telephony.Sms.DATE` is in milliseconds. Read as
        milliseconds every picture message is dated 1970 and sorts to the bottom of every thread;
        used in a WHERE clause the other way round, `since` matches nothing until the year 55000 and
        the archive silently never publishes a picture at all."""
        src = self._code("MmsStore.java")
        self.assertIn("raw * 1000L", src, "the mms date is not converted to milliseconds")
        # And multiplied only when it IS seconds. A provider that already stores milliseconds would
        # otherwise put every picture message tens of thousands of years in the future, where it
        # sorts ahead of everything and pushes the texts out of a conversation's newest N -- which
        # reads as "my replies are missing" with the replies untouched in the store.
        self.assertIn("raw > 100000000000L ? raw : raw * 1000L", src,
                      "a milliseconds-storing provider is multiplied into the year 57000")
        self.assertIn("dateMs / 1000L", src, "`since` compares milliseconds against a second column")

    def test_every_message_box_is_mapped_explicitly(self):
        """AOSP happens to number the two tables' boxes identically. Relying on that means an OEM
        that renumbers one of them draws every received picture as a sent one, in the thread,
        silently."""
        src = self._code("MmsStore.java")
        for box in ("MESSAGE_BOX_INBOX", "MESSAGE_BOX_SENT", "MESSAGE_BOX_DRAFTS",
                    "MESSAGE_BOX_OUTBOX", "MESSAGE_BOX_FAILED"):
            self.assertIn(box, src, "the %s case is missing" % box)

    def test_the_part_and_addr_uris_are_not_the_api_29_constants(self):
        """`Telephony.Mms.Part.CONTENT_URI`, `Part.getPartUriForMessage` and
        `Addr.getAddrUriForMessage` all arrived in API 29. minSdk here is 23, where reading one is a
        NoSuchFieldError at runtime that javac cannot see."""
        src = self._code("MmsStore.java")
        self.assertIn('Uri.parse("content://mms/part")', src)
        for gone in ("Part.CONTENT_URI", "getPartUriForMessage", "getAddrUriForMessage"):
            self.assertNotIn(gone, src, "an API 29 member is used on a minSdk 23 build")

    def test_the_phones_own_number_is_not_a_person(self):
        """AOSP files the handset's own address under a literal placeholder. Kept, every
        conversation is with yourself."""
        self.assertIn("insert-address-token", self._code("MmsStore.java"))

    def test_a_refusal_is_not_an_empty_list(self):
        """SmsStore's rule, and it matters more here: several OEM builds guard the MMS tables
        differently from the SMS ones, so this can be refused on a phone whose texts read
        perfectly — which renders as a thread that silently lost its photos."""
        src = self._code("MmsStore.java")
        self.assertIn("refused = true", src)
        self.assertIn("public static boolean refused()", src)

    def test_the_two_refusals_are_reported_separately(self):
        """Folded into one flag, an MMS-only refusal either blames the working half for the whole
        screen or disappears entirely."""
        src = self._code("SmsPlugin.java")
        self.assertIn("MmsStore.refused()", src)
        self.assertIn('o.put("mmsRefused"', src)
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("mmsRefused", js, "the client never reads it")

    def test_reading_pictures_never_writes_the_provider(self):
        """This app does not FETCH an MMS from the carrier (see MmsDeliverReceiver), and a
        placeholder row would put a message that does not exist into every app and every backup on
        the phone. Reading is reading."""
        src = self._code("MmsStore.java")
        for banned in ("ContentValues", "insert(", "downloadMultimediaMessage"):
            self.assertNotIn(banned, src, "the MMS reader writes to the provider")

    def test_a_picture_message_is_deleted_through_its_own_uri(self):
        """Handed to SmsStore it deletes nothing AND reports nothing — which the client reads as a
        provider refusal, so the archive is correctly left alone and the delete quietly did not
        happen, every time, with the message still on screen."""
        self.assertIn("Telephony.Mms.CONTENT_URI", self._code("MmsStore.java"))
        self.assertIn("MmsStore.delete", self._code("SmsPlugin.java"))
        self.assertIn("MmsStore.delete", self._code("ThreadActivity.java"))
        self.assertIn("mmsIds", open(SMSJS, encoding="utf-8").read(),
                      "the client sends every id down the SMS path")

    def test_every_screen_reads_both_providers(self):
        """A conversation that interleaves on one screen and not on another is the same bug reported
        three times. All three go through Messages."""
        self.assertIn("Messages.recent", self._code("SmsPlugin.java"))
        self.assertIn("Messages.thread", self._code("ThreadActivity.java"))
        self.assertIn("Messages.threads", self._code("ThreadListActivity.java"))

    def test_a_picture_message_never_draws_an_empty_bubble(self):
        """An empty bubble is what a message that FAILED looks like. The native screen labels its
        attachments; the client draws them and names the ones it cannot."""
        self.assertIn("sms_attachment", self._code("ThreadActivity.java"))
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("function snippetOf", js)
        self.assertIn("function attLabel", js)


class MmsAttachmentsTravelAcrossClients(unittest.TestCase):
    """A provider row id is phone-local; an encrypted Blossom hash is portable."""

    def test_archive_uses_the_encrypted_mms_folder(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("uploadEncFile", js)
        self.assertIn("'MMS'", js)
        self.assertIn("body.att.push", js)

    def test_remote_clients_decrypt_the_attachment_hash(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("PC.encFileUrl(sha", js)

    def test_failed_upload_does_not_advance_past_a_hollow_message(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("throw new Error((d && d.why)", js)


if __name__ == "__main__":
    unittest.main()
