"""Every live subscription Concord opens on a community's relays is `live` with no timeout.

relay.js's subscribeFrom closes after `timeout` (60 s by default) and redials only when `live`. The mini-app (webxdc)
subscriptions passed neither: a running mini app stopped hearing the others a minute in, and every reopen was a fresh
tunnelled socket to somebody else's relay -- the churn jskitty blocked this node over (2026-10-28). A rule over the
whole module, so the next subscription cannot forget it either."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text(encoding="utf-8")


def _calls(name):
    """Each call's argument text, by balanced parentheses."""
    for m in re.finditer(r"(?<![\w.])(?:R\.)?%s\(" % re.escape(name), SRC):
        if SRC[max(0, m.start() - 9):m.start()].endswith("function "):
            continue
        depth, i = 1, m.end()
        while depth and i < len(SRC):
            depth += {"(": 1, ")": -1}.get(SRC[i], 0)
            i += 1
        yield SRC[m.end():i - 1]


def test_every_external_live_subscription_is_live_with_no_timeout():
    bad = []
    for name in ("subscribeFrom", "cordPlaneSubscribe"):
        for args in _calls(name):
            if "viaOpts(o)" in args or "...options" in args:   # the door wrapper and the plane fan-out pass options through
                continue
            if not re.search(r"timeout:0\b", args) or not re.search(r"live:true\b", args):
                bad.append("%s(%s)" % (name, args[:120]))
    assert not bad, "a Concord subscription that closes after 60 s:\n" + "\n".join(bad)


def test_no_client_module_opens_a_live_subscription_that_times_out():
    """The same rule for every client module: `live:true` with relay.js's default 60 s timeout is a live subscription
    that silently ends a minute in (the Concord direct-invite inbox in dms.js did exactly that)."""
    import glob
    bad = []
    for path in sorted(glob.glob(str(Path(__file__).resolve().parents[2] / "static/js/client/*.js"))):
        src = Path(path).read_text(encoding="utf-8")
        for m in re.finditer(r"subscribeFrom\(", src):
            depth, i = 1, m.end()
            while depth and i < len(src):
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                i += 1
            args = src[m.end():i - 1]
            if re.search(r"live:true\b", args) and not re.search(r"timeout:0\b", args):
                bad.append("%s: subscribeFrom(%s)" % (Path(path).name, args[:120]))
    assert not bad, "live subscriptions that end after 60 s:\n" + "\n".join(bad)
