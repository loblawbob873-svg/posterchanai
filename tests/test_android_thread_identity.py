"""ONE PERSON IS ONE CONVERSATION, even when the platform gave them two thread ids.

A thread id is not an identity. Android hands one out from its canonical-addresses table, and the
same person reached two ways -- "+15551234567" as the carrier delivers it, "5551234567" as it was
dialled -- can be given TWO. Grouping by thread id then splits one conversation in half, and the
halves usually split by DIRECTION, because the incoming spelling is the carrier's and the outgoing
one is whatever dialled it.

That is not hypothetical; it is what a phone here did. Two "Mom" rows in the list, and a thread
showing the other person's messages with none of your own -- on a store that held 168 sent texts the
whole time. Nothing was missing and nothing logged; the screen was reading one of the two ids.

So `fold` keys on the PERSON and keeps every thread id under `ids`, and reading a conversation reads
all of them. The exception is a group picture message, which carries a single participant's address
like any other message and must NOT be folded into that participant's private conversation.

This compiles and RUNS the real SmsStore.fold. Each check was verified to fail with its rule removed
(fold keyed on `m.threadId` again): the split person came back as 2 conversations.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")
JAR = ac.android_jar()

# SmsStore loads on a plain JVM: its only static state is a String[] of column names, and those are
# compile-time constants that javac inlines. `fold` is called with a null Context and withNames
# false, which is the one path through it that never reaches the phone.
SOURCES = ["SmsKeys.java", "SmsMsg.java", "SmsPart.java", "Messages.java", "SmsStore.java"]

HARNESS = r"""
package place.poster.app.sms;

import java.util.*;

public class ThreadIdHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }

  static SmsMsg m(long id, long threadId, String address, long date, int type, int people) {
    SmsMsg x = new SmsMsg();
    x.id = id; x.threadId = threadId; x.address = address; x.date = date;
    x.type = type; x.people = people; x.body = "b" + id; x.read = true;
    return x;
  }

  static SmsStore.Thread find(List<SmsStore.Thread> ts, long anyId) {
    for (SmsStore.Thread t : ts) for (long i : t.ids) if (i == anyId) return t;
    return null;
  }

  public static void main(String[] a) {
    // Newest first, the order the store answers in.
    List<SmsMsg> rows = new ArrayList<SmsMsg>();
    // One person, two thread ids, split by direction. 9 is the newest.
    rows.add(m(5, 9, "5551234567",  5000, 2, 1));   // sent
    rows.add(m(4, 7, "+15551234567", 4000, 1, 1));  // received
    rows.add(m(3, 9, "5551234567",  3000, 2, 1));   // sent
    // A group picture message that happens to name the same person as its address.
    rows.add(m(2, 12, "+15551234567", 2000, 1, 3));
    // Somebody else entirely.
    rows.add(m(1, 20, "+15559998888", 1000, 1, 1));

    List<SmsStore.Thread> ts = SmsStore.fold(null, rows, false);
    say("conversations", ts.size());

    SmsStore.Thread person = find(ts, 7);
    say("person.ids", person == null ? "none" : person.ids.length);
    say("person.count", person == null ? -1 : person.count);
    // A reply must join the conversation's NEWEST thread id, not whichever was seen first by date.
    say("person.id", person == null ? -1 : person.id);
    say("person.holds9", person != null && find(ts, 9) == person);

    SmsStore.Thread group = find(ts, 12);
    say("group.separate", group != null && group != person);
    say("group.ids", group == null ? -1 : group.ids.length);

    // Placeholders and arguments must agree, or the IN clause throws at the provider.
    say("marks", SmsStore.marks(3));
    say("args", SmsStore.args(new long[]{7, 9}).length);
  }
}
"""


def _run():
    tmp = tempfile.mkdtemp(prefix="pc-threadid-")
    h = os.path.join(tmp, "ThreadIdHarness.java")
    with open(h, "w") as f:
        f.write(HARNESS)
    src = [os.path.join(SMS, s) for s in SOURCES] + [h]
    r = subprocess.run([JAVAC, "-nowarn", "-Xlint:-options", "-source", "11", "-target", "11",
                        "-classpath", JAR, "-d", tmp, "-sourcepath", ac.JAVA] + src,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    r = subprocess.run([JAVARUN, "-cp", tmp + os.pathsep + JAR,
                        "place.poster.app.sms.ThreadIdHarness"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-4000:]
    shutil.rmtree(tmp, ignore_errors=True)
    return dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)


def _strip_comments(src):
    """Code only. A rule asserted against a file that DESCRIBES the rule in a comment passes on the
    strength of its own documentation, which has happened here four times."""
    out = []
    i = 0
    while i < len(src):
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = len(src) if j < 0 else j + 2
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = len(src) if j < 0 else j
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def _method(src, signature):
    """One method's body, by brace matching -- not `src.index(name)`, which anchors on the first
    mention and can read a different method entirely."""
    i = src.index(signature)
    start = src.index("{", i)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("unbalanced braces after " + signature)


@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class ReplyJoinsItsConversation(unittest.TestCase):
    """THE SPLIT WAS BEING CREATED BY THIS APP, not merely displayed by it.

    `Telephony.Threads.getOrCreateThreadId` resolves an address through the canonical-addresses
    table, and the emphasis is on CREATE: handed a spelling that table has not seen -- "5551234567"
    typed into a conversation the carrier delivers as "+15551234567" -- it mints a NEW thread rather
    than finding the existing one. So every message sent after this app became the default landed in
    a second thread, and a conversation showed replies up to the day the messaging app was switched
    and nothing after it. Reported as "i see replies i made to my dad at jul 2, nothing after which
    is today".
    """

    def test_a_stored_reply_prefers_the_conversations_own_thread_id(self):
        src = _strip_comments(open(os.path.join(SMS, "SmsStore.java")).read())
        body = _method(src, "public static Uri storeSent(Context ctx, String address, String body, "
                            "long dateMs, int type,")
        self.assertIn("threadId > 0 ? threadId : threadIdFor(ctx, address)", body,
                      "a reply must join the conversation it was typed in, not resolve an address")

    def test_the_conversation_screen_sends_with_its_thread_id(self):
        src = _strip_comments(open(os.path.join(SMS, "ThreadActivity.java")).read())
        self.assertIn("SmsSender.send(this, address, body, threadId)", src,
                      "the screen knows which conversation this is; sending without it re-splits it")

    def test_only_recipients_decide_whether_a_picture_message_is_a_group(self):
        # Counting every address counted the phone's own number on an incoming message, so an
        # ordinary one-to-one picture message looked like a group of two and was held out of its own
        # conversation -- leaving the person split in exactly the way this all exists to fix.
        src = _strip_comments(open(os.path.join(SMS, "MmsStore.java")).read())
        body = _method(src, "private static void fillAddresses(Context ctx, List<SmsMsg> rows)")
        self.assertIn("people.add(SmsKeys.matchKey(a))", body)
        adds = [ln for ln in body.splitlines() if "people.add(" in ln]
        self.assertEqual(len(adds), 1, "one place decides this")
        self.assertIn("ADDR_TO", adds[0],
                      "only TO addresses are counted; FROM is the other person, not a crowd")


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(JAR is None, "no android.jar on this node")
@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class ThreadIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run()

    def test_a_person_split_across_two_thread_ids_is_one_conversation(self):
        # Keyed on thread id this is 3 conversations for 2 people, and the "Mom" row you tap holds
        # half of what she sent you.
        self.assertEqual(self.out["conversations"], "3",
                         "expected the split person, their group, and one other")
        self.assertEqual(self.out["person.ids"], "2", "both of the person's thread ids kept")
        self.assertEqual(self.out["person.count"], "3", "every message, both directions")
        self.assertEqual(self.out["person.holds9"], "true")

    def test_a_reply_joins_the_conversations_newest_thread_id(self):
        # Rows arrive newest first, so the first one seen carries it. Sending on a stale id is how a
        # reply lands in the half of the conversation the other phone is not looking at.
        self.assertEqual(self.out["person.id"], "9")

    def test_a_group_is_never_folded_into_a_members_private_conversation(self):
        # It carries one participant's address like any other message, so only `people` separates
        # them -- and getting this wrong puts messages several people can read into a thread that
        # looks private.
        self.assertEqual(self.out["group.separate"], "true")
        self.assertEqual(self.out["group.ids"], "1")

    def test_the_in_clause_placeholders_match_their_arguments(self):
        self.assertEqual(self.out["marks"], "?,?,?")
        self.assertEqual(self.out["args"], "2")


if __name__ == "__main__":
    unittest.main()
