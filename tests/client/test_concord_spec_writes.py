"""Concord: what WE write must be what the spec says (2026-09-28 CORD review).

  * CORD-02 §5 — we never published Guestbook Joins/Leaves (kind 3306), so our members were invisible
    in Armada/Vector member lists until they posted and a member who left stayed "present";
  * CORD-03 §3 — replies put every participant and mention in uppercase `P`, which must name only the
    thread root's author, inherited from the parent verbatim.

Runs the shipped reader and concord.js under node against a real created community.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_guestbook_writes_and_reply_tags_follow_the_spec():
    r = subprocess.run(["node", "tests/client/concord_guestbook_writer_runtime.mjs"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:] or r.stdout[-2000:]
    assert "guestbook join/leave writer passed" in r.stdout and "CORD-03 reply tags passed" in r.stdout


def test_join_and_leave_are_published_from_the_real_flows():
    src = (ROOT / "static/js/client/concord.js").read_text()
    assert "void publishGuestbook(p,latest[joined],'join')" in src, "accepting an invite publishes no Join"
    leave = src[src.index("async function leaveArmadaMembership("):]
    leave = leave[:leave.index("\n  }\n")]
    assert leave.index("publishGuestbook(p,room,'leave')") < leave.index("cordWriteMembership("), \
        "the Leave must go out before the keys are dropped"
    send = src[src.index("send.onclick=async()=>{"):]
    send = send[:send.index("\n")]
    assert "mentionTags.push(['P'" not in send, "mentions still go into uppercase P"
