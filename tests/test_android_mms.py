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
SOURCES = ["SmsKeys.java", "SmsMsg.java", "SmsPart.java", "Messages.java", "MmsWhen.java"]

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

    // THE DATE-UNIT RULE, RUN. The reader and the query predicate are one function and its mirror
    // image; the whole point of MmsWhen is that they cannot drift, so both come out of here.
    say("millis-from-seconds", MmsWhen.millis(1700000000L));
    say("millis-already-ms", MmsWhen.millis(1700000000123L));
    say("millis-zero", MmsWhen.millis(0L));
    say("after-sql", MmsWhen.after("date"));
    String[] aa = MmsWhen.afterArgs(1700000000123L);
    say("after-args", aa[0] + "," + aa[1]);
    String[] az = MmsWhen.afterArgs(-5L);
    say("after-args-negative", az[0] + "," + az[1]);
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
    # This harness RUNS only Messages.merge. Shim the two provider stores so javac does not
    # recursively pull PhoneBook and its AndroidX graphics dependency into this pure-Java test.
    stores = {
        "place/poster/app/sms/SmsStore.java": """package place.poster.app.sms;
import android.content.Context; import java.util.*;
final class SmsStore { static class Thread { String address,label; }
 static List<SmsMsg> recent(Context c,int n){return new ArrayList<>();}
 static List<SmsMsg> since(Context c,long d,int n){return new ArrayList<>();}
 static List<SmsMsg> thread(Context c,long[] ids,int n){return new ArrayList<>();}
 static List<Thread> platformThreads(Context c,int n,boolean w){return new ArrayList<>();}
 static List<Thread> fold(Context c,List<SmsMsg> m,boolean w){return new ArrayList<>();} }""",
        "place/poster/app/sms/MmsStore.java": """package place.poster.app.sms;
import android.content.Context; import java.util.*;
final class MmsStore {
 static List<SmsMsg> recent(Context c,int n){return new ArrayList<>();}
 static List<SmsMsg> since(Context c,long d,int n){return new ArrayList<>();}
 static List<SmsMsg> thread(Context c,long[] ids,int n){return new ArrayList<>();} }""",
    }
    r = ac.compile_sources(src, tmp, shims=stores)
    assert r.returncode == 0, r.stderr[-4000:]
    r = subprocess.run([JAVARUN, "-cp", os.path.join(tmp, "classes") + os.pathsep + JAR,
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
    def test_outgoing_mms_has_a_real_platform_completion_path(self):
        sender = open(os.path.join(SMS, "MmsSender.java"), encoding="utf-8").read()
        receiver = open(os.path.join(SMS, "MmsSendReceiver.java"), encoding="utf-8").read()
        manifest = open(os.path.join(ROOT, "mobile/android/app/src/main/AndroidManifest.xml"),
                        encoding="utf-8").read()
        self.assertIn("setExplicitBroadcastForSentMms(sent)", sender)
        self.assertIn('.setAction(MmsSendReceiver.ACTION_SENT)', sender)
        self.assertIn('Telephony.Mms.MESSAGE_BOX_SENT', receiver)
        self.assertIn('Telephony.Mms.MESSAGE_BOX_FAILED', receiver)
        self.assertIn('getResultCode()', receiver)
        self.assertIn('android:name=".sms.MmsSendReceiver"', manifest)
        failures = open(os.path.join(SMS, "MmsFailures.java"), encoding="utf-8").read()
        self.assertIn('android.telephony.extra.MMS_HTTP_STATUS', receiver)
        self.assertIn('MmsFailures.put(ctx, id, result, http)', receiver)
        for reason in ('invalid carrier APN', 'mobile data is unavailable',
                       'the selected SIM is inactive', 'carrier server rejected it'):
            self.assertIn(reason, failures)
        self.assertIn('Android cancelled MMS before the carrier returned a reason', failures)
        self.assertIn('verify mobile data and the carrier MMS APN', failures)
        self.assertIn('SubscriptionManager.getDefaultSmsSubscriptionId()', failures)

    def test_failed_native_mms_can_be_retried_without_deleting_before_acceptance(self):
        thread = open(os.path.join(SMS, "ThreadActivity.java"), encoding="utf-8").read()
        bubble = open(os.path.join(ROOT, "mobile/android/app/src/main/res/layout/sms_bubble.xml"),
                      encoding="utf-8").read()
        self.assertIn("m.mms && (m.failed() || m.pending()) && !m.parts.isEmpty()", thread)
        self.assertIn('android:id="@+id/pc_b_retry"', bubble)
        self.assertIn("retry.setVisibility(retryable ? View.VISIBLE : View.GONE)", thread)
        self.assertIn("retry.setOnClickListener(retryable ? view -> retryMms(m)", thread)
        retry = thread[thread.index("private void retryMms"):thread.index("private void deleteMessage")]
        self.assertIn("MmsStore.partBytes", retry)
        self.assertIn("MmsSender.send", retry)
        self.assertLess(retry.index("MmsSender.send"), retry.index("MmsStore.delete"))

    def test_phone_delete_tombstones_the_cross_device_archive(self):
        thread = open(os.path.join(SMS, "ThreadActivity.java"), encoding="utf-8").read()
        outbox = open(os.path.join(SMS, "SmsOutbox.java"), encoding="utf-8").read()
        relay = open(os.path.join(ac.JAVA, "place/poster/app/signer/SignerRelayService.java"),
                     encoding="utf-8").read()
        delete = thread[thread.index("private void deleteMessage"):
                        thread.index("private String bubbleText")]
        self.assertIn("if (n > 0) SignerRelayService.archiveDelete(this, m.docId())", delete)
        self.assertIn("public static List<JSONObject> archiveDelete", outbox)
        self.assertIn('a.add("a"); a.add(KIND + ":" + pubHex + ":" + doc)', outbox)
        self.assertIn("smsArchiveDeletes.add(doc)", relay)
        self.assertIn("events.addAll(SmsOutbox.archiveDelete", relay)

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

    def test_the_mms_table_is_read_and_selected_by_the_same_rule(self):
        """`Telephony.Mms.DATE` IS IN SECONDS by specification, `Telephony.Sms.DATE` is in
        milliseconds, and several OEM providers keep milliseconds in the MMS table anyway. Read the
        wrong way every picture message is dated 1970 and sorts to the bottom of every thread.

        Asserted as DELEGATION rather than as two literals, because two literals is exactly what
        went wrong: the reader learned about milliseconds providers and the WHERE clause did not,
        and there is no way to see that by looking at either one on its own."""
        src = self._code("MmsStore.java")
        self.assertIn("MmsWhen.millis(raw)", src, "the reader does not use the shared rule")
        self.assertIn("MmsWhen.after(", src, "`since` does not use the shared rule")
        self.assertIn("MmsWhen.afterArgs(", src, "`since` does not bind the shared arguments")
        # The old hand-rolled forms must be GONE, not merely joined by the new one -- a leftover
        # copy is how they drifted in the first place.
        self.assertNotIn("raw > 100000000000L ? raw", src, "a second copy of the unit rule survives")
        self.assertNotIn("dateMs / 1000L", src,
                         "`since` still divides unconditionally; on a milliseconds provider that "
                         "matches every row and pins the archive to the oldest corner of the store")

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
        `Addr.getAddrUriForMessage` all arrived in API 29. minSdk here is 26, where reading one is a
        NoSuchFieldError at runtime that javac cannot see."""
        src = self._code("MmsStore.java")
        self.assertIn('Uri.parse("content://mms/part")', src)
        for gone in ("Part.CONTENT_URI", "getPartUriForMessage", "getAddrUriForMessage"):
            self.assertNotIn(gone, src, "an API 29 member is used on a minSdk 26 build")

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
        """Carrier download belongs to the transport receiver; this provider reader must never
        manufacture a placeholder row that every messaging app and backup would then preserve."""
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
        self.assertIn("PC.encFileUrl(p.sha", js)

    def test_threads_use_an_encrypted_thumbnail_until_the_picture_is_opened(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("createImageBitmap(d.blob)", js)
        # The FIELD, not the exact call: what matters is that the published attachment carries the
        # preview hash beside the original. Pinning the whole expression made an unrelated addition
        # to the same object look like the thumbnail had been dropped.
        self.assertIn("thumb: p.thumb || ''", js)
        self.assertIn("const previewSha = isImage(p.ct) && p.thumb ? p.thumb : sha", js)
        self.assertIn("if(d.preview && p.sha && PC.encFileUrl)", js,
                      "the full picture is not deferred until the thumbnail is tapped")

    def test_failed_upload_does_not_advance_past_a_hollow_message(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("throw new Error((d && d.why)", js)

    def test_archive_provider_pages_from_the_oldest_pending_message(self):
        """A limited newest slice strands the older backlog forever once its cursor advances."""
        sms = open(os.path.join(SMS, "SmsStore.java"), encoding="utf-8").read()
        mms = open(os.path.join(SMS, "MmsStore.java"), encoding="utf-8").read()
        messages = open(os.path.join(SMS, "Messages.java"), encoding="utf-8").read()
        self.assertIn('new String[]{ String.valueOf(dateMs) }, "date ASC", limit)', sms)
        # The MMS half binds through MmsWhen now (the reader and this clause must agree about the
        # column's unit -- see TheArchiveCanReachEveryPictureMessage). The ORDER is the rule here.
        self.assertIn('MmsWhen.afterArgs(dateMs),\n                     "date ASC", limit)', mms)
        self.assertIn("Collections.sort(out, OLDEST_FIRST)", messages)

    def test_fixed_cursor_recovers_rows_old_clients_skipped(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("HWM_FIX", js)
        self.assertIn("since - 1000", js)

    def test_existing_body_only_archive_is_upgraded_not_skipped(self):
        """The document can already exist from an older client while its MMS hashes do not.  That
        is an incomplete message, not a duplicate: the handset must merge its provider ids and the
        mirror/import paths must publish the repaired version."""
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("function needsPartUpgrade", js)
        self.assertGreaterEqual(js.count("needsArchiveUpgrade(r, old)"), 3,
                                "one of read, mirror or history import still skips hollow MMS")
        self.assertIn("Object.assign({}, old || {}, local)", js,
                      "the phone's provider attachment ids are not merged into the archived body")


class OutgoingMms(unittest.TestCase):
    """A paperclip must terminate at the carrier, including when another client starts the send."""

    def test_native_messages_has_a_photo_picker_and_carrier_transport(self):
        thread = open(os.path.join(SMS, "ThreadActivity.java"), encoding="utf-8").read()
        layout = open(os.path.join(ROOT, "mobile/android/app/src/main/res/layout/sms_thread.xml"),
                      encoding="utf-8").read()
        sender = open(os.path.join(SMS, "MmsSender.java"), encoding="utf-8").read()
        gradle = open(os.path.join(ROOT, "mobile/android/app/build.gradle"), encoding="utf-8").read()
        self.assertIn("pc_th_attach", layout)
        self.assertIn("ACTION_OPEN_DOCUMENT", thread)
        self.assertIn("MmsSender.send(this, address, body, raw)", thread)
        self.assertIn("setUseSystemSending(true)", sender)
        self.assertIn("org.fossify:mmslib:1.0.0", gradle)

    def test_native_stuck_mms_has_a_visible_delete_action(self):
        thread = open(os.path.join(SMS, "ThreadActivity.java"), encoding="utf-8").read()
        layout = open(os.path.join(ROOT, "mobile/android/app/src/main/res/layout/sms_bubble.xml"), encoding="utf-8").read()
        self.assertIn('android:id="@+id/pc_b_delete"', layout)
        self.assertIn("mine && (m.pending() || m.failed())", thread)
        self.assertIn("MmsStore.delete(this", thread)
        self.assertIn("sms_delete_confirm", thread)

    def test_native_thread_observes_mms_and_uses_shared_subscription_aware_sender(self):
        thread = open(os.path.join(SMS, "ThreadActivity.java"), encoding="utf-8").read()
        self.assertIn("registerContentObserver(Telephony.Mms.CONTENT_URI", thread)
        self.assertIn("MmsSender.send(this, address, body, raw)", thread)
        send = thread[thread.index("private void sendMms(String body)") : thread.index("private void messageMenu")]
        self.assertNotIn("new Transaction", send)

    def test_web_composer_seals_remote_attachment_and_phone_sends_it(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn('id="sms-file"', js)
        self.assertIn("PC.uploadEncFile(file, 'MMS')", js)
        self.assertIn("PC.encFileUrl(req.attachment.sha", js)
        self.assertIn("P.sendMms", js)

    def test_non_default_phone_keeps_its_successful_mms_attachment(self):
        """Without the SMS role there is no provider row for mirror() to recover later."""
        js = open(SMSJS, encoding="utf-8").read()
        send = js[js.index("async function send(to, body, file)"):]
        send = send[:send.index("\n  }")]
        unstored = send[send.index("r.stored === false"):]
        self.assertIn("PC.uploadEncFile(file, 'MMS')", unstored)
        self.assertIn("m.parts =", unstored)
        self.assertIn("await publishOne(m)", unstored)

    def test_plugin_decodes_only_at_the_phone_transport_boundary(self):
        plugin = open(os.path.join(SMS, "SmsPlugin.java"), encoding="utf-8").read()
        sender = open(os.path.join(SMS, "MmsSender.java"), encoding="utf-8").read()
        self.assertIn("public void sendMms", plugin)
        self.assertIn("Base64.decode", plugin)
        self.assertIn("MmsSender.send", plugin)
        self.assertIn("SubscriptionManager.getDefaultSmsSubscriptionId()", sender)
        self.assertIn("SubscriptionManager.getDefaultDataSubscriptionId()", sender)
        self.assertIn("settings.setSubscriptionId(sub)", sender)
        self.assertIn("sendNewMessage", sender)
        self.assertIn("40_000_000L", sender)
        self.assertIn("8 * 1024 * 1024", sender)

    def test_background_phone_fetches_decrypts_and_sends_a_webui_photo_once(self):
        js = open(SMSJS, encoding="utf-8").read()
        outbox = open(os.path.join(SMS, "SmsOutbox.java"), encoding="utf-8").read()
        plugin = open(os.path.join(SMS, "SmsPlugin.java"), encoding="utf-8").read()
        self.assertIn("name:req.attachment.name, outbox:d", js)
        self.assertIn("SmsOutbox.claim(getContext(), outbox)", plugin)
        self.assertIn("SyncCrypto.unwrapMasterKey", outbox)
        self.assertIn("SyncCrypto.decrypt(mk, net.getBlob(sha))", outbox)
        self.assertIn("MmsSender.send(ctx, to, body, imageBytes)", outbox)
        self.assertIn("if (!claim(ctx, doc)) return null", outbox)
        self.assertIn('req.optBoolean("cancelled", false)', outbox)
        self.assertIn("cancel(ctx, doc)", outbox)
        self.assertIn("if (isCancelled(ctx, doc))", outbox)
        self.assertLess(outbox.index("if (isCancelled(ctx, doc))"),
                        outbox.index("MmsSender.send(ctx, to, body, imageBytes)"))

    def test_background_send_receipt_survives_a_relay_reconnect(self):
        service = open(os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/signer/SignerRelayService.java"), encoding="utf-8").read()
        self.assertIn('SMS_RECEIPTS = "poster_sms_outbox_receipts"', service)
        self.assertIn("queueSmsReceipt(done)", service)
        self.assertIn("flushSmsReceipts(socks.get(url))", service)
        self.assertIn("flushSmsReceipts(s);", service)
        self.assertNotIn("final WebSocket ws = socks.get(url);", service)

    def test_phone_already_open_keeps_watching_for_new_webui_mms(self):
        """A request published after the load/visibility hooks must not wait for another resume."""
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("let _drainingOutbox = null", js)
        self.assertIn("if(_drainingOutbox) return _drainingOutbox", js)
        self.assertIn("if(document.visibilityState !== 'visible') return", js)
        self.assertIn("if(st.telephony) drainOutbox();\n    }, 3000);", js)
        self.assertIn("typeof outboxPoll.unref === 'function'", js)

    def test_remote_photo_placeholder_uses_the_mms_document_identity(self):
        """The later provider mirror must replace the pending bubble, not create a duplicate."""
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("partsKeyOf(pendingParts)", js)
        self.assertIn("partsKeyOf(sentParts)", js)
        self.assertIn("old.pending && old.outbox === d", js)

    def test_message_bodies_are_encrypted_blobs_not_relay_payloads(self):
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("archiveMessageBody(m.doc, body)", js)
        self.assertIn("PC.uploadEncFile(file, 'Messages')", js)
        self.assertIn("obj = await openMessageBody(envelope)", js)
        self.assertIn("!archived._blob", js,
                      "legacy inline records are never migrated to encrypted Blossom")
        # Outbox commands stay inline NIP-44: Android's background receiver has no WebView drive
        # API with which to fetch a Blossom pointer. Archive HISTORY still uses encrypted Blossom.
        self.assertIn("const request = { to, body, at, attachment }", js)
        self.assertNotIn("archiveMessageBody(doc, { to, body, at, attachment })", js)
        self.assertIn("req = await openMessageBody(request)", js,
                      "new clients cannot drain older Blossom-backed remote-send requests")


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(JAR is None, "no android.jar on this node")
@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class TheArchiveCanReachEveryPictureMessage(unittest.TestCase):
    """WHY THE OLD PICTURE MESSAGES NEVER REACHED ENCRYPTED STORAGE.

    Two ceilings sat in front of them, both silent, and neither is a failure any log can show: a
    WHERE clause that disagreed with the reader about what unit `mms.date` is in, and a row cap that
    could not say it had been hit. Reported as "all the existing SMS media files are not syncing to
    Blossom" from a phone whose Texts screen was full."""

    out = None

    @classmethod
    def setUpClass(cls):
        cls.out = _java()

    @staticmethod
    def _code(name):
        return MmsProvider._code(name)

    # ---- the unit rule, RUN ------------------------------------------------------------------

    def test_a_seconds_column_is_read_as_milliseconds(self):
        self.assertEqual(self.out["millis-from-seconds"], "1700000000000")

    def test_a_milliseconds_column_is_left_alone(self):
        """A provider that already stores milliseconds, multiplied blind, lands every picture
        message tens of thousands of years in the future — where it sorts ahead of everything and
        pushes the texts out of a conversation's newest N. That reads as "my replies are missing"
        with the replies sitting untouched in the store."""
        self.assertEqual(self.out["millis-already-ms"], "1700000000123")

    def test_an_empty_date_stays_empty(self):
        """0 is the one value both units agree on, and multiplying it must not invent a date."""
        self.assertEqual(self.out["millis-zero"], "0")

    def test_the_selection_never_binds_a_negative_argument(self):
        """A clock that went backwards, or an unset mark read as -1, must not build a clause that
        selects rows the caller has already archived."""
        self.assertEqual(self.out["after-args-negative"], "0,0")

    # ---- the predicate against REAL sqlite ----------------------------------------------------

    def _select(self, unit_rows, since_ms):
        """Run the generated WHERE against SQLite — the engine actually behind the provider.

        A string assertion cannot tell a correct clause from one that parses and matches
        everything, which is precisely the bug this exists for."""
        import sqlite3
        sql = self.out["after-sql"]
        args = self.out["after-args"].split(",")
        # The harness generated its args for one fixed timestamp; regenerate for this call the same
        # way MmsWhen does, so the test drives the SHAPE from Java and the values from here.
        ms = max(0, since_ms)
        args = [str(ms), str(ms // 1000)]
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE pdu (_id INTEGER, date INTEGER)")
        db.executemany("INSERT INTO pdu VALUES (?,?)", list(enumerate(unit_rows)))
        got = db.execute("SELECT _id FROM pdu WHERE " + sql + " ORDER BY date ASC", args).fetchall()
        db.close()
        return [r[0] for r in got]

    def test_the_clause_parses_and_selects_on_a_seconds_provider(self):
        """The spec-compliant table, which has always worked and must keep working."""
        rows = [1699999999, 1700000000, 1700000001, 1700000002]        # seconds
        self.assertEqual(self._select(rows, 1700000000000), [2, 3])

    def test_the_clause_selects_on_a_milliseconds_provider(self):
        """THE BUG. Against `date > dateMs/1000` a millisecond column matches EVERY row, so
        `date ASC LIMIT n` hands back the oldest n picture messages on every single sweep — they
        archive once, become cheap skips, and the high-water mark can only ever land on the newest
        of that same oldest slice. The archive pins itself to the oldest corner of the store and no
        picture message behind it is ever offered to encrypted storage."""
        rows = [1699999999000, 1700000000000, 1700000001000, 1700000002000]
        self.assertEqual(self._select(rows, 1700000000000), [2, 3])
        # And the specific shape of the old failure: it must NOT match everything.
        self.assertNotEqual(self._select(rows, 1700000000000), [0, 1, 2, 3])

    def test_a_table_holding_both_units_is_read_row_by_row(self):
        """A restore that merged two phones, or an OEM that changed its mind across an upgrade. The
        test is against each row's own magnitude, so a mixed table is still correct."""
        rows = [1699999999, 1700000002, 1699999999000, 1700000002000]
        self.assertEqual(sorted(self._select(rows, 1700000000000)), [1, 3])

    # ---- the ceiling says it was hit ----------------------------------------------------------

    def test_the_row_cap_is_reported_and_not_merely_enforced(self):
        """"your picture messages" and "2,000 of your picture messages" are different sentences.
        Without the flag the archive walks the newest 2,000, finds nothing left to do and reports
        that it has copied the phone — so on a bigger store the oldest media is not slow to reach
        Blossom, it is never offered to it."""
        src = self._code("MmsStore.java")
        self.assertIn("public static boolean capped()", src, "the ceiling cannot be reported")
        self.assertIn("capped = limit > MAX_ROWS && out.size() >= MAX_ROWS", src)
        # Cleared at the start of every read, like `refused` — a latched flag would wear the notice
        # for the rest of the process with the whole store on the screen underneath it.
        self.assertIn("capped = false;", src, "the flag latches across reads")

    def test_the_plugin_hands_the_ceiling_to_the_client(self):
        plugin = open(os.path.join(SMS, "SmsPlugin.java"), encoding="utf-8").read()
        self.assertIn("MmsStore.capped()", plugin)
        self.assertIn('o.put("mmsCapped", mmsCapped)', plugin,
                      "the client cannot tell a truncated read from an exhausted one")

    # ---- the name the archive already promised to carry ---------------------------------------

    def test_the_mms_ceiling_is_asked_of_the_carrier(self):
        """Every published figure for the MMS size limit is folklore — 300KB, 600KB, "about a
        megabyte" — and the real number is per-carrier. The platform knows it because the MMS stack
        has to, so it is read from the same carrier config the transport applies rather than compiled
        into an app that has never met this SIM.

        `measured` rides with it for the usual reason: 300KB because AOSP says so and 300KB because
        THIS carrier says so are the same integer and different facts, and only one of them is worth
        overriding somebody's choice with."""
        plugin = open(os.path.join(SMS, "SmsPlugin.java"), encoding="utf-8").read()
        self.assertIn("public void mmsLimit(", plugin)
        self.assertIn("getCarrierConfigValues()", plugin,
                      "the limit is guessed rather than asked")
        self.assertIn('cfg.getInt("maxMessageSize"', plugin)
        self.assertIn('o.put("measured", bytes > 0)', plugin,
                      "a fallback is indistinguishable from a real carrier answer")
        # The fallback must exist, so a tablet or a config that cannot be read still yields a number
        # rather than throwing on the send path.
        self.assertIn("DEFAULT_MMS_MAX", plugin)

    def test_every_published_row_carries_the_contact_name(self):
        """The archive's own comment says the handset resolves the name against the phone's OWN
        address book and carries it, and the client's `fromRow` duly reads `r.name`. Nothing put one
        there, so every message published from this phone reached every other device as a bare
        number — the promise kept in prose and in the reader, and broken in the one place that had
        the answer."""
        plugin = open(os.path.join(SMS, "SmsPlugin.java"), encoding="utf-8").read()
        i = plugin.index("private JSONArray toJson(")
        seg = plugin[i:i + 2500]
        self.assertIn('o.put("name", PhoneBook.nameOf(getContext(), m.address))', seg,
                      "published rows carry no contact name")
        # `nameOf`, never `label`: label falls back to the digits, and those digits would then
        # travel into the archive as though somebody were called that.
        self.assertNotIn('o.put("name", PhoneBook.label(', seg)
        js = open(SMSJS, encoding="utf-8").read()
        self.assertIn("name: r.name || ''", js, "the client stopped reading the name")


if __name__ == "__main__":
    unittest.main()
