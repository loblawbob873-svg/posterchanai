"""A GROUP CONVERSATION LOOKS AND BEHAVES LIKE ONE, in the launcher's Texts app.

Reported, in the user's words: "I am in a group chat and had no idea when I opened it — it only
showed one number", "you have no idea who is saying what in the group chat", "you only see the
latest replier when you open the message up so it looks like a regular convo."

One root cause under all three. SmsStore reads every participant (recipient_ids resolved against
canonical-addresses) and then throws them away — `t.address = people.get(0)` — so ThreadActivity was
handed ONE number for a conversation with four people. It titled itself with that member, drew every
incoming bubble identically whoever sent it, offered to call that one person, and, worst of all,
addressed the REPLY to them: a private answer to a group question that nobody else in the group ever
saw, with nothing on screen to say so.

The rules live in SmsGroup, which is pure Java, so this file RUNS them on a JVM rather than asserting
their source text. The screens that use them are compiled against the real android.jar by
tests/test_android_shell_compiles.py, which already builds this whole package — the rules being
right is worth nothing if the Activity holding them does not build, and that floor exists already
(its klinker shim grew the group API this change needs, signatures read off the real AAR).
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

ROOT = ac.ROOT
SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

PURE = ["SmsKeys.java", "SmsMsg.java", "SmsGroup.java"]

HARNESS = r"""
import java.util.*;
import place.poster.app.sms.SmsGroup;
import place.poster.app.sms.SmsMsg;

public class GroupHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }

  static SmsMsg from(String address) {
    SmsMsg m = new SmsMsg();
    m.address = address;
    m.type = 1;            // inbox — a message that ARRIVED
    return m;
  }

  static SmsMsg mine() {
    SmsMsg m = new SmsMsg();
    m.address = "+15550100000";
    m.type = 2;            // sent
    return m;
  }

  public static void main(String[] a) {
    List<String> two = Arrays.asList("Alice", "Bob");
    List<String> four = Arrays.asList("Alice", "Bob", "Carol", "Dave");
    List<String> seven = Arrays.asList("Alice", "Bob", "Carol", "Dave", "Erin", "Frank", "Grace");

    say("one", SmsGroup.title(Arrays.asList("Alice")));
    say("two", SmsGroup.title(two));
    say("four", SmsGroup.title(four));
    say("seven", SmsGroup.title(seven));
    say("empty", "[" + SmsGroup.title(new ArrayList<String>()) + "]");
    say("sub", SmsGroup.subtitle(4) + "|" + "[" + SmsGroup.subtitle(1) + "]");
    say("isgroup", SmsGroup.isGroup(two) + " " + SmsGroup.isGroup(Arrays.asList("Alice"))
                 + " " + SmsGroup.isGroup(null));

    // Who is speaking: the label appears where the speaker CHANGES, never in a one-to-one.
    SmsMsg alice = from("+15550101111"), bob = from("+15550102222");
    SmsMsg aliceAgain = from("(555) 010-1111");        // the same person, written differently
    say("first", SmsGroup.showsSender(true, null, alice));
    say("changed", SmsGroup.showsSender(true, alice, bob));
    say("run", SmsGroup.showsSender(true, alice, aliceAgain));
    say("after-mine", SmsGroup.showsSender(true, mine(), alice));
    say("one-to-one", SmsGroup.showsSender(false, alice, bob));
    say("outgoing", SmsGroup.showsSender(true, alice, mine()));

    // Colour follows the person, not their position in a list that can come back reordered.
    say("colour-stable", SmsGroup.colorSlot("+15550101111", 4) == SmsGroup.colorSlot("555-010-1111", 4));
    say("colour-range", SmsGroup.colorSlot("+15550101111", 4) >= 0
                      && SmsGroup.colorSlot("+15550101111", 4) < 4);

    // A reply goes to everybody.
    List<String> people = Arrays.asList("+15550101111", "+15550102222", "+15550103333");
    say("reply-all", SmsGroup.replyTo(people, "+15550101111", "").size());
    say("reply-drops-me", SmsGroup.replyTo(people, "", "(555) 010-2222").size());
    say("reply-one", SmsGroup.replyTo(Arrays.asList("+15550101111"), "", "").size());
    say("reply-fallback", SmsGroup.replyTo(new ArrayList<String>(), "+15550109999", "").get(0));
    // Being wrong about which number is "me" must never leave a message addressed to nobody.
    say("reply-never-empty", SmsGroup.replyTo(Arrays.asList("+15550101111"), "+15550101111",
                                              "+1 555 010 1111").size());
    say("reply-dedupes", SmsGroup.replyTo(Arrays.asList("+15550101111", "555-010-1111"), "", "").size());
  }
}
"""


def _run_harness():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "GroupHarness.java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        r = subprocess.run([JAVAC, "-d", tmp, "-sourcepath", ac.JAVA, src]
                           + [os.path.join(SMS, p) for p in PURE],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-3000:]
        r = subprocess.run([JAVARUN, "-cp", tmp, "GroupHarness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-3000:]
    for line in r.stdout.splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            out[k] = v
    return out


@pytest.fixture(scope="module")
def ran():
    if not (JAVAC and JAVARUN):
        pytest.skip("needs a JDK")
    return _run_harness()


def test_a_group_is_titled_after_everyone_in_it(ran):
    """"It only showed one number." A title names the people, and when it cannot name them all it
    still says HOW MANY — "Alice, Bob, Carol" trailing off reads as a long name, "& 4 more" reads
    as a group."""
    assert ran["one"] == "Alice"
    assert ran["two"] == "Alice, Bob"
    assert ran["four"] == "Alice, Bob, Carol, Dave", "one name left over is shorter spelled out"
    assert ran["seven"] == "Alice, Bob, Carol & 4 more"
    assert ran["empty"] == "[]", "no participants is not a group of nobody"


def test_the_second_line_says_the_word_group(ran):
    assert ran["sub"] == "Group · 4 people|[]"
    assert ran["isgroup"] == "true false false"


def test_every_message_says_who_sent_it_when_the_speaker_changes(ran):
    """"You have no idea who is saying what." Labelled at the start of each run only: a label on
    all five of Alice's messages buries the one line that matters, which is Bob answering."""
    assert ran["first"] == "true"
    assert ran["changed"] == "true"
    assert ran["run"] == "false", "the same person written two ways is one speaker"
    assert ran["after-mine"] == "true", "my own message ends the run above it"
    assert ran["one-to-one"] == "false", "a two-person conversation needs no labels"
    assert ran["outgoing"] == "false", "my own messages are on my side already"


def test_a_persons_colour_follows_the_person(ran):
    assert ran["colour-stable"] == "true", "a colour derived from list position moves when the list does"
    assert ran["colour-range"] == "true"


def test_a_reply_goes_to_the_whole_group(ran):
    """The worst of the three: a reply addressed to one member is a private message the rest of the
    group never sees, and nothing on screen said so."""
    assert ran["reply-all"] == "3"
    assert ran["reply-drops-me"] == "2", "a carrier that lists you among the recipients"
    assert ran["reply-one"] == "1"
    assert ran["reply-fallback"] == "+15550109999", "no participant list → the address we had"
    assert ran["reply-never-empty"] == "1", "being wrong about 'me' must not address it to nobody"
    assert ran["reply-dedupes"] == "1"


def test_the_open_conversation_reads_the_participant_list():
    """The data was always there and the screen never asked for it. These are the three places it
    now has to: the header, the bubbles and the reply."""
    body = open(os.path.join(SMS, "ThreadActivity.java"), encoding="utf-8").read()
    assert "SmsStore.participants(" in body, "the open conversation never asks who is in it"
    assert "SmsGroup.showsSender(" in body, "bubbles do not name their sender"
    assert "SmsGroup.replyTo(" in body, "a reply is still addressed to one member"
    store = open(os.path.join(SMS, "SmsStore.java"), encoding="utf-8").read()
    assert "public static List<String> participants(" in store


def test_a_group_text_is_one_message_to_everybody():
    """SMS to several people is several PRIVATE messages, each starting its own conversation — the
    bug wearing a different hat. A group text has to leave as an MMS with setGroup(true)."""
    body = open(os.path.join(SMS, "MmsSender.java"), encoding="utf-8").read()
    assert "sendGroupText(" in body
    assert "settings.setGroup(" in body, "without this the library sends each recipient a private copy"
