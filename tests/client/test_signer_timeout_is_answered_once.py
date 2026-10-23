"""A SIGNATURE THE USER IS WAITING ON IS ANSWERED AFTER ONE CEILING, NOT TWO.

Measured in the browser (tests/client/test_signer_flows_full_app.py's set-up: a real bunker pairing between two
devices over this node's relay, then the signer device closed mid-post): "waiting for your signer…" at 4 s, then
nothing at all until "action failed: signer request timed out" at 241 s. The interactive ceiling is 120 s on purpose
(approving on a phone is a human act) and the re-send ladder inside `_rpc` already re-publishes the request four
times and revives the socket -- but `_send` then retried the WHOLE request once more on any non-refusal error,
the timeout included, so a signer that had gone away cost two ceilings.

This runs the SHIPPED `Nip46._send` / `_pump` against a stub `_rpc` (the transport is not the question): a
sign_event that times out is asked ONCE and the caller hears the timeout; a bulk decrypt that times out is still
retried (its 45 s ceiling exists to hand the slot back to that retry); a transport loss is still retried.
`PC_SIGNER_SRC` runs another copy of signer.js (fail-first).
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(not NODE, reason='node unavailable')

JS = r'''
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
function slice(start, end){ const i = src.indexOf(start); if(i < 0) throw new Error('missing ' + start);
  const j = src.indexOf(end, i); return src.slice(i, j + end.length); }
const members = [
  slice('    _cap: 12, _capP: 2,', '_queueP: [],'),
  slice('    _pump(){', '\n    },'),
  slice('    _PRIORITY: new Set(', ']),'),
  slice('    _send(method, params, opts){', '\n    },'),
].join('\n');
const ctx = { setTimeout, Promise, Error, String, Set, console };
vm.createContext(ctx);
vm.runInContext('globalThis.N = {\n' + members + '\n};', ctx);
const N = ctx.N;
async function run(method, err){
  let calls = 0;
  N._rpc = async () => { calls++; throw new Error(err); };
  const t0 = Date.now();
  let msg = '';
  try{ await N._send(method, []); }catch(e){ msg = String(e && e.message); }
  return { method, err, calls, msg, ms: Date.now() - t0 };
}
(async () => {
  const out = [];
  out.push(await run('sign_event', 'signer request timed out'));
  out.push(await run('nip44_decrypt', 'signer request timed out'));
  out.push(await run('sign_event', 'lost the signer relay — retrying'));
  out.push(await run('sign_event', 'rejected by user'));
  console.log(JSON.stringify(out));
})();
'''


def test_an_interactive_timeout_is_one_ceiling_and_the_rest_still_retry():
    src = os.environ.get('PC_SIGNER_SRC') or str(ROOT / 'static/js/client/signer.js')
    r = subprocess.run([NODE, '-e', JS, src], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    got = {(x['method'], x['err']): x for x in json.loads(r.stdout.strip().splitlines()[-1])}
    timed = got[('sign_event', 'signer request timed out')]
    assert timed['calls'] == 1, ('a sign_event that ran its ceiling was asked again', timed)
    assert 'timed out' in timed['msg'] and 'signer' in timed['msg'], timed
    assert got[('nip44_decrypt', 'signer request timed out')]['calls'] == 2, 'the bulk lane lost its retry'
    assert got[('sign_event', 'lost the signer relay — retrying')]['calls'] == 2, 'a transport loss is no longer retried'
    assert got[('sign_event', 'rejected by user')]['calls'] == 1, 'a refusal was retried'
