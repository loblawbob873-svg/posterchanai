"""Run the settings deletion action with mixed event kinds and relay failures."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'static/js/client/app.js').read_text()
ACTION = SOURCE[SOURCE.index('  const _DN_BATCH = 100;'):SOURCE.index('  // The wider relays')]


def run_action(events, *, fail=False, change_account=False, incomplete=False):
    js = r'''
const vm=require('node:vm');
const events=EVENTS, removed=[], sent=[], confirms=[], status={textContent:''};
let calls=0;
const context={GUEST:false,ME:{pubkey:'mine'},VIEW:'settings',
 document:{getElementById:()=>status},
 uiConfirm:async msg=>{confirms.push(msg); if(CHANGE && confirms.length===2)context.ME={pubkey:'other'};return true;},
 Relay:{query:async filters=>{calls++; const f=filters[0]; const batch=events.filter(e=>f.kinds.includes(e.kind) && e.created_at<=f.until).sort((a,b)=>b.created_at-a.created_at).slice(0,f.limit); batch.complete=!INCOMPLETE; return batch;},publishTo:async()=>{}},
 publish:async(kind,content,tags)=>{sent.push({kind,tags});return {ok:!FAIL,ev:{}};},
 Store:{removeEvent:id=>removed.push(id)},invalidateCounts:()=>{},_writeRelays:()=>[]};
vm.createContext(context);
vm.runInContext(ACTION,context);
context._deleteAllMyNotes().then(()=>process.stdout.write(JSON.stringify({removed,sent,confirms,status:status.textContent})));
'''
    for key, value in [('EVENTS', events), ('CHANGE', change_account), ('FAIL', fail), ('INCOMPLETE', incomplete), ('ACTION', ACTION)]:
        js = js.replace(key, json.dumps(value))
    result = subprocess.run(['node'], input=js, capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def event(kind, author='mine'):
    return {'id': f'{author}-{kind}', 'pubkey': author, 'kind': kind, 'created_at': 100}


def test_deletes_own_posts_reactions_and_both_repost_kinds():
    result = run_action([event(k) for k in [0, 1, 3, 4, 6, 7, 16, 1059]] + [event(7, 'someone-else')])
    assert set(result['removed']) == {'mine-1', 'mine-6', 'mine-7', 'mine-16'}
    assert result['sent'][0]['kind'] == 5
    assert {tuple(t) for t in result['sent'][0]['tags'] if t[0] == 'k'} == {('k', str(k)) for k in [1, 6, 7, 16]}
    assert 'likes/reactions and reposts' in result['confirms'][0]


def test_failed_publish_keeps_events_locally():
    result = run_action([event(6), event(7)], fail=True)
    assert result['removed'] == []
    assert '2 failed' in result['status']


def test_account_change_stops_deletion():
    result = run_action([event(7)], change_account=True)
    assert result['sent'] == []
    assert 'Account changed' in result['status']


def test_large_history_is_not_truncated_at_eighty_pages():
    events = [dict(event([1, 6, 7, 16][i % 4]), id=str(i), created_at=20000-i) for i in range(16200)]
    result = run_action(events)
    assert set(result['removed']) == {e['id'] for e in events}
    assert all(len([t for t in batch['tags'] if t[0] == 'e']) <= 100 for batch in result['sent'])


def test_a_full_timestamp_page_does_not_hide_older_activity():
    events = [dict(event(7), id=str(i)) for i in range(250)] + [dict(event(6), created_at=99)]
    result = run_action(events)
    assert set(result['removed']) == {e['id'] for e in events}


def test_incomplete_history_is_reported_before_and_after_deletion():
    result = run_action([event(7)], incomplete=True)
    assert 'history was incomplete' in result['confirms'][1]
    assert 'history was incomplete' in result['status']


def test_empty_timeout_does_not_claim_the_account_has_no_activity():
    result = run_action([], incomplete=True)
    assert 'history was incomplete' in result['status']
    assert result['sent'] == []


def test_relay_page_ceiling_is_reported_as_incomplete():
    events = [dict(event(7), id=str(i)) for i in range(5001)]
    result = run_action(events)
    assert 'history was incomplete' in result['confirms'][1]
    assert 'history was incomplete' in result['status']
    assert len(result['removed']) == 5000
