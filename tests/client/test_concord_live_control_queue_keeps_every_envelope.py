"""Live control envelopes that arrive while a Concord metadata refresh is running are QUEUED PER COMMUNITY and MERGED.

Code review, 2026-10-23: the queue was one slot (`controlPending=only`), so a second batch (a new channel) arriving
~300 ms after a first (a ban) replaced it while a rotating refresh ran; the live subscription never re-delivers an
envelope it has seen, so the ban then waited for the once-a-minute history read -- the delay the live subscription
exists to remove. This runs the SHIPPED queue (sliced from concord.js) under node.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'static/js/client/concord.js').read_text()


def _slice():
    start = SRC.index('  const controlLive={key:')
    end = SRC.index('\n  }', SRC.index('function queueControlRefresh(only){')) + 4
    return SRC[start:end]


def test_two_batches_for_one_community_are_both_kept_and_others_are_separate():
    script = _slice() + r'''
queueControlRefresh({identity:'room-a', extra:[{id:'ban'}]});
queueControlRefresh({identity:'room-a', extra:[{id:'new-channel'}]});
queueControlRefresh({identity:'room-b', extra:[{id:'pin'}]});
const a=controlPending.get('room-a'), b=controlPending.get('room-b');
console.log(JSON.stringify({a:a.extra.map(e=>e.id), b:b.extra.map(e=>e.id), n:controlPending.size}));
'''
    out = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['a'] == ['ban', 'new-channel'], ('a queued control envelope was dropped', got)
    assert got['b'] == ['pin'] and got['n'] == 2, got


def test_the_refresh_takes_its_next_room_from_the_queue_and_removes_it():
    fin = SRC[SRC.index("console.warn('Concord metadata sync failed',e);}finally{"):][:260]
    assert 'controlPending.values().next().value' in fin and 'controlPending.delete(next.identity)' in fin, fin
