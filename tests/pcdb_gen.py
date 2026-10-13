"""A seeded generator of relay events and NIP-01 filters for the PosterChanDB tests (it was born in the
differential test against the retired Postgres store, which is why it covers every ingest rule's corner)."""
import random
import time

from tests.test_posterchandb import ev_id                # noqa: E402
from tests.test_posterchandb_tsparser import FRAG        # noqa: E402

NOW = int(time.time())


class Gen:
    def __init__(self, seed):
        self.r = random.Random(seed)
        self.authors = ["%064x" % self.r.getrandbits(256) for _ in range(6)]
        self.made = []

    def text(self):
        r = self.r
        return " ".join(r.choice(FRAG) for _ in range(r.randint(0, 8)))

    def ts(self):
        r = self.r
        return NOW - r.choice([r.randint(0, 3 * 86400), r.randint(0, 50), 1000, 1000, 2000])   # plenty of ties

    def event(self):
        r = self.r
        pk = r.choice(self.authors)
        kind = r.choice([1, 1, 1, 1, 6, 7, 0, 3, 10002, 10000, 30023, 30078, 30078, 30617, 1111, 9735, 1059, 5, 5,
                         40, 34550, 10133, 20001, 1068])
        tags = []
        if self.made and r.random() < 0.6:
            tgt = r.choice(self.made)
            tags.append(["e", tgt["id"]] + ([""] if r.random() < 0.3 else []))
        for _ in range(r.randint(0, 3)):
            tags.append(r.choice([["p", r.choice(self.authors)], ["t", r.choice(["nostr", "art", "Nostr", "x y"])],
                                  ["K", r.choice(["1", "1617"])], ["d"], ["subject", "hi"]]))
        if kind in (30023, 30078, 30617, 34550) or r.random() < 0.05:
            for _ in range(r.choice([1, 1, 1, 2])):
                tags.append(["d", r.choice(["", "a", "b", "pcai:note:1", "pcai:note:2", "pcai:mail:x:INBOX:1"])])
        if kind == 5 and self.made:
            tgt = r.choice(self.made)
            if r.random() < 0.5:
                tags.append(["e", tgt["id"]])
            else:
                d = next((t[1] for t in tgt["tags"] if len(t) >= 2 and t[0] == "d"), "")
                tags.append(["a", "%d:%s:%s" % (tgt["kind"], r.choice([tgt["pubkey"], pk]), d)])
        if kind == 1 and self.made and r.random() < 0.2:
            q = r.choice(self.made)
            tags.append(["q", q["id"]] + ([""] if r.random() < 0.5 else ["", q["pubkey"]]))
        if r.random() < 0.08:
            tags.append(["expiration", str(r.choice([NOW - 10, NOW + 3600, NOW + 86400]))])
        if r.random() < 0.02:
            tags.append(["client-mode", "fedi-only"])
        ev = {"pubkey": pk, "created_at": r.choice([self.ts(), self.ts(), NOW + 5000]) if r.random() < 0.03
              else self.ts(), "kind": kind, "tags": tags, "content": self.text(), "sig": "%0128x" % r.getrandbits(512)}
        ev["id"] = ev_id(ev)
        self.made.append(ev)
        return ev

    def flt(self):
        r = self.r
        f = {}
        pick = r.random()
        if pick < 0.15 and self.made:
            f["ids"] = [r.choice(self.made)["id"] for _ in range(r.randint(1, 6))]
        if r.random() < 0.5:
            f["authors"] = r.sample(self.authors, r.randint(1, 3))
        if r.random() < 0.5:
            f["kinds"] = r.sample([0, 1, 3, 5, 6, 7, 1111, 9735, 10002, 30023, 30078, 1059], r.randint(1, 3))
        if r.random() < 0.2 and self.made:
            f["#e"] = [r.choice(self.made)["id"]]
        if r.random() < 0.2:
            f["#p"] = [r.choice(self.authors)]
            if r.random() < 0.5:
                f["_include_quotes"] = True
        if r.random() < 0.1:
            f["#t"] = [r.choice(["nostr", "art", "Nostr", "x y"])]
        if r.random() < 0.1:
            f["#d"] = [r.choice(["", "a", "b", "pcai:note:1"])]
        if r.random() < 0.08:
            f["#d~"] = [r.choice(["pcai:", "pcai:note:", "pcai:mail:x:INBOX:", "a"])]
        if r.random() < 0.15:
            f["search"] = r.choice([self.text(), r.choice(FRAG), " ".join(r.sample(FRAG, 2))])
        if r.random() < 0.3:
            f["since"] = NOW - r.randint(0, 3 * 86400)
        if r.random() < 0.3:
            f["until"] = NOW - r.randint(0, 2 * 86400)
        if r.random() < 0.7:
            f["limit"] = r.choice([1, 5, 20, 100, 500, 5000, 0])
        if r.random() < 0.1 and self.made:
            e = r.choice(self.made)
            f["_cursor"] = [e["created_at"], e["id"]]
        return f
