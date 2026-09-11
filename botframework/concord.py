"""Concord rooms for the bot framework.

A bot joins a room by its INVITE URL — the same link a person pastes into the client — and the `#`
fragment of that URL is the room's decryption secret. So `CONCORD_INVITE` is a credential, handled
like `NOSTR_NSEC`: it is never logged, and `__repr__` here refuses to print it.

WHY A NODE BRIDGE AND NOT A PYTHON PORT. A Concord message is not a Nostr note: the bundle is opened
from the invite, the control plane names the channels, and every message is a CORD gift wrap on the
ROOM's own relays. All of that is implemented once, in the `cord-protocol.js` / `cord-reader.js` the
web client ships. Porting it would create a second implementation of a wire format that other
clients (Armada) also read — a thing to drift. `concord_bridge.mjs` drives the shipped code; node
does the crypto, this module does the relays and the bot's logic.

The bridge is PURE: it never opens a socket. It is handed the events this module fetched and answers
with events for this module to publish, so relays, retries and rate limits stay in one place.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_BRIDGE = _ROOT / "concord_bridge.mjs"


class ConcordError(RuntimeError):
    pass


class Bridge:
    """One long-lived node process, spoken to in JSON lines.

    Long-lived because loading the two CORD bundles costs real time, and a bot answers messages in a
    loop. One lock: the protocol is request/response on a single pipe, so two callers interleaving
    would read each other's answers.
    """

    def __init__(self, node: str = "node") -> None:
        self._node = node
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def _start(self) -> subprocess.Popen:
        if self._proc and self._proc.poll() is None:
            return self._proc
        if not _BRIDGE.is_file():
            raise ConcordError(f"the Concord bridge is missing: {_BRIDGE}")
        # NO_COLOR/FORCE_COLOR: stdout IS the protocol here, and node colourises console output when
        # FORCE_COLOR is set in the environment — which a terminal or CI runner may well do.
        env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}
        self._proc = subprocess.Popen(
            [self._node, str(_BRIDGE)], cwd=str(_ROOT.parent), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )
        return self._proc

    def call(self, op: str, **args: Any) -> Any:
        with self._lock:
            proc = self._start()
            self._seq += 1
            req = {"id": self._seq, "op": op, **args}
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (BrokenPipeError, ValueError) as exc:
                self._proc = None
                raise ConcordError(f"the Concord bridge stopped: {exc}") from exc
            if not line:
                self._proc = None
                raise ConcordError("the Concord bridge closed without answering")
            answer = json.loads(line)
            if not answer.get("ok"):
                raise ConcordError(str(answer.get("error") or "unknown bridge error"))
            return answer.get("result")

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
            self._proc = None


class Room:
    """A Concord room a bot has joined, addressed by its invite URL."""

    def __init__(self, invite: str, nsec: str, bridge: Bridge | None = None) -> None:
        if not invite or "#" not in invite:
            # The fragment IS the secret; a URL without one cannot open anything, and saying so is
            # better than a decrypt failure three calls later.
            raise ConcordError("that is not a Concord invite — the link must keep its # fragment")
        self.invite = invite
        self._nsec = nsec
        self.bridge = bridge or Bridge()
        self.bundle: dict | None = None
        self.controls: list = []

    def __repr__(self) -> str:          # never print the secret half of the invite
        return f"<Room {self.invite.split('#', 1)[0]}#… joined={self.bundle is not None}>"

    def bootstrap_relays(self) -> list[str]:
        d = self.bridge.call("inviteDetails", url=self.invite)
        return list(d.get("bootstrapRelays") or [])

    def link_signer(self) -> str:
        return str(self.bridge.call("inviteDetails", url=self.invite).get("linkSigner") or "")

    def open(self, bundle_events: list) -> dict:
        """Open the invite against the kind-33301 events fetched from its bootstrap relays."""
        opened = self.bridge.call("openInvite", url=self.invite, events=bundle_events)
        self.bundle = opened["bundle"]
        return opened

    def inspect(self, control_wraps: list | None = None) -> dict:
        """Everything the bundle can see given these control wraps.

        Called TWICE by a reader, and the first call is the odd one: with no wraps at all the only
        useful field is `controlPubkeys`, which is what you must query the control stream by. There
        is no way to know those authors without asking the bundle first, so the seed pass is not a
        wasted call — it is how the chicken-and-egg is broken, and it is what the web client does.
        """
        if self.bundle is None:
            raise ConcordError("open() the invite before inspecting it")
        if control_wraps is not None:
            self.controls = control_wraps
        return self.bridge.call("inspect", bundle=self.bundle, controlWraps=self.controls)

    def channels(self, control_wraps: list | None = None) -> list:
        return self.inspect(control_wraps)["channels"]

    def read(self, channel_id: str, chat_wraps: list) -> dict:
        if self.bundle is None:
            raise ConcordError("open() the invite before reading")
        return self.bridge.call("read", bundle=self.bundle, controlWraps=self.controls,
                                channelId=channel_id, chatWraps=chat_wraps)

    def say(self, channel_id: str, text: str, tags: list | None = None, kind: int = 9) -> dict:
        """Build the wrap for a message. Publishing it to the room's relays is the caller's job."""
        if self.bundle is None:
            raise ConcordError("open() the invite before speaking")
        return self.bridge.call("say", bundle=self.bundle, controlWraps=self.controls,
                                channelId=channel_id, text=text, nsec=self._nsec,
                                tags=tags or [], kind=kind)

    def plane_auth(self, challenge: str, relay: str, relays: list | None = None) -> dict:
        """The NIP-42 event a CORD relay demands before it will take this room's traffic.

        Signed with a PLANE key from the room's own membership — NOT this bot's nsec. The relay is
        authenticating the room, and a bot's own key is simply not one of the keys it accepts.
        """
        if self.bundle is None:
            raise ConcordError("open() the invite before authenticating to its relays")
        # THE ALLOWED SET IS THE ROOM'S OWN RELAYS, never the one being asked about. Defaulting it
        # to `[relay]` makes the signer's check vacuous — it would sign an auth for any relay that
        # challenged it, which is this room's key vouching for a relay it never joined.
        allowed = relays or list((self.bundle or {}).get("relays") or [])
        if not allowed:
            raise ConcordError("this room names no relays, so there is nothing to authenticate to")
        return self.bridge.call("planeAuth", bundle=self.bundle, controlWraps=self.controls,
                                challenge=challenge, relay=relay, relays=allowed)


def from_env(env: dict | None = None) -> Room | None:
    """The room this bot was configured with, or None when it has none.

    `CONCORD_INVITE` is injected by `bot_manager_service` from the per-bot `concord_invite` config
    key. Absent is the ordinary case — most bots are not in a room — so this answers None rather
    than raising.
    """
    src = env if env is not None else os.environ
    invite = (src.get("CONCORD_INVITE") or "").strip()
    if not invite:
        return None
    nsec = (src.get("NOSTR_NSEC") or "").strip()
    if not nsec:
        raise ConcordError("CONCORD_INVITE is set but NOSTR_NSEC is not — a bot needs an identity "
                           "to speak in a room")
    return Room(invite, nsec)
