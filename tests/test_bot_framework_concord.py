"""A BOT CAN JOIN A CONCORD ROOM BY ITS INVITE LINK.

Asked for as "we need our bot framework to work with concord rooms. mayhe add field so it can join
the concord invite link?"

WHY THIS IS NOT JUST A CONFIG FIELD. A Concord message is not a Nostr note: the bundle is opened
from the invite (whose `#` fragment is the room's decryption secret), the control plane names the
channels, and every message is a CORD gift wrap published to the ROOM's own relays. All of that is
implemented once — in the `cord-protocol.js` / `cord-reader.js` the web client ships — so the bot
framework DRIVES that implementation through a node bridge instead of getting a Python port of it.
A second implementation of a wire format that other clients (Armada) also read is a second thing to
drift, and this repo has paid for that shape before.

THE BRIDGE OPENS NO SOCKETS. It is handed the events Python fetched and answers with events for
Python to publish, so relays, retries and rate limits stay in one place — the same split that keeps
`NOSTR_RELAYS` pointed at the local relay for every other bot.

THE INVITE IS A CREDENTIAL, not a setting. It is treated like `NOSTR_NSEC`: injected through the
process environment, never logged, and `Room.__repr__` prints the URL with its fragment elided.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "botframework"))
NODE = shutil.which("node")

ADMIN = (ROOT / "static/js/admin-bots.js").read_text(encoding="utf-8")
MANAGER = (ROOT / "app/services/bot_manager_service.py").read_text(encoding="utf-8")
BRIDGE = (ROOT / "botframework/concord_bridge.mjs").read_text(encoding="utf-8")


# ───────────────────────────────── the wiring ────────────────────────────────────────────────────

def test_the_invite_is_a_known_per_bot_config_key():
    """A key missing from this set is silently DROPPED from a bot's config on its next save — the
    same trap `nitter_feeds` fell into when it was removed."""
    assert "'concord_invite'" in ADMIN, (
        "concord_invite is not a known bot config key, so Admin -> Bots discards it on save")


def test_the_invite_reaches_the_bot_process():
    assert 'setif("concord_invite", "CONCORD_INVITE")' in MANAGER
    # Beside the identity it needs to speak, in the nostr listener's env — not the pleroma branch.
    nostr = MANAGER.split('elif platform == "nostr":', 1)[1][:1400]
    assert "CONCORD_INVITE" in nostr, "the invite is injected for the wrong platform"


def test_the_secret_half_of_the_invite_is_never_printed():
    import concord
    room = concord.Room("https://poster.place/invite/naddr1abc#s3cret", "nsec1x")
    assert "s3cret" not in repr(room), "the invite's decryption secret is printed in a repr"
    assert "naddr1abc" in repr(room), "…and the room is now unidentifiable in a log"


def test_a_link_without_its_fragment_is_refused_immediately():
    """The fragment IS the secret. Saying so beats a decrypt failure three calls later."""
    import concord
    with pytest.raises(concord.ConcordError) as e:
        concord.Room("https://poster.place/invite/naddr1abc", "nsec1x")
    assert "fragment" in str(e.value)


def test_a_bot_without_a_room_is_the_ordinary_case():
    import concord
    assert concord.from_env({}) is None, "a bot with no invite must not be an error"
    with pytest.raises(concord.ConcordError):
        concord.from_env({"CONCORD_INVITE": "https://x/invite/naddr1#s"})   # no identity to speak as


def test_the_bridge_keeps_stdout_for_the_protocol():
    """stdout IS the wire here. The CORD bundles log warnings ("Concord room refresh failed…"), and
    one of those in the stream would be parsed as a reply."""
    # The realm bootstrap lives in cord_realm.mjs — one definition, shared with the test fixture,
    # because a fixture with its own copy agrees with whatever gap the bridge has (that is how a
    # missing `URL` made every real invite link unopenable while the end-to-end test stayed green).
    realm = (ROOT / "botframework/cord_realm.mjs").read_text(encoding="utf-8")
    assert "globalThis.console = { log(){}, warn(){}, error(){} }" in realm
    assert '"NO_COLOR": "1"' in (ROOT / "botframework/concord.py").read_text(encoding="utf-8"), (
        "node colourises console output when FORCE_COLOR is set, and this protocol is line-based")


def test_the_bridge_never_opens_a_socket():
    """Relays, retries and rate limits belong in one place. A bridge that dialled relays itself
    would be a second network policy for the bots to disagree with."""
    realm = (ROOT / "botframework/cord_realm.mjs").read_text(encoding="utf-8")
    for banned in ("WebSocket", "net.connect", "fetch(", "http.request"):
        assert banned not in BRIDGE, f"the Concord bridge reaches the network itself ({banned})"
        assert banned not in realm, f"the CORD realm reaches the network itself ({banned})"


# ───────────────────────────────── RUN it against a real room ────────────────────────────────────

@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_bridge_opens_a_real_community_and_speaks_in_it():
    """END TO END against the shipped CORD implementation: mint a community, hand the bridge its
    bundle, read the channels back, and build a message wrap — the exact sequence a bot performs.

    A community is minted here rather than mocked because the only thing worth knowing is whether
    what the bridge produces is what the reader (and therefore Armada) accepts."""
    script = r"""
      import fs from 'node:fs'; import vm from 'node:vm';
      import { webcrypto } from 'node:crypto'; import { spawn } from 'node:child_process';
      function realm(){
        const c = vm.createContext({
          __enc:(s)=>Array.from(new TextEncoder().encode(String(s))),
          __dec:(a)=>new TextDecoder().decode(Uint8Array.from(a)),
          __rand:(n)=>Array.from(webcrypto.getRandomValues(new Uint8Array(n))),
          __uuid:()=>webcrypto.randomUUID(),
          __btoa:(s)=>Buffer.from(s,'binary').toString('base64'),
          __atob:(s)=>Buffer.from(s,'base64').toString('binary'),
          setTimeout, clearTimeout });
        vm.runInContext(`
          globalThis.TextEncoder=class{encode(s){return Uint8Array.from(__enc(s));}};
          globalThis.TextDecoder=class{decode(b){return __dec(Array.from(b??[]));}};
          globalThis.crypto={getRandomValues(a){a.set(__rand(a.length));return a;},randomUUID:()=>__uuid()};
          globalThis.btoa=(s)=>__btoa(String(s)); globalThis.atob=(s)=>__atob(String(s));
          globalThis.window=globalThis; globalThis.self=globalThis;
          globalThis.document={createElement:()=>({}),querySelector:()=>null};
          globalThis.location={origin:'https://poster.place'};`, c);
        return c;
      }
      const load=(c,f)=>vm.runInContext(fs.readFileSync(f,'utf8'),c,{filename:f});
      const nt=realm(); load(nt,'static/vendor/nostr/nostr.bundle.js');
      const cord=realm(); load(cord,'static/js/client/cord-protocol.js');
                          load(cord,'static/js/client/cord-reader.js');
      const NT=nt.NostrTools, into=(c)=>(v)=>vm.runInContext('('+JSON.stringify(v)+')',c);
      const toCord=into(cord), toNT=into(nt);
      const sk=NT.generateSecretKey(), pk=NT.getPublicKey(sk);
      const signEvent=(t)=>toCord(NT.finalizeEvent(toNT({kind:t.kind,
        created_at:t.created_at??Math.floor(Date.now()/1000),
        tags:t.tags||[],content:t.content??''}),sk));
      const opts=toCord({name:'bot room',icon:'',owner:pk,relays:['wss://relay.example'],
                         base:'https://poster.place'});
      opts.signEvent=signEvent;
      const made=await cord.PosterCord.createCommunity(opts);
      const bundle={community_id:made.communityId,owner:pk,owner_salt:made.secrets.ownerSalt,
        community_root:made.secrets.root,root_epoch:0,channels:[],
        relays:['wss://relay.example'],name:'bot room',creator_npub:pk};
      const wraps=made.events.filter(e=>e.kind===1059);
      const nsec=NT.nip19.nsecEncode(sk);

      const bridge=spawn('node',['botframework/concord_bridge.mjs'],{stdio:['pipe','pipe','inherit']});
      const say=(o)=>new Promise(res=>{
        bridge.stdout.once('data',d=>res(JSON.parse(String(d).trim().split('\n')[0])));
        bridge.stdin.write(JSON.stringify(o)+'\n');});
      const out={};
      const chans=await say({id:1,op:'inspect',bundle,controlWraps:wraps});
      out.inspect=chans.ok; out.channels=(chans.result?.channels||[]).map(c=>c.name);
      const cid=(chans.result?.channels||[])[0]?.id;
      const msg=await say({id:2,op:'say',bundle,controlWraps:wraps,channelId:cid,
                           text:'hello from a bot',nsec});
      out.say=msg.ok; out.wrapKind=msg.result?.wrap?.kind;
      const back=await say({id:3,op:'read',bundle,controlWraps:wraps,channelId:cid,
                            chatWraps:[msg.result.wrap]});
      out.read=back.ok; out.texts=(back.result?.messages||[]).map(m=>m.text);
      const bad=await say({id:4,op:'nope'});
      out.unknownOp=bad.ok;
      bridge.stdin.end();
      console.log('RESULT '+JSON.stringify(out));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"})
    assert done.returncode == 0, done.stderr[-2000:]
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT ")][-1]
    got = json.loads(line[len("RESULT "):])

    assert got["inspect"] is True, "the bridge could not read the community's control plane"
    assert got["channels"] == ["general"], got["channels"]
    assert got["say"] is True, "the bridge could not build a message for the room"
    assert got["wrapKind"] == 1059, "a Concord message must be published as a CORD gift wrap"
    assert got["read"] is True
    assert got["texts"] == ["hello from a bot"], (
        "the message the bridge built is not one the shipped reader can decrypt — Armada would not "
        "read it either: %r" % (got["texts"],))
    assert got["unknownOp"] is False, "an unknown op is answered as success"
