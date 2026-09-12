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


# ───────────────────── a saved room is a request to join it ─────────────────────

def test_a_bot_with_an_invite_is_spawned_with_exactly_the_modes_it_was_saved_with():
    """REPORTED TWICE, FROM OPPOSITE DIRECTIONS, AND BOTH REPORTS WERE RIGHT.

    First: "i don't see bot in room despite what the UI says". The row bore it out — `concord_invite`
    stored with its fragment intact, "Test join" answering 200, and `modes='--nostr'`. The bot held a
    community it never opened; nothing broken, nothing logged.

    The fix was to derive `--concord` from the invite at SPAWN, here. That worked, and produced:
    "In bots, concord can never be unchecked, wtf is this" — a decision re-derived on every start
    cannot be turned off by anybody, so the checkbox was decoration.

    The cause of both is a decision made where the operator cannot see it. The derivation is a
    DEFAULT now, applied once at the point a bot is created (the admin form ticks the box on screen;
    `bots._concord_default` does the same for the REST API and the migration seed), plus a one-shot
    backfill for rows that predate it. Spawn honours the row exactly.
    """
    from app.services.bot_manager_service import _cmd_for

    def modes(cfg, existing=("--nostr",)):
        cmd = _cmd_for({"platform": "nostr", "modes": list(existing), "config": cfg})
        return [a for a in cmd if a.startswith("--")]

    invite = {"concord_invite": "https://poster.place/invite/naddr1abc#secret"}
    assert modes({}) == ["--nostr"], "a bot with no room must not be given the listener"
    assert modes(invite) == ["--nostr"], (
        "spawn is deriving --concord from the invite again — that is what made the checkbox "
        "impossible to untick, because every restart undid it")
    assert modes(invite, existing=("--nostr", "--concord")) == ["--nostr", "--concord"], (
        "a bot saved WITH the listener must still be spawned with it")


def test_a_concord_bot_with_no_room_says_so(caplog):
    """A BOT THAT JOINS NOTHING MUST NOT LOOK HEALTHY — AND THE CHECK MUST READ THE REAL SHAPE.

    Measured: of the two bots carrying `--concord`, one had no invite. Its process was up, its
    checkbox ticked, its nsec valid, and it sat in no community with nothing to say why.

    THE FIRST VERSION OF THIS TEST GREPPED THE SOURCE and passed over a broken check. `bot_to_dict`
    FLATTENS the JSON config into the top level, so `bot_dict.get("config")` is always None: the
    warning fired about every Concord bot including a correctly configured one, and its older twin
    ("an invite but no listener") could never fire at all. A rule test that reads the code cannot
    see that; this one RUNS the function against the dict the manager actually passes.
    """
    import logging
    from app.services import bot_manager_service as mgr

    def warnings_for(**bot):
        bot.setdefault("platform", "nostr")
        bot.setdefault("name", "probe")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=mgr.logger.name):
            mgr._cmd_for(bot)
        return " ".join(r.getMessage() for r in caplog.records)

    invite = "https://poster.place/invite/naddr1abc#secret"

    # THE FLAT SHAPE, which is what `bot_to_dict` produces and the manager really uses.
    said = warnings_for(modes=["--nostr", "--concord"], concord_invite=invite)
    assert "NO community invite" not in said, (
        "a correctly configured Concord bot is being reported as having no room")

    said = warnings_for(modes=["--nostr", "--concord"])
    assert "NO community invite" in said, "a listener with no room says nothing"

    said = warnings_for(modes=["--nostr"], concord_invite=invite)
    assert "listener is switched OFF" in said, (
        "a bot holding a room it does not listen to says nothing — the original report")

    said = warnings_for(modes=["--nostr"])
    assert said.strip() == "", "a bot with neither is warned about anyway"

    # And the nested shape a raw row / API caller hands over must behave identically.
    said = warnings_for(modes=["--nostr", "--concord"], config={"concord_invite": invite})
    assert "NO community invite" not in said
    said = warnings_for(modes=["--nostr", "--concord"], config=json.dumps({"concord_invite": invite}))
    assert "NO community invite" not in said, "a JSON-string config is not parsed"


def test_the_form_warns_about_a_listener_with_no_room():
    js = (ROOT / "static/js/admin-bots.js").read_text()
    assert "bot_concord_noroom" in js
    assert "will not join any" in js
    # A VISIBLE note, not a tooltip: with no invite there is nothing on screen to hover.
    assert "note.textContent" in js and "note.hidden" in js


def test_creating_a_bot_with_an_invite_gives_it_the_listener():
    """The default lives where a row is BORN, so the callers with no form still work.

    The admin form ticks the box visibly; this is the same decision for the REST API and the
    first-start migration seed, which never open a form. It is a default and not a rule: modes that
    already mention `--concord` are returned untouched, and an UPDATE never calls this at all, so an
    operator's untick survives every later save and restart.
    """
    from app.routers.bots import _concord_default

    invite = {"concord_invite": "https://poster.place/invite/naddr1abc#secret"}
    assert _concord_default("--nostr", invite) == "--nostr,--concord"
    assert _concord_default("", invite) == "--concord"
    # No room saved: nothing is added, whatever the config looks like.
    assert _concord_default("--nostr", {}) == "--nostr"
    assert _concord_default("--nostr", {"concord_invite": "   "}) == "--nostr"
    # Already decided: returned verbatim, never duplicated.
    assert _concord_default("--nostr,--concord", invite) == "--nostr,--concord"


def test_the_backfill_runs_once_and_never_re_derives(tmp_path):
    """A backfill that ran on every boot WOULD BE the spawn-time forcing, one layer down.

    An operator who unticks Concord on a bot that still holds an invite must find it unticked after
    the next restart, so the marker is written even when the sweep changed nothing.
    """
    import inspect as _inspect
    from app import database

    src = _inspect.getsource(database._backfill_concord_modes)
    assert 'settings_store.get("bots_concord_mode_backfilled"' in src, (
        "the backfill no longer checks its marker — it would re-add --concord on every start, "
        "which is exactly the behaviour it replaced")
    assert 'settings_store.set("bots_concord_mode_backfilled", "1")' in src
    # The marker must be set OUTSIDE the `if changed:` branch, or a run that matched nothing repeats
    # for ever and reverts the first untick that happens after it.
    set_at = src.index('settings_store.set("bots_concord_mode_backfilled"')
    changed_at = src.index("if changed:")
    assert src[changed_at:set_at].count("\n        ") >= 1
    assert not src[set_at - 200:set_at].rstrip().endswith("changed += 1"), (
        "the marker looks conditional on having changed something")


def test_the_listener_says_so_when_it_has_no_room():
    """The other half of the same silence: a bot asked to run --concord with no invite must not sit
    in a loop doing nothing. It says it once and idles, so the log answers "why is nothing
    happening" without anybody reading this file."""
    main = (ROOT / "botframework/main.py").read_text(encoding="utf-8")
    block = main[main.index("if args.concord:"):]
    block = block[:block.index("# Every game referee")]
    assert "CONCORD_INVITE" in block and "Not listening" in block, (
        "a Concord bot with no invite starts a silent thread instead of saying why it cannot work")
