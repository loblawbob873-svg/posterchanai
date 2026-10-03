"""One spam guard for every way a post reaches the Social timeline.

"i want to avoid spammers hammering the social timeline of fedi and nostr". Posts arrive three ways:
a client publishing here (direct), the firehose/sync pulling the web of trust's posts from other relays
(wot), and the fediverse inbox storing them under puppet keys (bridge). A limit on only the first is a
limit on almost nothing -- nearly all of the timeline is the other two.

TIMELINE POSTS ONLY (notes, comments, reposts, polls, articles, picture/video posts, highlights).
Reactions are counts under a post, never posts on the timeline; DMs, app data, lists and settings are
not feed content at all.

Two clocks, on purpose:
  * DIRECT writes are counted by ARRIVAL (token buckets): a client can backdate its events, it cannot
    backdate when they reach us.
  * The FIREHOSE and the FEDIVERSE are counted by the author's OWN timestamps (minute/hour windows):
    a catch-up sync delivers an author's last day in one second, and that is not the author flooding
    anything -- 35 posts stamped in one minute is.

SPAM, NOT VOLUME ("those users did nothing wrong, i want to prevent actual spam from flooding the
timeline"). Measured over two weeks here: the busiest real accounts posted 41 timeline posts in a minute
and 92 in an hour, and they are people, not spam -- so the VOLUME ceiling (60/min, 400/h) only stops a
machine-gun flood. What spam actually looks like is repetition:
  * one author pasting the same post again and again -- every author over 3 identical posts an hour in
    those two weeks was spam (a donation scam x13, "Want more sats, share my daily content" x10, "Stay
    humble and stack zaps" x7, the same link x7 ...);
  * the same post from many different accounts -- a bot farm. The most distinct accounts behind one text
    in an hour here was 5 (a thread game); farms run to dozens.
Defaults: 60/min, 400/h, 3 identical per author per hour, 5 accounts per identical text per hour.
Admin -> Nostr Relay; 0 turns any one off.
"""
from __future__ import annotations

import hashlib
import time

TIMELINE_KINDS = frozenset({1, 1111, 6, 16, 1068, 30023, 20, 21, 22, 9802})
_MAX_KEYS = 200_000


class SpamGuard:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._buckets: dict = {}      # direct: pubkey -> [minute tokens, hour tokens, last]
        self._minute: dict = {}       # timestamped: (pubkey, created_at // 60) -> count
        self._hour: dict = {}         # timestamped: (pubkey, created_at // 3600) -> count
        self._dups: dict = {}         # (pubkey, content hash, hour) -> count (both clocks)
        self._crowd: dict = {}        # (content hash, hour) -> set of pubkeys (both clocks)

    def _num(self, key, default):
        try:
            return int(self.cfg.get(key, default) or 0)
        except (TypeError, ValueError):
            return default

    def limits(self):
        return (self._num("rate_per_min", 60), self._num("rate_per_hour", 400), self._num("dup_per_hour", 3))

    def _crowd_ok(self, ev, hour) -> str:
        """The same text from more than N different accounts in an hour: the extra accounts are a farm."""
        n = self._num("dup_authors_per_hour", 5)
        content = str(ev.get("content") or "").strip()
        if n <= 0 or int(ev.get("kind", 0)) not in (1, 1111) or len(content) < 40:
            return ""
        key = (hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:24], hour)
        who = self._crowd.get(key)
        pk = str(ev.get("pubkey", ""))
        if who is None:
            who = self._crowd[key] = set()
        if pk not in who:
            if len(who) >= n:
                return f"rate-limited: the same post from more than {n} accounts"
            who.add(pk)
            self._prune(self._crowd)
        return ""

    @staticmethod
    def applies(ev) -> bool:
        try:
            return int(ev.get("kind", -1)) in TIMELINE_KINDS
        except (TypeError, ValueError):
            return False

    def _dup_key(self, ev, hour):
        content = str(ev.get("content") or "").strip()
        if int(ev.get("kind", 0)) not in (1, 1111) or len(content) < 20:
            return None
        return (str(ev.get("pubkey", "")),
                hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:24], hour)

    def _prune(self, d):
        if len(d) > _MAX_KEYS:
            for k in list(d)[: _MAX_KEYS // 2]:
                d.pop(k, None)

    def check_arrival(self, ev, now: float | None = None) -> str:
        """A direct write. '' = allowed (and counted), else the NIP-01 'rate-limited:' reason."""
        if not self.applies(ev):
            return ""
        per_min, per_hour, dup_max = self.limits()
        now = time.time() if now is None else now
        pk = str(ev.get("pubkey", ""))
        if per_min > 0 or per_hour > 0:
            cap_m = per_min if per_min > 0 else 10 ** 9
            cap_h = per_hour if per_hour > 0 else 10 ** 9
            b = self._buckets.get(pk)
            if b is None:
                b = [float(cap_m), float(cap_h), now]
            else:
                dt = max(0.0, now - b[2])
                b = [min(cap_m, b[0] + dt * cap_m / 60.0), min(cap_h, b[1] + dt * cap_h / 3600.0), now]
            if b[0] < 1 or b[1] < 1:
                self._buckets[pk] = b
                return f"rate-limited: slow down -- more than {per_min} posts a minute or {per_hour} an hour"
            b[0] -= 1
            b[1] -= 1
            self._buckets[pk] = b
            self._prune(self._buckets)
        crowd = self._crowd_ok(ev, int(now // 3600))
        if crowd:
            return crowd
        if dup_max > 0:
            key = self._dup_key(ev, int(now // 3600))
            if key is not None:
                n = self._dups.get(key, 0)
                if n >= dup_max:
                    return f"rate-limited: the same post more than {dup_max} times an hour"
                self._dups[key] = n + 1
                self._prune(self._dups)
        return ""

    def check_timestamps(self, ev) -> str:
        """A post from the firehose or the fediverse, judged by the author's own timestamps."""
        if not self.applies(ev):
            return ""
        per_min, per_hour, dup_max = self.limits()
        pk = str(ev.get("pubkey", ""))
        try:
            ts = int(ev.get("created_at", 0))
        except (TypeError, ValueError):
            ts = 0
        mk, hk = (pk, ts // 60), (pk, ts // 3600)
        if per_min > 0 and self._minute.get(mk, 0) >= per_min:
            return f"rate-limited: more than {per_min} posts in one minute"
        if per_hour > 0 and self._hour.get(hk, 0) >= per_hour:
            return f"rate-limited: more than {per_hour} posts in one hour"
        dkey = self._dup_key(ev, ts // 3600) if dup_max > 0 else None
        if dkey is not None and self._dups.get(dkey, 0) >= dup_max:
            return f"rate-limited: the same post more than {dup_max} times an hour"
        crowd = self._crowd_ok(ev, ts // 3600)
        if crowd:
            return crowd
        self._minute[mk] = self._minute.get(mk, 0) + 1
        self._hour[hk] = self._hour.get(hk, 0) + 1
        if dkey is not None:
            self._dups[dkey] = self._dups.get(dkey, 0) + 1
        for d in (self._minute, self._hour, self._dups):
            self._prune(d)
        return ""
