"""New-post limits for the Social timeline: posts per minute, and saying the same thing over and over.

"focus more on new post limits per min and saying the same thing over and over". Two checks, on every
route a post reaches the timeline by -- published here, pulled from other Nostr relays (firehose, sync),
and from the fediverse (bridge puppets):

  1. NEW POSTS PER MINUTE, per author (kind 1: notes and replies). Measured over two weeks here: people
     posted at most 5 a minute; above 10 were only feed bots in bursts (a newspaper bot 35, Twitter
     mirrors 15/14/11, a news briefing 12). Default 10.
  2. THE SAME TEXT FROM ONE AUTHOR more than 3 times in an hour (20+ characters). Everyone who did that
     here was spam: a donation scam x13, "#GM Fren" x11, zap-begging x10, canned replies x7.

Counted by each post's OWN timestamp, so a catch-up sync of an author's older posts is never a burst.
Different people saying the same thing are never affected; reactions, boosts, DMs and app data are not
counted at all. A post past a limit is not stored (a client publishing here is told "rate-limited:").
Admin -> Nostr Relay; 0 turns a check off.
"""
from __future__ import annotations

import hashlib

_MAX_KEYS = 200_000


class SpamGuard:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._minute: dict = {}   # (pubkey, created_at // 60) -> new posts counted
        self._same: dict = {}     # (pubkey, text hash, created_at // 3600) -> times said

    def _num(self, key, default):
        try:
            return int(self.cfg.get(key, default) or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def applies(ev) -> bool:
        try:
            return int(ev.get("kind", -1)) == 1
        except (TypeError, ValueError):
            return False

    def check(self, ev) -> str:
        """'' = store it (and it is counted); otherwise the 'rate-limited:' reason it is refused for."""
        if not self.applies(ev):
            return ""
        per_min, same_max = self._num("posts_per_min", 10), self._num("same_per_hour", 3)
        pk = str(ev.get("pubkey", ""))
        try:
            ts = int(ev.get("created_at", 0))
        except (TypeError, ValueError):
            ts = 0
        mk = (pk, ts // 60)
        if per_min > 0 and self._minute.get(mk, 0) >= per_min:
            return f"rate-limited: more than {per_min} new posts in a minute"
        text = str(ev.get("content") or "").strip()
        sk = None
        if same_max > 0 and len(text) >= 20:
            sk = (pk, hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24], ts // 3600)
            if self._same.get(sk, 0) >= same_max:
                return f"rate-limited: the same post more than {same_max} times an hour"
        self._minute[mk] = self._minute.get(mk, 0) + 1
        if sk is not None:
            self._same[sk] = self._same.get(sk, 0) + 1
        for d in (self._minute, self._same):
            if len(d) > _MAX_KEYS:
                for k in list(d)[: _MAX_KEYS // 2]:
                    d.pop(k, None)
        return ""
