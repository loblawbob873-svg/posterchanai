"""Phone and Texts can block a number -- the phone's own blocked-numbers list, one list for both.

Reported: "Phone App -> No way to block callers." / "Texts App -> No way to block texters."

Android enforces BlockedNumberContract itself (Telecom refuses the call, telephony drops the SMS/MMS),
so the app's job is the MENU and an honest report of what the list says after a change. The pure parts
are RUN here under `java`; tests/test_android_shell_compiles.py compiles the activities that draw them.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

APPJ = os.path.join(ac.JAVA, "place", "poster", "app")
JAVAC, JAVA = shutil.which("javac"), shutil.which("java")

HARNESS = r"""
import place.poster.app.phone.RowMenu;
import place.poster.app.phone.Blocklist;
import place.poster.app.sms.ThreadMenu;
public class BlockHarness { public static void main(String[] a) {
  System.out.println(RowMenu.actions(false, true, "+15550100", Boolean.FALSE));
  System.out.println(RowMenu.actions(false, true, "+15550100", Boolean.TRUE));
  System.out.println(RowMenu.actions(false, true, "+15550100", null));
  System.out.println(RowMenu.actions(false, true, "", Boolean.FALSE));
  System.out.println(java.util.Arrays.toString(ThreadMenu.actions(false, Boolean.FALSE)));
  System.out.println(java.util.Arrays.toString(ThreadMenu.actions(true, Boolean.TRUE)));
  System.out.println(java.util.Arrays.toString(ThreadMenu.actions(false, null)));
  System.out.println(Blocklist.blockable("+1 555 0100") + " " + Blocklist.blockable("+15550100;+15550101")
      + " " + Blocklist.blockable("") + " " + Blocklist.blockable("GOOGLE") + " " + Blocklist.blockable(" - "));
  System.out.println(Blocklist.outcome(true, Boolean.TRUE) + " " + Blocklist.outcome(false, Boolean.FALSE) + " "
      + Blocklist.outcome(true, Boolean.FALSE) + " " + Blocklist.outcome(true, null));
  System.out.println(RowMenu.BLOCK + " " + RowMenu.UNBLOCK + " " + ThreadMenu.BLOCK + " " + ThreadMenu.UNBLOCK
      + " " + Blocklist.BLOCKED + " " + Blocklist.UNBLOCKED + " " + Blocklist.REFUSED + " " + Blocklist.UNKNOWN);
}}
"""


@pytest.mark.skipif(not (JAVAC and JAVA and ac.android_jar()), reason="JDK / android.jar not on this node")
def test_the_menus_offer_block_or_unblock_only_when_the_list_can_be_asked():
    tmp = tempfile.mkdtemp()
    h = os.path.join(tmp, "BlockHarness.java")
    open(h, "w").write(HARNESS)
    jar = ac.android_jar()
    srcs = [os.path.join(APPJ, "phone", "RowMenu.java"), os.path.join(APPJ, "phone", "Blocklist.java"),
            os.path.join(APPJ, "sms", "ThreadMenu.java"), h]
    r = subprocess.run([JAVAC, "-nowarn", "-cp", jar, "-d", tmp] + srcs, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([JAVA, "-cp", tmp + os.pathsep + jar, "BlockHarness"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    rb, ru, tb, tu, ob, ou, orf, ounk = (int(x) for x in lines[9].split())
    not_blocked, blocked, unknown, no_number = (eval(l) for l in lines[:4])
    assert rb in not_blocked and ru not in not_blocked, not_blocked
    assert ru in blocked and rb not in blocked, blocked
    assert rb not in unknown and ru not in unknown, ("a Block offered when the list cannot be asked", unknown)
    assert rb not in no_number, no_number
    t_off, t_on, t_unknown = (eval(l) for l in lines[4:7])
    assert tb in t_off and t_on[1] == tu and tb not in t_unknown and tu not in t_unknown, (t_off, t_on, t_unknown)
    assert lines[7] == "true false false true false", ("blockable", lines[7])
    assert lines[8] == f"{ob} {ou} {orf} {ounk}", ("the outcome is what the list says, not what was asked", lines[8])


# The two activities that draw these menus are compiled against the real android.jar by
# tests/test_android_shell_compiles.py (the whole sms + phone packages, with its shims).


def test_both_screens_wire_the_shared_list_and_report_the_measured_outcome():
    dialer = open(os.path.join(APPJ, "phone", "DialerActivity.java")).read()
    menu = dialer[dialer.index("private void rowMenu("):][:4000]
    assert "Blocklist.isBlocked(this, r.number)" in menu and "RowMenu.BLOCK" in menu and "setBlocked(" in menu
    texts = open(os.path.join(APPJ, "sms", "ThreadListActivity.java")).read()
    tmenu = texts[texts.index("private void threadMenu("):][:3000]
    assert "Blocklist.isBlocked(this, t.address)" in tmenu and "setBlocked(t, " in tmenu
    setb = texts[texts.index("private void setBlocked("):][:900]
    assert "Blocklist.set(" in setb and "Blocklist.outcome(" in setb
    strings = open(os.path.join(ac.ROOT, "mobile/android/app/src/main/res/values/strings.xml")).read()
    for name in ("pc_block", "pc_unblock", "pc_blocked_done", "pc_unblocked_done", "pc_block_refused", "pc_block_unknown"):
        assert f'name="{name}"' in strings, name
