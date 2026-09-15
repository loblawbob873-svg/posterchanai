"""Offline appointments must stay with their owner through account changes and async I/O."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
CALENDAR = ROOT / "static/js/client/calendar.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _run(body, source=None):
    source = source if source is not None else CALENDAR.read_text()
    api = source[source.index("    async function api("):source.index("    /* A SUBSCRIBED calendar")]
    jput = source[source.index("    const jput ="):source.index("    // ---- dates")]
    cache = source[source.index("    const CalCache = {"):source.index("    /* THE OFFLINE WRITE QUEUE")]
    queue = source[source.index("    const CalQueue = {"):source.index("    /* Paint from the cache")]
    boot = r"""
const assert=require('node:assert/strict');
let currentOwner='alice';const owner=()=>currentOwner;
const S={queued:7},requests=[],notices=[],writes=[];
const toast=message=>notices.push(message);
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
const drain=()=>new Promise(resolve=>setImmediate(resolve));
const copy=x=>JSON.parse(JSON.stringify(x));
const a={op:'put',cal:'alice-private',uid:'a',ics:'BEGIN:VCALENDAR\nprivate appointment'};
const b={op:'put',cal:'bob-private',uid:'b',ics:'Bob appointment'};
const store=new Map([['queue:alice',[a]],['queue:bob',[b]]]);
let readGate=null,writeGate=null,authGate=null,requestGate=null;
const responses=[];
const ensureAiSession=async()=>{if(authGate)await authGate.promise;};
const authFetch=async(path,opts)=>{
  requests.push({owner:owner(),path,opts});
  if(requestGate)await requestGate.promise;
  const reply=responses.shift()||200,status=typeof reply==='number'?reply:reply.status;
  return {ok:status<400,status,json:async()=>({detail:typeof reply==='number'?'fixture refusal':reply.detail})};
};
""" + api + jput + cache + queue + r"""
// Keep the real cache key selection and queue methods; only IndexedDB scheduling is controlled.
CalCache._tx=async(mode,fn)=>{
  const out=fn({get:key=>({result:copy(store.get(key)||[])}),
    put:(value,key)=>{writes.push(key);store.set(key,copy(value));}});
  if(mode==='readonly'&&readGate)await readGate.promise;
  if(mode==='readwrite'&&writeGate)await writeGate.promise;
  return out&&out.result;
};
"""
    script = boot + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
    return subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=5)


def test_an_offline_add_finishing_after_switch_stays_in_its_original_queue():
    result = _run(r"""
readGate=deferred();
const added={op:'put',cal:'alice-private',uid:'new',ics:'new appointment'};
const pending=CalQueue.add(added);await drain();
currentOwner='bob';readGate.resolve();await pending;
assert.deepEqual(store.get('queue:alice'),[a,added],'offline appointment was lost from its owner queue');
assert.deepEqual(store.get('queue:bob'),[b],'old account data overwrote the new account queue');
assert.deepEqual(writes,['queue:alice']);
assert.equal(S.queued,7,'old account completion changed the new account pending badge');
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("boundary", ["read", "auth", "request", "persist"])
def test_queue_flush_does_not_cross_account_boundaries(boundary):
    result = _run(f"const boundary={json.dumps(boundary)};\n" + r"""
const gate=deferred();
if(boundary==='read')readGate=gate;
if(boundary==='auth')authGate=gate;
if(boundary==='request')requestGate=gate;
if(boundary==='persist')writeGate=gate;
const second={...a,uid:'second'};store.set('queue:alice',[a,second]);
const pending=CalQueue.flush();await drain();
const before=requests.length;
assert.equal(before,['request','persist'].includes(boundary)?(boundary==='persist'?2:1):0,
  'the intended async boundary was not reached');
currentOwner='bob';gate.resolve();await pending;
assert.equal(requests.length,before,'old queue was sent after the account changed');
assert(requests.every(r=>r.owner==='alice'),'an appointment was sent as the wrong account');
assert.deepEqual(store.get('queue:bob'),[b],'flush replaced the new account pending writes');
assert.equal(S.queued,7,'old flush changed the new account pending badge');
assert.equal(notices.length,0,'old flush posted a notice for the new account');
assert.deepEqual(store.get('queue:alice'),boundary==='persist'?[]:[a,second],
  'an interrupted flush lost retryable appointments');
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_owner_add_replaces_an_older_pending_edit():
    result = _run(r"""
await CalQueue.add({...a,ics:'edited'});
assert.deepEqual(store.get('queue:alice'),[{...a,ics:'edited'}]);
assert.deepEqual(store.get('queue:bob'),[b]);assert.equal(S.queued,1);
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_flush_keeps_temporary_failures_and_reports_permanent_refusals():
    result = _run(r"""
const refused={op:'del',cal:'alice-private',uid:'gone'},retry={...a,uid:'retry'};
store.set('queue:alice',[a,refused,retry]);responses.push(200,403,503);
assert.equal(await CalQueue.flush(),1);
assert.equal(requests.length,3);assert.equal(requests[1].opts.method,'DELETE');
assert.deepEqual(store.get('queue:alice'),[retry]);assert.deepEqual(store.get('queue:bob'),[b]);
assert.equal(S.queued,1);assert.equal(notices.length,1);assert.match(notices[0],/refused/);
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_structured_api_error_keeps_certificate_details_and_http_status():
    result = _run(r"""
responses.push({status:400,detail:{error:'untrusted feed certificate',certificate:true}});
await assert.rejects(api('/api/calendar/subscribe'),error=>{
  assert.equal(error.message,'untrusted feed certificate');assert.equal(error.status,400);
  assert.deepEqual(error.detail,{error:'untrusted feed certificate',certificate:true});return true;
});
""")
    assert result.returncode == 0, result.stdout + result.stderr
