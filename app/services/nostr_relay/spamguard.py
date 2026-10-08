"""New-post limits for the Social timeline: posts per minute, and saying the same thing over and over.

"focus more on new post limits per min and saying the same thing over and over". Two checks, on every
route a post reaches the timeline by -- published here, pulled from other Nostr relays (firehose, sync),
and from the fediverse (bridge puppets):

  1. NEW POSTS PER MINUTE, per author (kind 1: notes and replies). Measured over two weeks here: people
     posted at most 5 a minute; above 10 were only feed bots in bursts (a newspaper bot 35, Twitter
     mirrors 15/14/11, a news briefing 12). Default 10.
  2. THE SAME TEXT FROM ONE AUTHOR more than 3 times in an hour (SAME_MIN_CHARS+ characters). Everyone who
     did that here was spam: a donation scam x13, "#GM Fren" x11, zap-begging x10, canned replies x7. The
     minimum was 20 until a relay prober posted "mc-relay-probe" (14 characters) 753 times in a day, about 35
     an hour, straight under it (2026-10-08). Measured at 8 over 30 days, besides the prober's 1,375 notes it
     only trims the 4th-and-later copy in one hour of short repeats ("GM!🐱 🍵 ✨" x37, a harassment line
     x21); "gm" and friends stay under it, and nobody loses their first three.

Counted by each post's OWN timestamp, so a catch-up sync of an author's older posts is never a burst.
Different people saying the same thing are never affected; reactions, boosts, DMs and app data are not
counted at all. A post past a limit is not stored (a client publishing here is told "rate-limited:").
Admin -> Nostr Relay; 0 turns a check off.
"""
from __future__ import annotations

import hashlib
import time

_MAX_KEYS = 200_000


SAME_MIN_CHARS = 8              # shortest text the same-post check applies to (see 2. above)
GIFT_WINDOW = 600               # seconds: the gift-wrap budget is counted per recipient per 10 minutes


class SpamGuard:
    def __init__(self, cfg: dict, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self._minute: dict = {}   # (pubkey, created_at // 60) -> new posts counted
        self._same: dict = {}     # (pubkey, text hash, created_at // 3600) -> times said
        self._gift: dict = {}     # (recipient, arrival // GIFT_WINDOW) -> gift wraps stored

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
        if same_max > 0 and len(text) >= SAME_MIN_CHARS:
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

    def check_gift(self, ev) -> str:
        """A FLOOD OF GIFT WRAPS AT ONE PERSON (kind 1059). '' = store it; otherwise the reason it is refused.

        "Someone is spamming kind 1059 gift-wrap events right now. About 13k per minute. And 96%+ of them
        are all being directed at one npub" (2026-10-04). A gift wrap is signed by a throwaway key, so no
        web-of-trust test can see its author; this relay accepts any wrap written to it (Concord's wraps
        carry random cover tags) and pulls any wrap addressed to a member -- and one member here had
        already been sent 32,262 of them. So the budget is per RECIPIENT, counted by ARRIVAL time: NIP-59
        backdates created_at by up to two days, and counted by created_at a flood spreads thin enough to
        pass. Concord's random cover tags never concentrate on one recipient, so rooms are unaffected.
        The default (300 per 10 minutes, ~30/min) holds a busy inbox and a reconnect catch-up; a flood of
        thousands a minute is cut off within seconds. 0 turns it off."""
        try:
            if int(ev.get("kind", -1)) != 1059:
                return ""
        except (TypeError, ValueError):
            return ""
        cap = self._num("gift_per_recipient", 300)
        if cap <= 0:
            return ""
        slot = int(self.clock()) // GIFT_WINDOW
        rcpts = {str(t[1]).lower() for t in (ev.get("tags") or [])
                 if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0] == "p" and t[1]}
        keys = [(r, slot) for r in rcpts]
        if any(self._gift.get(k, 0) >= cap for k in keys):
            return f"rate-limited: more than {cap} gift wraps for one recipient in 10 minutes"
        for k in keys:
            self._gift[k] = self._gift.get(k, 0) + 1
        if len(self._gift) > _MAX_KEYS:
            for k in [k for k in self._gift if k[1] < slot]:
                self._gift.pop(k, None)
            if len(self._gift) > _MAX_KEYS:
                for k in list(self._gift)[: _MAX_KEYS // 2]:
                    self._gift.pop(k, None)
        return ""
