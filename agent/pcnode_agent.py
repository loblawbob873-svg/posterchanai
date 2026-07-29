#!/usr/bin/env python3
"""
PosterChan node agent — a lightweight standalone worker.

Runs on router.lan or any machine that CAN'T run the full PosterChanAI app. It:
  * generates a Nostr keypair on first run and prints its npub (paste it into
    Admin → Nodes → Worker nodes AND the trusted list on the controller),
  * connects to a relay and listens for encrypted NIP-90 command events (kind 5300)
    p-tagged to its npub, FROM A TRUSTED CONTROLLER NPUB ONLY (Nostr signature = auth),
  * runs each command LOCALLY, ONE AT A TIME (queued — a burst can't overload the box),
  * publishes an encrypted result (kind 6300) back to the controller.

Protocol: see docs/NODE_AGENT_NOSTR.md (must match the app worker exactly).
Deps: cryptography, websockets. Nostr core is vendored in ./nostr (no PosterChanAI install).

  python3 pcnode_agent.py --relay wss://poster.place/relay --trust npub1yourcontroller…
"""
import os
import sys
import json
import time
import asyncio
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # find the vendored ./nostr
from nostr import bech32, bip340, nip44, event as nevent  # noqa: E402

log = logging.getLogger("pcnode-agent")

REQ_KIND = 5300          # controller → worker (command)
RES_KIND = 6300          # worker → controller (result)
MAX_OUTPUT = 200_000     # cap result output so a runaway command can't blow the event
DEFAULT_STEP_TIMEOUT = 900   # seconds per command (0 = none)


# ---- identity -------------------------------------------------------------------------------------
def load_or_create_key(path: str) -> bytes:
    """Load the 32-byte secret key from `path`, or generate + persist one (0600) on first run."""
    if os.path.exists(path):
        with open(path) as f:
            return bytes.fromhex(f.read().strip())
    sk = os.urandom(32)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(sk.hex())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return sk


def npub(pubkey_hex: str) -> str:
    return bech32.encode("npub", bytes.fromhex(pubkey_hex))


def decode_npub(s: str) -> str | None:
    """npub1… (or 64-hex) → 64-char hex pubkey, or None."""
    s = s.strip()
    if not s:
        return None
    if s.startswith("npub1"):
        raw = bech32.decode("npub", s)
        return raw.hex() if raw else None
    if len(s) == 64:
        try:
            bytes.fromhex(s)
            return s.lower()
        except ValueError:
            return None
    return None


# ---- command execution (serialized by a single worker task) --------------------------------------
async def run_shell(command: str, timeout: int) -> dict:
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout or None)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"status": "error", "summary": f"timed out after {timeout}s", "output": "", "exit": None}
    text = (out or b"").decode("utf-8", "replace")[-MAX_OUTPUT:]
    return {"status": "done" if proc.returncode == 0 else "error",
            "summary": f"exit {proc.returncode}", "output": text, "exit": proc.returncode}


async def execute(params: dict, cfg) -> dict:
    mode = (params.get("mode") or "shell").lower()
    if mode == "shell":
        cmd = params.get("command") or ""
        if not cmd:
            return {"status": "error", "summary": "empty command", "output": "", "exit": None}
        return await run_shell(cmd, cfg.step_timeout)
    if mode == "agent":
        # The lightweight agent has no local LLM — the agentic loop runs on full app-workers.
        return {"status": "error",
                "summary": "This worker runs shell only (no local LLM for 'agent' mode). Use mode 'shell'.",
                "output": "", "exit": None}
    return {"status": "error", "summary": f"unknown mode '{mode}'", "output": "", "exit": None}


# ---- the agent ------------------------------------------------------------------------------------
class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sk = load_or_create_key(cfg.key_path)
        self.pk = bip340.pubkey_from_seckey(self.sk).hex()
        self.npub = npub(self.pk)
        self.trusted = set(filter(None, (decode_npub(t) for t in cfg.trust)))
        import re as _re
        _raw = cfg.relay if isinstance(cfg.relay, list) else [cfg.relay]
        self.relays = [u for entry in _raw for u in _re.split(r"[,\s]+", (entry or "").strip()) if u] \
            or ["wss://poster.place/relay"]
        self.queue: asyncio.Queue = asyncio.Queue()   # holds (event, ws) so the reply goes back the way it came
        self.seen: set = set()          # dedup by event id (shared across relays)

    def _decrypt(self, author_hex: str, content: str) -> dict | None:
        try:
            return json.loads(nip44.decrypt_from(self.sk, bytes.fromhex(author_hex), content))
        except Exception as e:
            log.warning("decrypt failed from %s: %s", author_hex[:12], e)
            return None

    def _result_event(self, author_hex: str, req_id: str, jid, result: dict) -> dict:
        payload = json.dumps({"id": jid, **result})
        enc = nip44.encrypt_to(self.sk, bytes.fromhex(author_hex), payload)
        return nevent.build_event(self.sk, RES_KIND, enc,
                                  tags=[["p", author_hex], ["e", req_id]])

    async def _worker(self):
        """Single consumer → ONE job at a time (queue prevents overload). Each item is (event, ws):
        the result is published back on the SAME relay the request arrived on, which is where the
        controller awaits it (a controller only ever sees its own relay)."""
        while True:
            ev, ws = await self.queue.get()
            author = ev["pubkey"]
            params = self._decrypt(author, ev["content"]) or {}
            jid = params.get("id") or ev["id"][:8]
            log.info("▶ job %s from %s: mode=%s", jid, npub(author)[:16], params.get("mode"))
            try:
                result = await execute(params, self.cfg)
            except Exception as e:
                result = {"status": "error", "summary": f"agent error: {e}", "output": "", "exit": None}
            log.info("✔ job %s → %s", jid, result.get("status"))
            try:
                await ws.send(json.dumps(["EVENT", self._result_event(author, ev["id"], jid, result)]))
            except Exception as e:
                log.warning("publish result failed for %s: %s", jid, e)
            self.queue.task_done()

    def _accept(self, ev: dict) -> bool:
        if ev.get("kind") != REQ_KIND or ev.get("id") in self.seen:
            return False
        if not nevent.verify_event(ev):          # signature must be valid
            log.warning("bad signature, dropped %s", ev.get("id", "")[:12])
            return False
        if ev.get("pubkey") not in self.trusted:  # WHITELIST ONLY
            log.warning("untrusted npub %s — dropped", npub(ev.get("pubkey", "0" * 64))[:16])
            return False
        return True

    async def _listen(self, relay: str):
        """Connect to ONE relay, subscribe for our command events, and enqueue accepted ones with this
        relay's ws so the reply goes back here. Reconnects forever. Runs one per configured relay so a
        keyless worker can be commanded by MULTIPLE controllers (each publishes only to its own relay)."""
        import websockets
        while True:
            try:
                async with websockets.connect(relay, max_size=2 ** 22, ping_interval=30) as ws:
                    sub = "pcnode-" + os.urandom(4).hex()
                    await ws.send(json.dumps(["REQ", sub, {"kinds": [REQ_KIND], "#p": [self.pk],
                                                            "since": int(time.time()) - 5}]))
                    log.info("connected to %s, listening…", relay)
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        if m[0] == "EVENT" and len(m) >= 3:
                            ev = m[2]
                            if self._accept(ev):
                                self.seen.add(ev["id"])
                                await self.queue.put((ev, ws))
            except Exception as e:
                log.warning("relay %s connection lost (%s); reconnecting in 5s…", relay, e)
                await asyncio.sleep(5)

    async def run(self):
        print(f"\n  pcnode agent — this worker's npub:\n\n    {self.npub}\n")
        print(f"  Add it as a Worker node AND trust your controller npub(s): {', '.join(npub(t) for t in self.trusted) or '(none set — use --trust)'}")
        print(f"  Relays: {', '.join(self.relays)}\n", flush=True)
        if not self.trusted:
            log.warning("No --trust npubs set: this worker will accept NOTHING until you add one.")
        try:
            import websockets  # noqa: F401
        except ImportError:
            sys.exit("Missing dependency: pip install websockets")
        asyncio.create_task(self._worker())
        await asyncio.gather(*(self._listen(r) for r in self.relays))


def main():
    ap = argparse.ArgumentParser(description="PosterChan standalone node agent")
    ap.add_argument("--relay", default=os.environ.get("PCNODE_RELAY", "wss://poster.place/relay"),
                    help="relay URL(s) — repeatable, or one arg with comma/space-separated URLs. Connect to "
                         "EACH controller's relay so any of them can command this worker.", action="append")
    ap.add_argument("--trust", action="append", default=(os.environ.get("PCNODE_TRUST", "").split() or []),
                    help="controller npub allowed to command this worker (repeatable)")
    ap.add_argument("--data-dir", default=os.environ.get("PCNODE_DATA", os.path.expanduser("~/.pcnode-agent")))
    ap.add_argument("--step-timeout", type=int, default=int(os.environ.get("PCNODE_STEP_TIMEOUT", DEFAULT_STEP_TIMEOUT)))
    ap.add_argument("--print-npub", action="store_true", help="print this worker's npub and exit")
    cfg = ap.parse_args()
    cfg.key_path = os.path.join(cfg.data_dir, "agent.key")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = Agent(cfg)
    if cfg.print_npub:
        print(agent.npub)
        return
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
