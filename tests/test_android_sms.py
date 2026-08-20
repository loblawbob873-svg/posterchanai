"""THE MESSAGES APP — its rules RUN, and the two halves checked against each other.

An SMS app is the one feature in this repo where a mistake is unrecoverable: a text that is not
written to the system provider does not exist anywhere on the phone, there is no retry, and nothing
in any log says it happened. So the parts that decide things are pure Java (SmsKeys, SendTo) and this
compiles and runs them; the parts that talk to the platform are compiled against the real android.jar
and exercised on the emulator by mobile/android/app/src/androidTest.

Two guards here are about DRIFT rather than logic, and both were checked to fail without their fix:
the four components Android demands before it will offer this app the SMS role at all, and the id a
send-from-another-device is filed under, which is computed in Java on the phone and in JavaScript on
the laptop.
"""
import glob
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
MANIFEST = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "AndroidManifest.xml")
SMSJS = os.path.join(ROOT, "static", "js", "client", "sms.js")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")
NODE = shutil.which("node")

PURE = ["SmsKeys.java", "SmsMsg.java"]

HARNESS = r"""
import java.util.*;
import place.poster.app.sms.SmsKeys;

public class SmsHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }
  public static void main(String[] a) {
    say("norm", SmsKeys.normalize("+1 (555) 010-4477") + " " + SmsKeys.normalize("22000")
              + " " + SmsKeys.normalize("tel:+44 20 7946 0958"));

    // The same person written three ways by three apps on one phone.
    say("same", SmsKeys.sameNumber("+15550104477", "(555) 010-4477")
              + " " + SmsKeys.sameNumber("5550104477", "+1 555 010 4477"));
    // Two different people who happen to share a suffix shorter than seven digits must NOT merge.
    say("shortcodes", SmsKeys.sameNumber("22000", "22001") + " " + SmsKeys.sameNumber("22000", "22000"));
    say("different", SmsKeys.sameNumber("+15550104477", "+15550109999"));

    say("thread", SmsKeys.threadKey(Arrays.asList("+1555", "+1444"))
               + " " + SmsKeys.threadKey(Arrays.asList("+1444", "+1555")));

    // Identity: the same message is the same document, milliseconds within a second are the same
    // message, and the direction is part of who it is.
    String a1 = SmsKeys.docId("+15550104477", 1700000000123L, "hello", true);
    String a2 = SmsKeys.docId("(555) 010-4477", 1700000000999L, "hello", true);
    String a3 = SmsKeys.docId("+15550104477", 1700000000123L, "hello", false);
    String a4 = SmsKeys.docId("+15550104477", 1700000001123L, "hello", true);
    say("doc-same", a1.equals(a2));
    say("doc-direction", a1.equals(a3));
    say("doc-second", a1.equals(a4));
    say("doc-shape", a1);

    say("join", SmsKeys.joinParts(Arrays.asList("part one ", "part two")));
    say("join-null", "[" + SmsKeys.joinParts(null) + "]");

    StringBuilder long160 = new StringBuilder();
    for (int i = 0; i < 160; i++) long160.append('a');
    StringBuilder long161 = new StringBuilder(long160).append('a');
    say("seg", SmsKeys.segments("hi") + " " + SmsKeys.segments(long160.toString())
             + " " + SmsKeys.segments(long161.toString())
             + " " + SmsKeys.segments("你好"));

    say("outbox", SmsKeys.outboxId("+1 555 010 4477", "on my way", 1700000000000L));
    say("is-sms-doc", SmsKeys.isSmsDoc("pcai:sms:abc") + " " + SmsKeys.isSmsDoc("pcai:note:abc"));
  }
}
"""

NODE_HARNESS = r"""
const { webcrypto } = require('crypto');
global.crypto = webcrypto;
// `__PC` must be present BEFORE the module is required: its init() retries every 50ms until the
// app's shared surface exists, so without this node never exits and the test hangs rather than
// failing. Only the members the two functions under test touch are provided.
global.window = { __PC: { capPlugin: () => null } };
global.document = { addEventListener(){}, querySelector(){ return null; } };
global.localStorage = { getItem(){ return null; }, setItem(){} };
require(process.argv[2]);
(async () => {
  const S = global.window.PCSms;
  const out = {
    outbox: await S._outboxId('+1 555 010 4477', 'on my way', 1700000000000),
    // The archive's address for one message. The JS half started composing sent messages when the
    // app is not the default SMS app, so it now has to agree with SmsKeys.docId or the same text
    // is filed at two addresses and appears twice in the thread the moment the role is granted.
    doc: await S._docId('+15550104477', 1700000000123, 'hello', true),
    doc_ms: await S._docId('(555) 010-4477', 1700000000999, 'hello', true),
    doc_out: await S._docId('+15550104477', 1700000000123, 'hello', false),
    key_full: S._key('+1 (555) 010-4477'),
    key_other: S._key('5550104477'),
    key_short: S._key('22000'),
  };
  console.log(JSON.stringify(out));
})();
"""


def _run_node():
    """The shipped sms.js under node, returning what its rule functions answered."""
    with tempfile.TemporaryDirectory() as tmp:
        h = os.path.join(tmp, "h.js")
        with open(h, "w") as f:
            f.write(NODE_HARNESS)
        r = subprocess.run([NODE, h, SMSJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class SmsRules(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        h = os.path.join(cls.tmp, "SmsHarness.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        src = [os.path.join(SMS, f) for f in PURE]
        r = subprocess.run([JAVAC, "-nowarn", "-d", cls.tmp, "-sourcepath", ac.JAVA] + src + [h],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        r = subprocess.run([JAVARUN, "-cp", cls.tmp, "SmsHarness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.out = dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_number_is_reduced_to_what_can_be_compared(self):
        self.assertEqual(self.out["norm"], "+15550104477 22000 +442079460958")

    def test_the_same_person_written_three_ways_is_one_conversation(self):
        """The rule the platform's own PhoneNumberUtils.compare settles on. Without it a thread
        splits in three on one phone and nobody can read it."""
        self.assertEqual(self.out["same"], "true true")

    def test_short_codes_must_match_exactly(self):
        """Every five-digit sender would otherwise be the same conversation — a bank's alerts in the
        same thread as a delivery firm's."""
        self.assertEqual(self.out["shortcodes"], "false true")

    def test_two_different_numbers_are_two_conversations(self):
        self.assertEqual(self.out["different"], "false")

    def test_a_group_thread_is_the_same_thread_in_any_order(self):
        a, b = self.out["thread"].split(" ")
        self.assertEqual(a, b)

    def test_a_message_has_one_identity_everywhere(self):
        """The archive's address is derived from the MESSAGE, never from the provider's row id — a
        row id is local to one handset, so a restored backup would mint fresh ids for everything and
        republish the whole history."""
        self.assertEqual(self.out["doc-same"], "true", "the same message got two addresses")
        self.assertEqual(self.out["doc-direction"], "false", "sent and received collapsed together")
        self.assertEqual(self.out["doc-second"], "false", "a second apart is not the same message")
        self.assertTrue(re.fullmatch(r"pcai:sms:[0-9a-f]{24}", self.out["doc-shape"]),
                        self.out["doc-shape"])

    @unittest.skipIf(not NODE, "no node on this node")
    def test_the_javascript_files_a_message_at_the_same_address(self):
        """The client composes sent messages itself when the app is not the default SMS app — there
        is no provider row to read one back from. If its id rule differs from SmsKeys.docId by so
        much as a separator, the same text is filed twice and appears twice in the thread the moment
        the role is granted and the provider copy is published.

        Milliseconds inside one second are the same message; the direction is part of identity."""
        js = _run_node()
        self.assertEqual(js["doc"], self.out["doc-shape"],
                         "sms.js and SmsKeys.docId disagree about a message's address")
        self.assertEqual(js["doc_ms"], js["doc"], "a rounded timestamp made a second document")
        self.assertNotEqual(js["doc_out"], js["doc"], "direction is not part of the identity")

    def test_a_multipart_message_is_stored_whole(self):
        """Getting this wrong stores a long text as its first 160 characters and discards the rest,
        with no error anywhere."""
        self.assertEqual(self.out["join"], "part one part two")
        self.assertEqual(self.out["join-null"], "[]")

    def test_the_segment_counter_never_undercounts(self):
        one, at160, at161, unicode_ = self.out["seg"].split(" ")
        self.assertEqual(one, "1")
        self.assertEqual(at160, "1")
        self.assertEqual(at161, "2")
        self.assertEqual(unicode_, "1")     # two UCS-2 characters still fit one part

    def test_an_sms_document_is_told_apart_from_a_note(self):
        self.assertEqual(self.out["is-sms-doc"], "true false")

    @unittest.skipIf(not NODE, "no node on this node")
    def test_the_phone_and_the_laptop_agree_on_a_send_request_id(self):
        """THE ONE VALUE THAT EXISTS IN BOTH LANGUAGES.

        A laptop asks the phone to send a text by publishing a document at `pcai:smsout:<id>`; the
        phone files a completion marker at the SAME address, and that is what stops the request being
        performed twice. Compute it differently in the two halves and the marker lands where nothing
        is watching — so the phone sends the message again on every drain, for ever, and there is no
        way to un-send a text."""
        with tempfile.TemporaryDirectory() as tmp:
            h = os.path.join(tmp, "h.js")
            with open(h, "w") as f:
                f.write(NODE_HARNESS)
            r = subprocess.run([NODE, h, SMSJS], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        js = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(js["outbox"], self.out["outbox"],
                         "sms.js and SmsKeys.outboxId disagree")

    @unittest.skipIf(not NODE, "no node on this node")
    def test_the_two_halves_group_a_conversation_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = os.path.join(tmp, "h.js")
            with open(h, "w") as f:
                f.write(NODE_HARNESS)
            r = subprocess.run([NODE, h, SMSJS], capture_output=True, text=True, timeout=120)
        js = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(js["key_full"], js["key_other"], "one person, two threads")
        self.assertEqual(js["key_short"], "22000", "a short code must not be truncated")


@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class SmsRole(unittest.TestCase):
    """Android refuses the SMS role unless ALL FOUR of these exist. A role that cannot be granted is
    a feature that silently does not exist — the app simply never appears in the picker, with nothing
    to say why."""

    def setUp(self):
        self.man = open(MANIFEST, encoding="utf-8").read()

    def test_the_sms_deliver_receiver(self):
        self.assertIn("android.provider.Telephony.SMS_DELIVER", self.man)
        i = self.man.index('android:name=".sms.SmsDeliverReceiver"')
        self.assertIn("android.permission.BROADCAST_SMS", self.man[i:i + 500])
        self.assertIn('android:exported="true"', self.man[i:i + 500])

    def test_the_wap_push_receiver_for_mms(self):
        self.assertIn("android.provider.Telephony.WAP_PUSH_DELIVER", self.man)
        i = self.man.index('android:name=".sms.MmsDeliverReceiver"')
        block = self.man[i:i + 600]
        self.assertIn("android.permission.BROADCAST_WAP_PUSH", block)
        self.assertIn("application/vnd.wap.mms-message", block)

    def test_the_sendto_activity_answers_all_four_schemes(self):
        i = self.man.index('android:name=".sms.SendToActivity"')
        block = self.man[i:i + 1200]
        self.assertIn("android.intent.action.SENDTO", block)
        for scheme in ("sms", "smsto", "mms", "mmsto"):
            self.assertIn('android:scheme="%s"' % scheme, block)

    def test_the_respond_via_message_service(self):
        i = self.man.index('android:name=".sms.RespondService"')
        block = self.man[i:i + 900]
        self.assertIn("android.intent.action.RESPOND_VIA_MESSAGE", block)
        self.assertIn("android.permission.SEND_RESPOND_VIA_MESSAGE", block)

    def test_the_exported_receivers_are_guarded_by_platform_permissions(self):
        """BROADCAST_SMS and BROADCAST_WAP_PUSH are signature permissions the platform alone holds.
        Without them on the receiver, any app on the phone could inject a text message into
        somebody's inbox — and it would land in the system store looking exactly like a real one."""
        for cls in (".sms.SmsDeliverReceiver", ".sms.MmsDeliverReceiver"):
            i = self.man.index('android:name="%s"' % cls)
            block = self.man[i:i + 500]
            self.assertRegex(block, r'android:permission="android\.permission\.BROADCAST_(SMS|WAP_PUSH)"')

    def test_a_granted_role_is_not_read_before_it_settles(self):
        """THE SECOND HALF OF "sms does nothing when checked", and the harder half.

        Granting a role is asynchronous on the system side: the dialog returns and for a moment
        `getDefaultSmsPackage` still names the OLD app. Read once, right there, and the answer is
        "no" for a role that WAS granted — the switch springs back while Android's own settings
        screen already says PosterChan. Reported exactly that way.

        And the retry must watch the role that was ASKED for. Settling on "any role is held" returns
        instantly for somebody who already has the home screen and is now granting SMS, which is the
        same bug wearing a different hat."""
        plugin = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                              "place", "poster", "app", "home", "HomePlugin.java")
        src = open(plugin, encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        code = re.sub(r"//[^\n]*", " ", code)
        # Sliced to roleResult ITSELF, not the whole file: `settle(call` also appears in its own
        # declaration and in its recursion, so a search over the file passes against a roleResult
        # that reads the state once and answers.
        i = code.index("private void roleResult(")
        body = code[i:code.index("\n    }", i)]
        self.assertIn("settle(", body, "the role result is read once, immediately")
        self.assertNotIn("status(call)", body, "it answers before the grant has settled")
        self.assertIn("postDelayed", code, "there is no re-read")
        # It watches the requested role, not "anything at all".
        self.assertIn('asking = "sms"', code)
        self.assertIn('asking = "dialer"', code)
        self.assertIn('asking = "home"', code)
        self.assertIn("private boolean asked()", code)
        # And it is BOUNDED — this codebase does not poll, and an unbounded retry on the one process
        # that holds the HOME role would run for the life of the battery.
        self.assertIn("SETTLE_TRIES", code)
        self.assertIn("tries >= SETTLE_TRIES", code)

    def test_the_switch_can_say_the_role_was_refused(self):
        """Android refuses a role the app cannot hold by starting the request activity and finishing
        it with RESULT_CANCELED — no dialog, no error, no log, which is indistinguishable from a
        switch that was never wired up."""
        js = open(os.path.join(ROOT, "static", "js", "client", "phoneshell.js"), encoding="utf-8").read()
        self.assertIn("smsCapable", js, "the switch cannot tell whether the role is even possible")
        self.assertIn("openDefaultApps", js, "there is no route when the role dialog does not take")
        self.assertIn("visibilitychange", js, "coming back from Android's own screen changes nothing")

    def test_messages_has_a_launcher_icon_of_its_own(self):
        """Routing is not an app: without a MAIN/LAUNCHER filter Messages appears in no drawer at
        all, ours or the stock one, and from the person's side it does not exist. It is an
        activity-alias so the four components ROLE_SMS requires are left exactly as they are."""
        i = self.man.index('android:name=".sms.Messages"')
        block = self.man[i:i + 900]
        self.assertIn("android.intent.category.LAUNCHER", block)
        self.assertIn("ic_launcher_messages", block, "it shows the PosterChan mark, not a bubble")
        self.assertIn('android:targetActivity=".sms.ThreadListActivity"', block)
        # And the SENDTO filter — one of the four — is still on the activity itself.
        j = self.man.index('android:name=".sms.SendToActivity"')
        self.assertIn("android.intent.action.SENDTO", self.man[j:j + 1200])

    def test_the_two_app_icons_are_not_the_same_picture(self):
        """Three drawer entries all showing the PosterChan mark is the letter-tile complaint again:
        the app is there and looks like it is not."""
        self.assertNotEqual(
            open(os.path.join(ROOT, "mobile/android/app/src/main/res/drawable/ic_app_messages_fg.xml"),
                 encoding="utf-8").read(),
            open(os.path.join(ROOT, "mobile/android/app/src/main/res/drawable/ic_app_phone_fg.xml"),
                 encoding="utf-8").read())

    def test_the_app_icons_resolve_on_every_android_this_supports(self):
        """minSdk is 23. An adaptive icon alone is an unresolvable resource on 23-25 — the legacy
        raster in each density folder is what makes the icon exist there at all."""
        for name in ("messages", "phone"):
            for dens in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
                p = os.path.join(ROOT, "mobile/android/app/src/main/res",
                                 "mipmap-" + dens, "ic_launcher_%s.png" % name)
                self.assertTrue(os.path.exists(p), "missing " + p)
            p = os.path.join(ROOT, "mobile/android/app/src/main/res/mipmap-anydpi-v26",
                             "ic_launcher_%s.xml" % name)
            self.assertTrue(os.path.exists(p), "missing the adaptive icon for " + name)

    def test_telephony_is_not_a_required_feature(self):
        """Declaring it required removes this app from every tablet and Wi-Fi-only device."""
        i = self.man.index('android.hardware.telephony')
        self.assertIn('android:required="false"', self.man[i:i + 200])


@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class SmsSources(unittest.TestCase):

    @staticmethod
    def _code(path):
        src = open(path, encoding="utf-8").read()
        src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        return re.sub(r"//[^\n]*", " ", src)

    def test_an_incoming_message_is_stored_before_anything_else_happens(self):
        """ORDER IS THE GUARD. Storing matters more than notifying and notifying matters more than
        the WebView ever hearing about it — so if a notification throws, the message is already in
        the phone's own store, where every other app and every backup will find it."""
        src = self._code(os.path.join(SMS, "SmsDeliverReceiver.java"))
        store = src.index("storeInbox")
        notify = src.index("SmsNotifier.incoming")
        plugin = src.index("SmsPlugin.onIncoming")
        self.assertLess(store, notify, "the notification is posted before the message is stored")
        self.assertLess(notify, plugin, "the app is told before the person is")

    def test_each_step_of_delivery_is_guarded_separately(self):
        """One try around all three would mean a failing notification costs the message."""
        src = self._code(os.path.join(SMS, "SmsDeliverReceiver.java"))
        body = src[src.index("private void deliver"):]
        self.assertGreaterEqual(body.count("try {"), 3, "delivery is not guarded step by step")

    def test_the_notification_reply_box_is_mutable_and_nothing_else_is(self):
        """THE TRAP THAT SENDS AN EMPTY REPLY, SILENTLY.

        From Android 12 every PendingIntent must declare IMMUTABLE or MUTABLE. Every one in this app
        is correctly immutable — except the reply action's, which CANNOT be: RemoteInput delivers
        what the person typed by WRITING IT INTO that intent. Copy the immutable spelling here and the
        reply arrives empty, the message is never sent, and the notification is taken down as though
        it had been."""
        src = self._code(os.path.join(SMS, "SmsNotifier.java"))
        reply = src[src.index("Action replyAction("):]
        reply = reply[:reply.index("\n    }")]
        self.assertIn("FLAG_MUTABLE", reply)
        self.assertNotIn("FLAG_IMMUTABLE", reply)
        # And the rest of the file must not have caught it.
        rest = src.replace(reply, "")
        self.assertNotIn("FLAG_MUTABLE", rest, "another PendingIntent was made mutable")

    def test_mms_is_declared_unsupported_rather_than_faked(self):
        """A placeholder row would put a message that does not exist into every app and every backup
        on the phone. The receiver has to exist for the role; what it must not do is pretend."""
        src = self._code(os.path.join(SMS, "MmsDeliverReceiver.java"))
        for banned in ("storeInbox", "ContentValues", "downloadMultimediaMessage", "insert("):
            self.assertNotIn(banned, src, "the MMS receiver writes to the provider")
        self.assertIn("mmsUnsupported", src, "an unfetched MMS is silent")

    def test_the_send_path_refuses_when_this_app_is_not_the_default(self):
        """A non-default app can still call SmsManager and may NOT write the provider, so the message
        would be sent and then be missing from the thread it was sent in."""
        src = self._code(os.path.join(SMS, "SmsSender.java"))
        self.assertIn("HasRole.sms(ctx)", src)
        self.assertLess(src.index("HasRole.sms(ctx)"), src.index("sendMultipartTextMessage"))

    def test_the_outgoing_row_is_written_before_the_radio_is_asked(self):
        """If the process dies mid-send the message is visible as pending rather than absent. The
        alternative loses what somebody typed."""
        src = self._code(os.path.join(SMS, "SmsSender.java"))
        self.assertLess(src.index("storeSent"), src.index("sendMultipartTextMessage"))

    def test_only_the_last_part_of_a_multipart_send_moves_the_row(self):
        """A three-part message answers three times; treating each answer as 'sent' moves the row
        while two parts are still in the air."""
        src = self._code(os.path.join(SMS, "SmsSender.java"))
        self.assertIn("last ? r.row : null", src)

    def test_the_client_pins_the_archive_against_cache_eviction(self):
        """THE THIRD AUTO-CLEANER, which Notes learned about the hard way.

        The client cache evicts newest-N by created_at — right for a firehose, fatal for a document
        only its author can decrypt. On every device that is NOT the phone this archive is the only
        copy, so eviction is not a cache miss, it is the messages being gone."""
        store = open(os.path.join(ROOT, "static", "js", "client", "store.js"), encoding="utf-8").read()
        pinned = store[store.index("function _isPinned"):]
        pinned = pinned[:pinned.index("\n  }")]
        self.assertIn("pcai:sms", pinned)
        app = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
        carry = app[app.index("const _CARRY_D"):]
        carry = carry[:carry.index("];")]
        self.assertIn("pcai:sms", carry)


if __name__ == "__main__":
    unittest.main()
