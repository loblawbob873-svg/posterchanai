"""The phone's native Texts list hides the same archived conversations every other device does.

Archiving happens on the Texts screen (any device) as one encrypted `pcai:smsarc:` record per
conversation; the relay service on the phone receives those records and the native thread list leaves
the conversation out -- until a message newer than the archive arrives. The rule is pure Java
(ArchivedThreads) and is RUN here, against the same cases as the JavaScript `isArchived` in sms.js, so
the two cannot disagree about which conversations are filed away.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
SMSJS = os.path.join(ac.ROOT, "static", "js", "client", "sms.js")
JAVAC, JAVA, NODE = shutil.which("javac"), shutil.which("java"), shutil.which("node")

# (records, address, newest message date) -> hidden?
#   records: [doc, key-source-address-or-empty, upto, created_at]
CASES = [
    ("archived, nothing newer", [["d1", "+1 (555) 010-0100", 5000, 10]], "+15550100100", 5000, True),
    ("same number written differently", [["d1", "5550100100", 5000, 10]], "+1 555 010 0100", 4000, True),
    ("a newer message brings it back", [["d1", "+15550100100", 5000, 10]], "+15550100100", 5001, False),
    ("another conversation is untouched", [["d1", "+15550100100", 5000, 10]], "+15550199999", 10, False),
    ("unarchived (newer empty record)", [["d1", "+15550100100", 5000, 10], ["d1", "", 0, 11]], "+15550100100", 10, False),
    ("a late OLDER archive does not undo an unarchive", [["d1", "", 0, 11], ["d1", "+15550100100", 5000, 10]], "+15550100100", 10, False),
    ("nothing archived", [], "+15550100100", 10, False),
]

HARNESS = r"""
import place.poster.app.sms.*;
public class ArcHarness {
  public static void main(String[] a) throws Exception {
    java.io.BufferedReader in = new java.io.BufferedReader(new java.io.InputStreamReader(System.in));
    String line; StringBuilder out = new StringBuilder("[");
    while ((line = in.readLine()) != null && !line.isEmpty()) {
      String[] f = line.split("\u0001", -1);           // address, date, then records
      ArchivedThreads t = new ArchivedThreads();
      for (int i = 2; i + 3 < f.length; i += 4)
        t.put(f[i], f[i + 1].isEmpty() ? "" : SmsKeys.matchKey(f[i + 1]), Long.parseLong(f[i + 2]), Long.parseLong(f[i + 3]));
      ArchivedThreads back = ArchivedThreads.parse(t.serialize());   // what the phone keeps
      if (out.length() > 1) out.append(',');
      out.append(t.hidden(f[0], Long.parseLong(f[1]))).append(',').append(back.hidden(f[0], Long.parseLong(f[1])));
    }
    System.out.println(out.append(']'));
  }
}
"""


@pytest.mark.skipif(not (JAVAC and JAVA), reason="JDK not installed")
def test_the_phone_rule_hides_exactly_the_archived_conversations():
    tmp = tempfile.mkdtemp()
    h = os.path.join(tmp, "ArcHarness.java")
    open(h, "w").write(HARNESS)
    src = [os.path.join(SMS, "ArchivedThreads.java"), os.path.join(SMS, "SmsKeys.java")]
    r = subprocess.run([JAVAC, "-nowarn", "-d", tmp] + src + [h], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = []
    for _, recs, addr, date, _ in CASES:
        parts = [addr, str(date)]
        for rec in recs:
            parts += [rec[0], rec[1], str(rec[2]), str(rec[3])]
        lines.append("\u0001".join(parts))
    r = subprocess.run([JAVA, "-cp", tmp, "ArcHarness"], input="\n".join(lines) + "\n\n",
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for i, (name, _, _, _, want) in enumerate(CASES):
        assert got[2 * i] is want, name
        assert got[2 * i + 1] is want, name + " (after the phone stored and re-read it)"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_web_rule_agrees_case_for_case():
    js = open(SMSJS, encoding="utf-8").read()
    key = re.search(r"\n  function key\(addr\)\{.*?\n  \}\n", js, re.S).group(0)
    upto = re.search(r"\n  function archivedUpto\(k\)\{.*?\n  \}\n", js, re.S).group(0)
    arc = re.search(r"\n  function isArchived\(t\)\{.*?\n  \}\n", js, re.S).group(0)
    # absorb()'s newest-wins, reduced to what it stores: one entry per document, a later
    # created_at replaces it, an empty record is {k:''}.
    prog = key + upto + arc + r"""
      const cases = JSON.parse(process.argv[1]), out = [];
      for(const [recs, addr, date] of cases){
        const S = { archived: new Map() };
        globalThis.S = S;
        for(const [d, src, u, at] of recs){
          const had = S.archived.get(d);
          if(had && had._at >= at) continue;
          S.archived.set(d, src ? {k:key(src), upto:u, _at:at} : {k:'', upto:0, _at:at});
        }
        out.push(isArchived({key:key(addr), date}));
      }
      console.log(JSON.stringify(out));
    """
    prog = prog.replace("function archivedUpto", "var S; function archivedUpto")
    prog = "var S;" + prog.replace("var S; function archivedUpto", "function archivedUpto").replace(
        "globalThis.S = S;", "").replace("const S = { archived: new Map() };", "S = { archived: new Map() };")
    r = subprocess.run([NODE, "-e", prog, json.dumps([[c[1], c[2], c[3]] for c in CASES])],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for (name, *_, want), g in zip(CASES, got):
        assert g is want, name


def test_the_relay_service_feeds_the_list_and_the_list_honours_it():
    svc = open(os.path.join(ac.JAVA, "place", "poster", "app", "signer", "SignerRelayService.java")).read()
    body = svc[svc.index("private void smsOutbox(String url, JSONObject ev)"):][:900]
    assert "SmsArchived.absorb(" in body and body.index("SmsArchived") < body.index("SmsOutbox.isRequest"), \
        "archive records must be taken before the outbox filter drops them"
    assert svc.count("SmsArchived.filter(") == 2, "both REQs (connect and after AUTH) must ask for the archive records"
    lst = open(os.path.join(SMS, "ThreadListActivity.java")).read()
    draw = lst[lst.index("private void draw()"):][:1200]
    assert "SmsArchived.load(this)" in draw and "archived.hidden(t.address, t.date)" in draw
    js = open(SMSJS, encoding="utf-8").read()
    assert "['l', 'pcai-smsarc']" in js and "D_ARC = 'pcai:smsarc:'" in js
    assert open(os.path.join(SMS, "SmsArchived.java")).read().count('"pcai:smsarc:"') == 1


# ---- archiving FROM THE PHONE ("why is sms archive not on apk") --------------------------------------

WRITE_HARNESS = r"""
import place.poster.app.sms.*;
public class ArcWrite {
  public static void main(String[] a) throws Exception {
    StringBuilder o = new StringBuilder("{");
    // The record address, for the owner/number pairs the web side also hashes.
    o.append("\"docs\":[");
    for (int i = 1; i < a.length; i += 2) {
      if (i > 1) o.append(',');
      o.append('"').append(ArchivedThreads.docFor(a[i], SmsKeys.matchKey(a[i + 1]))).append('"');
    }
    o.append("],");
    // The long-press menu.
    int[] live = ThreadMenu.actions(false), filed = ThreadMenu.actions(true);
    o.append("\"menu_live\":[").append(live[0]).append(',').append(live[1]).append("],");
    o.append("\"menu_filed\":[").append(filed[0]).append(',').append(filed[1]).append("],");
    o.append("\"ARCHIVE\":").append(ThreadMenu.ARCHIVE).append(",\"UNARCHIVE\":").append(ThreadMenu.UNARCHIVE)
     .append(",\"DELETE\":").append(ThreadMenu.DELETE).append(',');
    // Which rows each view lists: [main, archived view, searching] for a filed and a live conversation.
    ArchivedThreads t = new ArchivedThreads();
    t.put("d1", SmsKeys.matchKey("+15550100100"), 5000, 10);
    o.append("\"filed\":[").append(t.listed("+15550100100", 5000, false, false)).append(',')
     .append(t.listed("+15550100100", 5000, true, false)).append(',')
     .append(t.listed("+15550100100", 5000, false, true)).append("],");
    o.append("\"live\":[").append(t.listed("+15550199999", 10, false, false)).append(',')
     .append(t.listed("+15550199999", 10, true, false)).append(',')
     .append(t.listed("+15550199999", 10, true, true)).append("],");
    // Two taps in one second must not share a timestamp: an unarchive written in the same second as
    // the archive it undoes would lose a newest-wins tie half the time.
    long first = t.nextAt("d1", 10), fresh = t.nextAt("d9", 10);
    t.put("d1", "", 0, first);
    o.append("\"next\":[").append(first).append(',').append(fresh).append(',')
     .append(t.hidden("+15550100100", 5000)).append("]}");
    System.out.println(o);
  }
}
"""

OWNERS = [("ab" * 32, "+1 (555) 010-0100"), ("0f" * 32, "5550100100"), ("12" * 32, "alice@example.com")]


def _write_side():
    tmp = tempfile.mkdtemp()
    open(os.path.join(tmp, "ArcWrite.java"), "w").write(WRITE_HARNESS)
    src = [os.path.join(SMS, n) for n in ("ArchivedThreads.java", "SmsKeys.java", "ThreadMenu.java")]
    r = subprocess.run([JAVAC, "-nowarn", "-d", tmp] + src + [os.path.join(tmp, "ArcWrite.java")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    args = [x for pair in OWNERS for x in pair]
    r = subprocess.run([JAVA, "-cp", tmp, "ArcWrite", "-"] + args, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(not (JAVAC and JAVA and NODE), reason="JDK or node not installed")
def test_the_phone_writes_the_same_record_address_as_the_web():
    """Archive on the phone, unarchive on the laptop: only works if both name the SAME record."""
    got = _write_side()["docs"]
    js = open(SMSJS, encoding="utf-8").read()
    key = re.search(r"\n  function key\(addr\)\{.*?\n  \}\n", js, re.S).group(0)
    line = re.search(r"const d = D_ARC \+ \(await sha256hex\((.*?)\)\)\.slice\(0, 24\);", js)
    assert line, "sms.js setArchived no longer derives the record address the way this test mirrors"
    prog = key + r"""
      const crypto = require('crypto');
      const sha256hex = s => crypto.createHash('sha256').update(s, 'utf8').digest('hex');
      const out = [];
      for (const [owner, addr] of JSON.parse(process.argv[1])) {
        const t = { key: key(addr) };
        out.push('pcai:smsarc:' + sha256hex(%s).slice(0, 24));
      }
      console.log(JSON.stringify(out));
    """ % line.group(1)
    r = subprocess.run([NODE, "-e", prog, json.dumps(OWNERS)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == got
    assert all(len(d) == len("pcai:smsarc:") + 24 for d in got)


@pytest.mark.skipif(not (JAVAC and JAVA), reason="JDK not installed")
def test_long_press_offers_archive_and_the_archived_view_lists_only_archived():
    g = _write_side()
    assert g["menu_live"] == [g["ARCHIVE"], g["DELETE"]], "a live conversation offers Archive, then Delete"
    assert g["menu_filed"] == [g["UNARCHIVE"], g["DELETE"]], "an archived one offers Unarchive"
    assert g["filed"] == [False, True, True], "archived: not in the list, in the Archived view, found by search"
    assert g["live"] == [True, False, True], "live: in the list, not in the Archived view, found by search"
    first, fresh, still_hidden = g["next"]
    assert first == 11 and fresh == 10, "a record never reuses the timestamp of the one it replaces"
    assert still_hidden is False, "the unarchive written in the same second wins"


def test_the_list_long_press_archives_and_the_record_reaches_a_relay():
    lst = open(os.path.join(SMS, "ThreadListActivity.java")).read()
    press = lst[lst.index("setOnItemLongClickListener"):][:400]
    assert "threadMenu(t)" in press and "confirmDeleteThread" not in press, \
        "a long press must offer Archive, not go straight to Delete"
    menu = lst[lst.index("private void threadMenu("):][:1400]
    assert "ThreadMenu.actions(" in menu and "archive(t, acts[w] == ThreadMenu.ARCHIVE)" in menu
    assert "SmsArchived.set(ThreadListActivity.this, t.address, t.date, on)" in lst
    draw = lst[lst.index("private void draw()"):][:2400]
    assert "archived.listed(t.address, t.date, archivedView, !q.isEmpty())" in draw
    assert "R.string.sms_archived_n" in draw
    lay = open(os.path.join(ac.ROOT, "mobile", "android", "app", "src", "main", "res", "layout", "sms_list.xml")).read()
    assert 'android:id="@+id/pc_sms_archived"' in lay

    arc = open(os.path.join(SMS, "SmsArchived.java")).read()
    setb = arc[arc.index("public static int set("):][:2600]
    assert "ArchivedThreads.docFor(meHex, key)" in setb and ".nextAt(doc," in setb
    assert '"pcai-sms"' in setb and "L_ARC" in setb, "both index tags, like sms.js"
    assert setb.index("pending(ctx).edit().putString(doc") < setb.index("absorb(ctx, ev)"), \
        "queued before it is applied: a record this phone believes in must reach the relay"
    svc = open(os.path.join(ac.JAVA, "place", "poster", "app", "signer", "SignerRelayService.java")).read()
    pub = svc[svc.index("private void publishSmsArchive()"):][:300]
    assert "sendArchiveRecords();" in pub, "sent on every connect, AUTH and kick"
    ok = svc[svc.index('"OK".equals(m.optString(0, ""))'):][:400]
    assert "SmsArchived.accepted(this, m.optString(1, \"\"))" in ok, "dropped from the queue only on an OK"
