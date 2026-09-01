"""THE LAUNCHER'S TEXTS HAS A BACKUP CONTROL — because without one it was indistinguishable from
an app that backs nothing up.

Reported as: "one thing about the texts app, its confusing since the launcher is missing features
like sync and backup that the posterchan -> texts app uses".

That was true, and it was a WINDOW that was missing rather than machinery. The screen already
nudges a pass when it opens (`SignerRelayService.sweepSms`, whose own comment says it exists for
exactly this screen), and `SmsArchive` already records both the high-water mark and the last pass's
own sentence. Nothing showed either, and nothing could ask again — so a phone that was quietly
failing to publish looked identical to one that was working.

The sentence matters most. `sweepSms` writes "not archiving: the background signer is switched off,
so this phone has no relay connection to publish through" when it cannot run at all, and until now
that answer was written to prefs and read by nobody.

`rescan()` clears the mark — the same deliberate "read the whole phone again" the web client's
Re-scan does. It is a person asking, so it is unbounded on purpose; each PASS still bounds itself,
because the phone is in somebody's hand.

Java is compiled, not grepped: a missing import or a method that does not exist is a build failure
for the whole APK, and the regex tests in this repo have been green through exactly that before.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMS = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms"
ACTIVITY = (SMS / "ThreadListActivity.java").read_text(encoding="utf-8")
ARCHIVE = (SMS / "SmsArchive.java").read_text(encoding="utf-8")
SERVICE = (ROOT / "mobile/android/app/src/main/java/place/poster/app/signer/SignerRelayService.java"
           ).read_text(encoding="utf-8")
LAYOUT = (ROOT / "mobile/android/app/src/main/res/layout/sms_list.xml").read_text(encoding="utf-8")
STRINGS = (ROOT / "mobile/android/app/src/main/res/values/strings.xml").read_text(encoding="utf-8")


def test_the_control_exists_and_is_bound():
    """A button in the layout that nothing clicks is the same as no button."""
    assert 'android:id="@+id/pc_sms_backup"' in LAYOUT
    assert "findViewById(R.id.pc_sms_backup).setOnClickListener" in ACTIVITY
    assert "showBackup()" in ACTIVITY


def test_it_shows_what_the_archive_actually_recorded():
    """Both halves: how far it has got, and what the last pass said. The mark alone cannot explain
    a phone that is not publishing; the sentence can."""
    body = ACTIVITY[ACTIVITY.index("private void showBackup()"):]
    body = body[:body.index("\n    }\n") + 6]
    assert "SmsArchive.mark(this)" in body
    assert "SmsArchive.last(this)" in body, (
        "the last pass's own words are not shown — that is where 'the background signer is switched "
        "off' reaches somebody")


def test_it_can_ask_again_and_can_start_over():
    """Two different asks. "Back up now" nudges a pass; "Re-scan everything" clears the mark first,
    which is the web client's Re-scan and the only way back out of a completed-but-wrong archive."""
    body = ACTIVITY[ACTIVITY.index("private void showBackup()"):]
    body = body[:body.index("\n    }\n") + 6]
    assert body.count("SignerRelayService.sweepSms(") >= 2, "one of the two actions does not sweep"
    assert "SmsArchive.rescan(" in body, "there is no way to re-read the whole phone"


def test_every_call_it_makes_exists_on_the_other_side():
    """The cheap half of compiling: these are static methods on classes this file does not own."""
    assert "public static void rescan(Context ctx)" in ARCHIVE
    assert "public static long mark(Context ctx)" in ARCHIVE
    assert "public static String last(Context ctx)" in ARCHIVE
    assert "public static void sweepSms(Context ctx)" in SERVICE


def test_the_strings_it_shows_are_declared():
    """A missing string resource is a compile error, and a wrong one is an empty dialog."""
    for name in re.findall(r"R\.string\.(sms_backup[a-z_]*)", ACTIVITY):
        assert f'name="{name}"' in STRINGS, f"@string/{name} is used but never declared"


def test_nothing_is_read_or_encrypted_on_the_ui_thread():
    """The reason this is a nudge and not a sweep. "encrypting and copying messages to blossom makes
    it glitchy" is what an unbounded pass on the foreground feels like; the service decides whether
    a relay is even connected."""
    body = ACTIVITY[ACTIVITY.index("private void showBackup()"):]
    body = body[:body.index("\n    }\n") + 6]
    for forbidden in ("SmsArchive.sweep(", "SmsArchive.commit(", "readWhole("):
        assert forbidden not in body, f"{forbidden} runs on the UI thread from the backup dialog"


@pytest.mark.skipif(not shutil.which("javac"), reason="javac not available")
def test_the_activity_still_parses_as_java():
    """A syntax error here does not fail one screen, it fails the APK build — and this repo has
    shipped regex-green tests over a file that could not compile before."""
    src = SMS / "ThreadListActivity.java"
    done = subprocess.run(["javac", "-proc:none", "-d", "/tmp/pc-javac-parse", str(src)],
                          capture_output=True, text=True, timeout=180)
    # Android classes are absent here, so resolution errors are expected; SYNTAX errors are not.
    bad = [l for l in done.stderr.splitlines()
           if re.search(r"(illegal start|reached end of file|';' expected|class, interface,"
                        r"|not a statement|<identifier> expected)", l)]
    assert not bad, "ThreadListActivity.java no longer parses:\n" + "\n".join(bad[:6])
