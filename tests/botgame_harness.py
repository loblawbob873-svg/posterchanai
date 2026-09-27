"""Play a game bot for real, with only the NETWORK faked.

The game bots (chess, Connect 4, Hangman, tic-tac-toe) are driven exactly as in production: their
`process_<game>()` loop reads mentions and NIP-17 DMs off the relay, starts and advances games, saves
state as replaceable kind-30388 documents, DMs each player their board and posts the result. Here the
relay is an in-memory store, image upload records the PNG it was handed, and profile lookups answer
from a table -- everything else is the shipped code: real signed events, real NIP-17 gift wraps (a
player's DM reply is really encrypted to the bot, the bot's DM is really decrypted by the player),
real NIP-44 for Hangman's secret word, real board rendering.
"""
import asyncio
import importlib
import os
import sys
import time

from app.services.nostr import bip340, event as E, nip17

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(ROOT, "botframework")
BOT_SK = bytes([0x42]) * 32
BOT_PK = bip340.pubkey_from_seckey(BOT_SK).hex()
REPLACEABLE = lambda k: k in (0, 3) or 10000 <= k < 20000 or 30000 <= k < 40000


class FakeRelay:
    def __init__(self):
        self.events = []

    def _key(self, ev):
        k = ev["kind"]
        if 30000 <= k < 40000:
            d = next((t[1] for t in ev.get("tags", []) if len(t) > 1 and t[0] == "d"), "")
            return (k, ev["pubkey"], d)
        if REPLACEABLE(k):
            return (k, ev["pubkey"])
        return None

    async def publish(self, relays, ev, *a, **kw):
        key = self._key(ev)
        if key is not None:
            self.events = [e for e in self.events if self._key(e) != key or e["created_at"] > ev["created_at"]]
            if any(self._key(e) == key for e in self.events):
                return 1                        # an older version never replaces a newer one
        self.events.append(ev)
        return 1

    @staticmethod
    def _match(ev, f):
        if "kinds" in f and ev["kind"] not in f["kinds"]:
            return False
        if "authors" in f and ev["pubkey"] not in f["authors"]:
            return False
        if "ids" in f and ev["id"] not in f["ids"]:
            return False
        if "since" in f and ev["created_at"] < f["since"]:
            return False
        for k, want in f.items():
            if k.startswith("#") and not any(len(t) > 1 and t[0] == k[1:] and t[1] in want for t in ev.get("tags", [])):
                return False
        return True

    async def query(self, relays, filters, *a, **kw):
        out = {}
        for f in filters:
            hits = sorted((e for e in self.events if self._match(e, f)), key=lambda e: -e["created_at"])
            for e in hits[:f.get("limit", 10 ** 6)]:
                out[e["id"]] = e
        return sorted(out.values(), key=lambda e: -e["created_at"])


class Player:
    def __init__(self, name, n):
        self.name, self.sk = name, bytes([n]) * 32
        self.pk = bip340.pubkey_from_seckey(self.sk).hex()


class GameWorld:
    """One bot, one relay, any number of players."""

    def __init__(self, monkeypatch, tmp_path, module, loop_fn):
        monkeypatch.setenv("NOSTR_NSEC", BOT_SK.hex())
        monkeypatch.setenv("NOSTR_RELAYS", "ws://fake")
        monkeypatch.syspath_prepend(BOTS)
        for m in ("config", "nostr", module):
            sys.modules.pop(m, None)
        self.relay, self.uploads, self.names = FakeRelay(), [], {BOT_PK: "gamebot"}
        # A NIP-17 message's id hashes (sender, content, created_at in WHOLE seconds): a player sending
        # "1" twice in one second sends the SAME message, and the bot rightly treats it as a
        # duplicate. People do not type that fast; a test does. So messages get a clock that moves a
        # second each, which also keeps "which DM came last" well defined.
        import types
        clock = {"t": int(time.time())}

        def _tick_time():
            clock["t"] += 1
            return clock["t"]
        monkeypatch.setattr(nip17, "time", types.SimpleNamespace(time=_tick_time))
        self._clock = clock
        nk = importlib.import_module("nostr")
        monkeypatch.setattr(nk._svc.relay, "publish", self.relay.publish)
        monkeypatch.setattr(nk._svc.relay, "query", self.relay.query)

        async def upload(cfg, sk, data, mime, *a, **kw):
            assert data[:8] == b"\x89PNG\r\n\x1a\n", "a board that is not a PNG was uploaded"
            self.uploads.append(data)
            return {"url": f"https://media.test/board{len(self.uploads)}.png", "sha256": "0" * 64, "dim": "1x1"}
        monkeypatch.setattr(nk._svc.media, "upload", upload)
        monkeypatch.setattr(nk, "resolve_user", lambda pk: {"username": self.names.get(pk, pk[:8]), "pubkey": pk})
        nk._wrap_cache.clear() if hasattr(nk, "_wrap_cache") else None
        self.nk = nk
        self._mp, self._tmp, self._module, self._loop_fn = monkeypatch, tmp_path, module, loop_fn
        self._load_game_module()
        self._seq = 0

    def _load_game_module(self):
        """Import the game module and point its files and clock at the test's -- also what a
        'restart' calls, so a reloaded bot keeps the same clock and temp dir."""
        monkeypatch, tmp_path, module, loop_fn, clock = self._mp, self._tmp, self._module, self._loop_fn, self._clock
        import types
        sys.modules.pop(module, None)
        self.game = importlib.import_module(module)
        for attr in ("_IDS_FILE", "_DM_IDS_FILE", "_LOCK_FILE"):
            if hasattr(self.game, attr):
                monkeypatch.setattr(self.game, attr, str(tmp_path / f"{module}{attr}"))
        if hasattr(self.game, "script_dir"):
            monkeypatch.setattr(self.game, "script_dir", str(tmp_path))
        # The bot reads the SAME clock (without advancing it): its pointer timestamps and the players'
        # DM timestamps have to be comparable, or "sent before the pointer" means nothing here.
        import time as _real
        game_time = types.SimpleNamespace(**{k: getattr(_real, k) for k in dir(_real) if not k.startswith("_")})
        game_time.time = lambda: float(clock["t"])
        monkeypatch.setattr(self.game, "time", game_time)
        if hasattr(self.game, "_invite_times"):
            self.game._invite_times.clear()
        self._loop = getattr(self.game, loop_fn)

    def restart(self):
        """The bot process restarts: fresh module state, same relay and files."""
        self._load_game_module()

    def player(self, name):
        self._seq += 1
        p = Player(name, 0x10 + self._seq)
        self.names[p.pk] = name
        return p

    # ---- what a player does ----------------------------------------------------------------------
    def _now(self):
        self._seq += 1
        return int(time.time()) + self._seq            # strictly increasing, never in the future by much

    def reply(self, who, text, root):
        """A PUBLIC reply in the game thread (the cross-client way to play)."""
        return self.mention(who, text, root=root)

    def replies_to(self, note_id):
        """The bot's public answers to one note."""
        return [e["content"] for e in self.posts()
                if any(len(t) > 1 and t[0] == "e" and t[1] == note_id for t in e.get("tags", []))]

    def mention(self, who, text, tag=(), root=None):
        tags = [["p", BOT_PK]] + [["p", p.pk] for p in tag]
        if root:
            tags.insert(0, ["e", root, "", "root"])
        ev = E.build_event(who.sk, 1, text, tags=tags, created_at=self._now())
        asyncio.run(self.relay.publish(None, ev))
        return ev["id"]

    def publish(self, who, kind, content="", tags=()):
        ev = E.build_event(who.sk, kind, content, tags=[list(t) for t in tags], created_at=self._now())
        asyncio.run(self.relay.publish(None, ev))
        return ev

    def events(self, kind):
        return sorted(asyncio.run(self.relay.query(None, [{"kinds": [kind], "authors": [BOT_PK]}])),
                      key=lambda e: e["created_at"])

    def dm(self, who, text):
        asyncio.run(self.relay.publish(None, nip17.wrap(who.sk, BOT_PK, text)))

    def tick(self, n=1):
        for _ in range(n):
            self._loop()

    # ---- what a player sees ----------------------------------------------------------------------
    def dms_to(self, who):
        out = []
        for w in asyncio.run(self.relay.query(None, [{"kinds": [1059], "#p": [who.pk]}])):
            try:
                sender, text, rumor = nip17.unwrap(who.sk, w)
            except Exception:
                continue
            if sender == BOT_PK:
                out.append((rumor.get("created_at", 0), text))
        return [t for _, t in sorted(out)]

    def posts(self):
        """The bot's public kind-1 posts, oldest first."""
        evs = asyncio.run(self.relay.query(None, [{"kinds": [1], "authors": [BOT_PK]}]))
        return [e for e in sorted(evs, key=lambda e: e["created_at"])]

    def state(self, gameid):
        return self.game._load_game(gameid)
