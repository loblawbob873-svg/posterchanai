"""Opening the Social feed must ask a socket that can ANSWER, and must notice when none did.

THE REPORT: "I can pull up another app or webpage, but the app registers as offline. Also, if I
close and reopen the app it shows new posts from a minute ago."

Two symptoms, one cause. `relay.js _send` drops a REQ written to a socket that is CONNECTING — no
error, no event, no EOSE — and `renderTimeline` subscribed the instant it was entered, without ever
asking whether the pool could carry it. The moment a timeline is most likely to be opened against
such a socket is right after a resume or a login, and a phone coming back from sleep holds something
worse: a ZOMBIE, which the browser still reports OPEN while it delivers nothing. `Relay.ready()`
detects that (30s silent on a trusted socket) and calls `reviveStale()` to repair it. The feed never
called it, so it subscribed into a dead pipe and sat there looking connected.

WHY ONE DROPPED REQ FREEZES THE WHOLE VISIT, and not just the first page: `_tl.eosed` only becomes
true on an EOSE, and `_bufferLive` is gated on `_tl.eosed` — so the live posts that DO arrive are
discarded too. Nothing is logged and the cached feed still renders, so it reads as "the app is
stale", and closing and reopening is the only thing that ever asks again. That is the second half of
the report, exactly.

AND THE BANNER: "offline" is driven by Relay's STATUS stream, which reports CHANGES. A socket that
died while the phone was asleep and was quietly replaced never announces itself, so the banner
latches on over a feed that is loading perfectly. An EOSE is proof of the opposite that is MEASURED
rather than retold — a relay just answered us — so it settles the question.

This file asserts the contract, not the wording. Every check here was run against the pre-fix file
and fails on it.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
RELAY = os.path.join(ROOT, "static", "js", "client", "relay.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, head):
    """The body of a function, by brace matching — a fixed slice drifts the moment anything moves."""
    i = src.index(head)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"{head} never closes")


def _decomment(src):
    """Comments explain the rule; they must not be able to SATISFY a test about the code.

    This file states its own forbidden strings inside its own comments, which is how a guard
    silently passes against the very code it was written to reject."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", "", src)


class TheFeedWaitsForASocketThatCanAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.tl = _decomment(_fn(cls.app, "function renderTimeline("))
        cls.relay = _read(RELAY)

    # ---- the premise these rules rest on ----------------------------------------------------
    def test_a_req_to_a_socket_that_is_not_open_is_dropped_in_silence(self):
        """If this stops being true the whole file is guarding against nothing.

        Read off the CONNECTION's _send (the one that owns a socket), not the pool's fan-out and
        not one of the eight call sites that share the name."""
        send = _decomment(_fn(self.relay, "_send(arr){ if (this.ws"))
        self.assertIn("readyState === 1", send,
                      "_send no longer gates on the socket state; the premise here has changed")
        self.assertNotIn("throw", send, "_send reports a dropped REQ now — re-read this test")

    def test_subscribe_writes_the_req_straight_at_every_open_socket(self):
        """The other half of the premise: nothing between renderTimeline and the wire queues or
        retries a REQ, so if no socket is OPEN at that instant the ask simply never happens."""
        i = self.relay.index("subscribe(filters")
        body = _decomment(self.relay[i:self.relay.index("return id;", i)])
        self.assertIn("readyState === 1", body,
                      "subscribe no longer writes only to OPEN sockets — re-read this test")
        self.assertNotIn("_queue", body, "subscribe queues a REQ now — re-read this test")

    def test_an_eose_is_not_proof_of_anything_because_subscribe_invents_one(self):
        """THE TRAP THIS FILE EXISTS TO AVOID REPEATING. `subscribe` fires onEose from a 12-second
        backstop when NO relay answered, so a caller that read an EOSE as "a relay spoke to us"
        would announce a connection at exactly the moment there is none."""
        i = self.relay.index("subscribe(filters")
        body = self.relay[i:self.relay.index("return id;", i)]
        self.assertIn("_fireEose", body,
                      "the EOSE backstop is gone — an EOSE may be honest proof again, but check "
                      "before moving the banner's proof back onto it")

    def test_ready_repairs_a_zombie_rather_than_only_reporting_one(self):
        """This is why `ready()` and not a bare readyState check: a phone back from sleep holds a
        socket the browser calls OPEN that delivers nothing."""
        body = _decomment(_fn(self.relay, "ready(ms="))
        self.assertIn("reviveStale", body,
                      "ready() no longer repairs a zombie, so waiting on it fixes less than this "
                      "test claims")

    def test_a_connecting_socket_cannot_leave_android_offline_forever(self):
        body = _decomment(_fn(self.relay, "reviveStale(){"))
        self.assertIn("c._openingAt", body)
        self.assertIn("stuck", body)
        self.assertIn("10000", body)

    def test_publish_uses_connection_recovery_before_reporting_offline(self):
        body = _decomment(_fn(self.relay, "async publish(event"))
        self.assertIn("await this.ready", body)
        self.assertIn("msg:'offline'", body)

    # ---- the fix ----------------------------------------------------------------------------
    def test_the_subscription_waits_on_ready(self):
        """Measured on CONTROL FLOW, not on the order the lines happen to be written in: the REQ
        must live inside the continuation, so it cannot be issued on the synchronous path."""
        sub = _fn(self.tl, "const fullSub =")
        self.assertIn("Relay.ready(", sub,
                      "the feed subscribes without asking whether the pool can carry the REQ")
        go = _fn(sub, "const go =")
        self.assertIn("Relay.subscribe(", go,
                      "the REQ is not in the continuation, so it runs before ready() answers")
        self.assertEqual(sub.count("Relay.subscribe("), 1,
                         "there is a second, unguarded subscribe on the synchronous path")
        self.assertNotIn("Relay.ready(", go, "ready() is inside the continuation it gates")

    def test_a_pool_that_never_came_up_still_gets_the_req(self):
        """Both arms of the promise must install the subscription. A relay that connects a moment
        later re-REQs what is installed; never asking is the bug this file is about."""
        sub = _fn(self.tl, "const fullSub =")
        self.assertTrue(re.search(r"\.then\(\s*go\s*,\s*go\s*\)", sub),
                        "a failed/timed-out ready() abandons the subscription entirely")

    def test_the_paint_still_happens_before_any_of_it(self):
        """Cache-first is not traded away for this: the feed is drawn from the Store first and only
        the network half waits. See test_cache_first_paint.py for the general rule."""
        self.assertLess(self.tl.index("_drawTimeline(false)"), self.tl.index("Relay.ready("),
                        "the first paint now sits behind a network wait")

    def test_a_silent_req_is_asked_again_exactly_once(self):
        """Once: a second silence is a fact about the network, and more asking will not change it.
        The cached feed stays on screen, which is honest."""
        watch = _fn(self.tl, "const _watchEose =")
        self.assertIn("_eoseRetried", watch, "nothing notices that no relay ever answered")
        self.assertIn("fullSub()", watch, "the watchdog notices and then does nothing about it")
        self.assertIn("Relay.close", watch,
                      "the dead subscription is left installed, so the retry leaks one")
        self.assertIn("_eoseRetried=true", watch.replace(" ", ""),
                      "the retry is not latched, so a dead relay is asked for ever")

    def test_the_watchdog_is_armed_by_the_subscribe_and_disarmed_by_the_answer(self):
        sub = _fn(self.tl, "const fullSub =")
        self.assertIn("_watchEose()", sub, "the watchdog is never armed")
        marked = _fn(self.tl, "const markEosed =")
        self.assertIn("clearTimeout(_eoseWatch)", marked,
                      "an answered feed still re-subscribes seven seconds later")

    def test_an_arriving_event_clears_the_offline_banner(self):
        """The banner is driven by a status stream that reports CHANGES, so a socket replaced while
        the phone slept never announces itself. An event pushed down that socket is measured proof
        — and unlike an EOSE it cannot be invented by a timer (see the test above)."""
        ev = _fn(self.tl, "const onEvent =")
        self.assertIn("updateOfflineBar('ok')", ev,
                      "the app can load a feed and still call itself offline")
        self.assertIn("is-offline", ev,
                      "the call is not gated, so it runs once per event on a firehose")

    def test_the_eose_does_not_claim_a_connection(self):
        """It fires from a backstop when nobody answered; using it as proof puts the banner exactly
        the wrong way round."""
        marked = _fn(self.tl, "const markEosed =")
        self.assertNotIn("updateOfflineBar", marked,
                         "an invented EOSE would announce a connection that does not exist")

    def test_the_banner_only_clears_on_ok(self):
        """The premise for the test above: nothing else in updateOfflineBar clears it, so the EOSE
        really is the only other way out."""
        body = _decomment(_fn(self.app, "function updateOfflineBar("))
        self.assertIn("s === 'ok'", body, "updateOfflineBar changed — re-read this test")

    def test_live_posts_are_still_gated_on_a_real_eose(self):
        """The tempting shortcut is to set `_tl.eosed` when the watchdog fires, which would claim a
        complete feed nobody sent. It must only ever be set by an actual EOSE."""
        assigns = re.findall(r"_tl\.eosed\s*=\s*true", self.tl)
        self.assertEqual(len(assigns), 1,
                         "_tl.eosed is set somewhere other than markEosed — an empty answer would "
                         "be dressed up as an answer")
        marked = _fn(self.tl, "const markEosed =")
        self.assertIn("_tl.eosed=true", marked.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
