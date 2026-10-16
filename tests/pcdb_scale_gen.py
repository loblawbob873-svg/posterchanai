"""Realistic-shaped events, FAST, for the PosterChanDB scale tests: many authors, a vocabulary with a long tail, `p`/`e`/`t`
tags and app documents with `pcai:` d tags (a third of a real relay's events are addressable app documents). No signatures:
the Store never verifies one (the relay does, before it stores)."""
import random

KINDS = [1] * 10 + [7] * 6 + [6] * 2 + [0, 3, 10002, 30078, 30078, 30078, 30023, 1059, 5, 9735]


class ScaleGen:
    def __init__(self, seed=1, authors=20000, vocab=50000, now=1_760_000_000):
        self.r = random.Random(seed)
        self.authors = ["%064x" % self.r.getrandbits(256) for _ in range(authors)]
        self.vocab = ["w%x" % i for i in range(vocab)]
        self.now = now
        self.i = 0

    def event(self) -> dict:
        r, self.i = self.r, self.i + 1
        kind = r.choice(KINDS)
        pk = self.authors[min(len(self.authors) - 1, int(r.paretovariate(1.2)) - 1)] if r.random() < .7 else r.choice(self.authors)
        tags = [["p", r.choice(self.authors)] for _ in range(r.randint(0, 2))]
        if r.random() < .4:
            tags.append(["e", "%064x" % r.getrandbits(256)])
        if r.random() < .2:
            tags.append(["t", r.choice(self.vocab[:500])])
        content = " ".join(self.vocab[min(len(self.vocab) - 1, int(r.paretovariate(1.05)) - 1)] for _ in range(r.randint(0, 12)))
        if kind >= 30000:
            tags.append(["d", "pcai:t:%s:%x" % (r.choice(["users", "notes", "fs", "kv"]), r.getrandbits(48))])
            content = "x" * r.randint(40, 400)
        return {"id": "%064x" % r.getrandbits(256), "pubkey": pk, "created_at": self.now - r.randint(0, 86400 * 30) + self.i // 50,
                "kind": kind, "tags": tags, "content": content, "sig": "0" * 128}
