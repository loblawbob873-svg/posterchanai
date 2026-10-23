"""A NODE WHOSE RELAY STORE AN OLDER BUILD WROTE STARTS ON TODAY'S BUILD AND READS ALL OF IT.

A node's settings, every account's private documents and every public event live in its relay's PosterChanDB store.
A deploy restarts the relay on new code over the SAME directory, so a change to how the store lays out its files, how
a settings document is encrypted or how a private document is read would make the node forget its own configuration
and its members' data -- once, at the upgrade, which no other test exercises.

The old data is written by the OLD CODE, in its own process (tests/old_builds.py extracts `app/` of the old commit):
the old relay (RelayServer over PcdbRelayStore) on a fresh directory; the old settings writer
(`settings_store.write_through`, operator-signed `pcai:setting:` documents); the old `nostr_store.put_doc` for an
account's drafts document under its storage key; and public events published over a socket like any client (a
profile, two posts, one of them deleted with a kind 5, a follow list replaced by a newer one). The old relay is shut
down. Today's code then opens the same directory in a fresh process -- the relay, `settings_store.hydrate_from_db`
over the loopback socket, `nostr_store.get_doc(strict=True)` -- and every value must come back, the deleted post
must stay deleted and the older follow list must stay replaced.

Old builds: the last one before HEAD's day, and the FIRST build that stored the relay on PosterChanDB (older nodes ran
the relay on Postgres; moving off it is scripts/posterchandb_promote.py's job, and needs a Postgres this test does not
have). Skips on a shallow checkout (the desktop CI), which holds no old build.

Fail-first: PC_UPGRADE_TODAY_TREE=<a copy of the tree with a change> reads with that copy instead of this tree.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests import old_builds

ROOT = old_builds.ROOT
PY = sys.executable
PCDB = 'app/services/nostr_relay/pcdb_store.py'


def _builds():
    why = old_builds.unavailable()
    if why:
        return {'': why}
    at = old_builds.head_time()
    out = {'yesterday': old_builds.before(at.replace(hour=0, minute=0, second=0, microsecond=0), needs=PCDB),
           'first_posterchandb': old_builds.first_commit_with(PCDB)}
    if not all(out.values()):
        return {'': 'the history has no PosterChanDB relay build: %r' % out}
    return out


BUILDS = _builds()

SETTINGS = {'site_name': 'Old Build Node', 'activitypub_enabled': 'false', 'registration_message': 'Ask first ✓'}
DRAFTS = [{'id': 'd-old-1', 'ts': 1760000000, 'text': 'a draft the old node kept'},
          {'id': 'd-old-2', 'ts': 1760000100, 'del': True}]

# Run with the OLD tree first on sys.path, in its own process: the old relay over a fresh directory, written by the
# old producers, then shut down cleanly (as a deploy's restart does).
WRITER = r'''
import asyncio, json, os, sys
root, store_dir = sys.argv[1], sys.argv[2]
settings, drafts = json.loads(sys.argv[3]), json.loads(sys.argv[4])
sys.path.insert(0, root)
import websockets
from app.services.nostr_relay.pcdb_store import PcdbRelayStore, init_new
from app.services.nostr_relay.server import RelayServer
from app.services.nostr_relay.wot import WotGate
from app.services import nostr_store, settings_store
from app.services.nostr.event import build_event

OP, USER, STORAGE = bytes([41]) * 32, bytes([42]) * 32, bytes([43]) * 32

async def publish(port, ev):
    async with websockets.connect('ws://127.0.0.1:%d' % port, max_size=None) as ws:
        await ws.send(json.dumps(['EVENT', ev]))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m[0] == 'OK' and m[1] == ev['id']:
                assert m[2], m
                return

async def main():
    init_new(store_dir)
    st = PcdbRelayStore(store_dir, max_events=0, retention_days=0, maintenance=False)
    st.open(asyncio.get_running_loop())
    relay = RelayServer(st, WotGate(), {'wot_enabled': False, 'block_json': False, 'max_subs_per_conn': 64})
    server = await websockets.serve(relay.handle, '127.0.0.1', 0, max_size=None, process_request=relay.process_request)
    port = server.sockets[0].getsockname()[1]
    settings_store._OP_SK = OP
    settings_store._port = lambda db=None: port
    wrote = await settings_store.write_through(None, settings)
    assert wrote == len(settings), wrote
    assert await nostr_store.put_doc(port, STORAGE, 'pcai:drafts', {'drafts': drafts})
    import time
    now = int(time.time())
    kept = build_event(USER, 1, 'a post the old node kept', [], now - 50)
    gone = build_event(USER, 1, 'a post deleted on the old node', [], now - 40)
    for ev in (build_event(USER, 0, json.dumps({'name': 'Old Timer'}), [], now - 60), kept, gone,
               build_event(USER, 5, '', [['e', gone['id']]], now - 30),
               build_event(USER, 3, '', [['p', '1' * 64], ['p', '2' * 64]], now - 20),
               build_event(USER, 3, '', [['p', '1' * 64], ['p', '2' * 64], ['p', '3' * 64]], now - 10)):
        await publish(port, ev)
    server.close()
    await server.wait_closed()
    st.close()
    print(json.dumps({'kept': kept['id'], 'gone': gone['id']}))

asyncio.run(main())
'''

# Today's tree, a fresh process: the relay over the SAME directory, and today's readers.
READER = r'''
import asyncio, json, sys
root, store_dir = sys.argv[1], sys.argv[2]
keys = json.loads(sys.argv[3])
sys.path.insert(0, root)
import websockets
from app.services.nostr_relay.pcdb_store import PcdbRelayStore
from app.services.nostr_relay.server import RelayServer
from app.services.nostr_relay.wot import WotGate
from app.services import nostr_store, settings_store
from app.services.nostr import nostr_service

OP, USER, STORAGE = bytes([41]) * 32, bytes([42]) * 32, bytes([43]) * 32

async def query(port, flt):
    out = []
    async with websockets.connect('ws://127.0.0.1:%d' % port, max_size=None) as ws:
        await ws.send(json.dumps(['REQ', 'q', flt]))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m[0] == 'EVENT':
                out.append(m[2])
            elif m[0] in ('EOSE', 'CLOSED'):
                return out

async def main():
    loop = asyncio.get_running_loop()
    st = PcdbRelayStore(store_dir, max_events=0, retention_days=0, maintenance=False)
    st.open(loop)
    relay = RelayServer(st, WotGate(), {'wot_enabled': False, 'block_json': False, 'max_subs_per_conn': 64})
    server = await websockets.serve(relay.handle, '127.0.0.1', 0, max_size=None, process_request=relay.process_request)
    port = server.sockets[0].getsockname()[1]
    settings_store._operator_seckey = lambda db=None: OP
    settings_store._port = lambda db=None: port
    hydrated = await loop.run_in_executor(None, settings_store.hydrate_from_db, None)
    user = nostr_service.derive_pubkey(USER)
    user = user if isinstance(user, str) else user.hex()
    drafts = await nostr_store.get_doc(port, 'pcai:drafts', seckey=STORAGE, strict=True)
    notes = await query(port, {'authors': [user], 'kinds': [1]})
    k3 = await query(port, {'authors': [user], 'kinds': [3]})
    k0 = await query(port, {'authors': [user], 'kinds': [0]})
    server.close()
    await server.wait_closed()
    st.close()
    print(json.dumps({'hydrated': hydrated, 'settings': {k: settings_store.get(k) for k in keys},
                      'drafts': drafts, 'notes': sorted(e['id'] for e in notes),
                      'follows': [sorted(t[1] for t in e['tags'] if t[0] == 'p') for e in k3],
                      'profile': [json.loads(e['content']).get('name') for e in k0]}))

asyncio.run(main())
'''


def _run(script, cwd, *args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env.pop('PYTHONPATH', None)
    r = subprocess.run([PY, '-c', script, str(cwd), *args], cwd=str(cwd), capture_output=True, text=True,
                       timeout=300, env=env)
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-4000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif('' in BUILDS, reason=BUILDS.get('', ''))
@pytest.mark.parametrize('label', [k for k in BUILDS if k] or ['none'])
def test_todays_node_reads_the_relay_store_an_older_build_wrote(label):
    sha = BUILDS[label]
    tmp = tempfile.mkdtemp(prefix='pc-upgrade-node-')
    try:
        old = old_builds.extract(sha, Path(tmp) / 'old', paths=('app',))
        store = Path(tmp) / 'relay'
        wrote = _run(WRITER, old, str(store), json.dumps(SETTINGS), json.dumps(DRAFTS))
        got = _run(READER, os.environ.get('PC_UPGRADE_TODAY_TREE') or ROOT, str(store), json.dumps(sorted(SETTINGS)))
        where = '%s build %s' % (label, sha[:9])
        assert got['settings'] == SETTINGS, (where, 'settings the old node saved, read by today\'s', got['settings'])
        assert got['hydrated'] >= len(SETTINGS), (where, got['hydrated'])
        assert (got['drafts'] or {}).get('drafts') == DRAFTS, (where, 'the drafts document', got['drafts'])
        assert got['notes'] == [wrote['kept']], (where, 'the kept post and NOT the deleted one', got['notes'], wrote)
        assert got['follows'] == [['1' * 64, '2' * 64, '3' * 64]], (where, 'only the NEWER follow list', got['follows'])
        assert got['profile'] == ['Old Timer'], (where, got['profile'])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
