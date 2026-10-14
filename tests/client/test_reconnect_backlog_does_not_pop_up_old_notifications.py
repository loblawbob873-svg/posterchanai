"""An old notification replayed by a relay that connected late is counted, never popped up.

2026-10-13: "I opened git and got stale git issue notifications just now". Opening Git dials the repo's relays; the
pool re-arms the notifications subscription on them and they replay their backlog. Anything newer than the last time
Notifications was opened then toasted and OS-notified as if it had just been filed. Follows already only rang when
made since this session started watching (Sub B); mentions/issues/zaps now obey the same rule. Runs the SHIPPED
`_livePing` and checks the subscription asks it.
"""
import subprocess

from tests.client_source import client_source


def test_a_replayed_old_issue_is_not_popped_up_and_a_new_one_is():
    src = client_source()
    a = src.index("  function _livePing(ev){")
    fn = src[a:src.index("\n  }\n", a) + 4]
    sub = src[src.index("_notifSeenIds.clear();"):src.index("onEose: ()=>{ _notifReady=true;")]
    assert "_livePing(ev)) notifPing(ev)" in sub, "the notifications subscription pops up without asking _livePing"
    harness = r'''
const assert=require('node:assert/strict');
let _notifReady=true; const _watchSince=1_000_000, _LIVE_SLACK=600;
FN
assert.equal(_livePing({created_at:_watchSince-86400}), false, 'an issue filed yesterday, replayed now, popped up');
assert.equal(_livePing({created_at:_watchSince+30}), true, 'an issue filed just now did not pop up');
assert.equal(_livePing({created_at:_watchSince-60}), true, 'an author a minute slow lost their notification');
_notifReady=false;
assert.equal(_livePing({created_at:_watchSince+30}), false, 'the backlog before EOSE popped up');
'''.replace("FN", fn)
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr[-1500:]
