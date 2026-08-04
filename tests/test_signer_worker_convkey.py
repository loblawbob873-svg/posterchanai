"""The NIP-44 conversation key is derived once per peer, not once per message.

Notes encrypts every note to the user's OWN key, so opening a library re-derives ONE conversation
key hundreds of times. That ECDH is ~92% of the cost of opening the screen (measured below), which
is why a cache-first, fully-local notebook felt like it was waiting on the network.

The cache is per-login: reusing a key derived under a previous account would decrypt the wrong
person's data, so it must be cleared on setKey and clearKey.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "static", "js", "client", "signer-worker.js")
BUNDLE = os.path.join(ROOT, "static", "vendor", "nostr", "nostr.bundle.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(js):
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr or r.stdout
    return r.stdout


def _load_worker():
    """Run the real worker file under node with a fake `self`, and hand back its message port."""
    return """
      const fs = require('fs');
      const NT = (new Function(fs.readFileSync(%r,'utf8') + ';return NostrTools;'))();
      /* Count the ECDHs by handing the worker a VIEW of nostr-tools, not by patching the bundle:
       * esbuild exports the namespaces as getters, so a plain assignment onto NT.nip44 is silently
       * dropped and every count comes back zero — a test that passes whatever the worker does. */
      let derived = 0;
      const realCk = NT.nip44.getConversationKey;
      const nip44 = Object.create(NT.nip44);
      // defineProperty, not assignment: the inherited name is a getter with no setter, so `=` is
      // silently discarded in sloppy mode and every count comes back zero.
      Object.defineProperty(nip44, 'getConversationKey',
                            { value: (sk, pk) => { derived++; return realCk(sk, pk); } });
      const NTv = Object.create(NT);
      Object.defineProperty(NTv, 'nip44', { value: nip44 });
      const replies = [];
      global.self = { NostrTools: NTv, postMessage: (m) => replies.push(m), onmessage: null };
      global.importScripts = () => {};
      (new Function(fs.readFileSync(%r,'utf8')))();
      const call = async (op, args) => {
        const n = replies.length;
        await self.onmessage({ data: { id: n, op, args } });
        const r = replies[replies.length - 1];
        if (!r.ok) throw new Error(r.error);
        return r.data;
      };
      const counted = () => derived;
    """ % (BUNDLE, WORKER)


def test_one_derivation_for_many_messages_to_the_same_peer():
    out = _run(_load_worker() + """
      (async () => {
        const sk = NT.generateSecretKey(), skHex = Buffer.from(sk).toString('hex');
        const pk = NT.getPublicKey(sk);
        await call('setKey', { sk: skHex });
        const before = counted();
        const cts = [];
        for (let i = 0; i < 50; i++) cts.push((await call('nip44enc', { peer: pk, text: 'n' + i })).ct);
        for (let i = 0; i < 50; i++) {
          const pt = (await call('nip44dec', { peer: pk, ct: cts[i] })).pt;
          if (pt !== 'n' + i) throw new Error('round trip broke at ' + i);
        }
        console.log(JSON.stringify({ derivations: counted() - before }));
      })().catch(e => { console.error(e); process.exit(1); });
    """)
    n = int(re.search(r'"derivations":(\d+)', out).group(1))
    assert n == 1, "100 self-encrypted messages should derive the conversation key once, got %d" % n


def test_a_second_peer_gets_its_own_key():
    out = _run(_load_worker() + """
      (async () => {
        const sk = NT.generateSecretKey();
        const a = NT.getPublicKey(sk);
        const b = NT.getPublicKey(NT.generateSecretKey());
        await call('setKey', { sk: Buffer.from(sk).toString('hex') });
        const before = counted();
        await call('nip44enc', { peer: a, text: 'x' });
        await call('nip44enc', { peer: b, text: 'x' });
        await call('nip44enc', { peer: a, text: 'y' });
        console.log(JSON.stringify({ derivations: counted() - before }));
      })().catch(e => { console.error(e); process.exit(1); });
    """)
    assert int(re.search(r'"derivations":(\d+)', out).group(1)) == 2


def test_the_cache_does_not_survive_a_key_change():
    """A key cached under the previous login would decrypt the wrong account's messages."""
    out = _run(_load_worker() + """
      (async () => {
        const sk1 = NT.generateSecretKey(), sk2 = NT.generateSecretKey();
        const peer = NT.getPublicKey(NT.generateSecretKey());
        await call('setKey', { sk: Buffer.from(sk1).toString('hex') });
        const ct1 = (await call('nip44enc', { peer, text: 'first account' })).ct;
        await call('setKey', { sk: Buffer.from(sk2).toString('hex') });
        // Same peer, different identity: the cached key must NOT be reused, so this must fail
        // rather than silently produce garbage or (worse) succeed.
        let leaked = false;
        try { await call('nip44dec', { peer, ct: ct1 }); leaked = true; } catch (_) {}
        console.log(JSON.stringify({ leaked }));
      })().catch(e => { console.error(e); process.exit(1); });
    """)
    assert '"leaked":false' in out, "a conversation key outlived the login it belonged to"

    out = _run(_load_worker() + """
      (async () => {
        const sk = NT.generateSecretKey();
        const peer = NT.getPublicKey(sk);
        await call('setKey', { sk: Buffer.from(sk).toString('hex') });
        await call('nip44enc', { peer, text: 'x' });
        await call('clearKey', {});
        await call('setKey', { sk: Buffer.from(sk).toString('hex') });
        const before = counted();
        await call('nip44enc', { peer, text: 'x' });
        console.log(JSON.stringify({ rederived: counted() - before }));
      })().catch(e => { console.error(e); process.exit(1); });
    """)
    assert '"rederived":1' in out, "clearKey must empty the conversation-key cache"


def test_the_derivation_really_is_the_expensive_part():
    """The measurement the cache exists for — asserted loosely so it documents, not flakes."""
    out = _run("""
      const fs = require('fs');
      const NT = (new Function(fs.readFileSync(%r,'utf8') + ';return NostrTools;'))();
      const sk = NT.generateSecretKey(), pk = NT.getPublicKey(sk);
      const ck = NT.nip44.getConversationKey(sk, pk);
      const cts = [];
      for (let i = 0; i < 100; i++) cts.push(NT.nip44.encrypt(JSON.stringify({t:'note '+i, b:'x'.repeat(2000)}), ck));
      let t = process.hrtime.bigint();
      for (let i = 0; i < 100; i++) NT.nip44.getConversationKey(sk, pk);
      const ecdh = Number(process.hrtime.bigint() - t);
      t = process.hrtime.bigint();
      for (const c of cts) NT.nip44.decrypt(c, ck);
      console.log(JSON.stringify({ ratio: ecdh / Number(process.hrtime.bigint() - t) }));
    """ % BUNDLE)
    ratio = float(re.search(r'"ratio":([\d.]+)', out).group(1))
    assert ratio > 2, ("deriving the key was only %.1fx the cost of decrypting with it; if that is "
                       "genuinely true now, the cache is no longer worth its complexity" % ratio)
