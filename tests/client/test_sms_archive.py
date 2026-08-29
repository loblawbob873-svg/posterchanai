"""The phone's text archive: what it publishes, what it deletes, and what it must never do twice.

Run: venv-unified/bin/python -m unittest tests.client.test_sms_archive

These drive the SHIPPED static/js/client/sms.js under node against a stub phone and a stub relay
(sms_sim.js), because every rule worth checking here is a relationship between two calls rather than
a string:

  * A DELETE IS TWO DELETES. The phone's provider is authoritative on the device and the Nostr
    document is the copy every other device reads. Remove them in the wrong order and a provider
    delete that fails leaves a tombstone the next mirror publishes straight back over.
  * THE HIGH-WATER MARK MAY ONLY MOVE PAST WHAT LANDED. A relay that stops taking writes half way
    through a batch must leave the mark where the last success was — otherwise the rest of somebody's
    history is skipped silently and nothing ever goes back for it.
  * A SEND ANOTHER DEVICE ASKED FOR MUST BE MARKED DONE EVEN WHEN IT FAILED. The alternative is a
    phone that performs it again on every drain, and there is no way to un-send a text.

Each of these was checked to fail with the rule removed.
"""
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests" / "client" / "sms_sim.js"

DAY = 86400000
# RECENT, because the phone only publishes the last thirty days on a first run. Fixtures dated three
# years ago produced an archive of nothing and every assertion failed identically — which reads
# exactly like the mirror being broken.
NOW = int(time.time() * 1000)


def msg(n, *, addr="+15550100", body=None, date=None, incoming=True, rid=None):
    return {"id": rid if rid is not None else n,
            "thread": 1,
            "address": addr,
            "body": body if body is not None else "message %d" % n,
            "date": date if date is not None else NOW - 60000 + n * 1000,
            "type": 1 if incoming else 2,
            "incoming": incoming,
            "read": False,
            "doc": "pcai:sms:%024d" % n}


def ev(d, payload, at=1000):
    return {"kind": 30078, "content": "enc:" + json.dumps(payload), "created_at": at,
            "pubkey": "me", "id": "x" + d, "tags": [["d", d], ["l", "pcai-sms"]]}


def run(**opts):
    out = subprocess.run(["node", str(SIM), json.dumps(opts)], capture_output=True, timeout=90)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-3000:])
    return json.loads(out.stdout.decode())


def calls_of(res, name):
    return [c for c in res["calls"] if c[0] == name]


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ColdArchiveFailures(unittest.TestCase):

    def test_firefox_signer_zero_and_oversize_records_do_not_block_texts(self):
        """NIP-44 signers reject decoded plaintext outside 1..65535 bytes. A bad record in either
        the first paint or detached history tail must be quarantined while valid siblings load, and
        the tail rejection must not escape as window.unhandledrejection / "action failed"."""
        cached = []
        for n in range(40):
            cached.append(ev("pcai:sms:valid-%02d" % n,
                             {"address": "+15550100", "body": "ok %d" % n,
                              "date": n + 1, "incoming": True}, at=1000 - n))
        cached[5]["id"] = "bad-zero"
        cached[5]["content"] = "enc:"
        cached[35]["id"] = "bad-oversize"
        cached[35]["content"] = "enc:" + ("x" * 65536)

        res = run(isPhone=False, cached=cached, relayEmpty=True,
                  rejectInvalidPlaintext=True, steps=["load", "settle"])
        self.assertEqual(len(res["docs"]), 38)
        self.assertTrue(any("ok 0" in bodies for bodies in
                            (thread["bodies"] for thread in res["threads"])))

    def test_fire_and_forget_texts_render_contains_signer_rejection(self):
        """app.js's lazy Texts callback does not await render(). Firefox turns a rejection crossing
        that boundary into the global action-failed handler, leaving the route unusable. Reproduce a
        signer plaintext-boundary failure during the cold cache transaction and require the public
        render promise to settle instead of reaching window.unhandledrejection."""
        res = run(isPhone=False, coldLoadSignerFailure=True, steps=["render", "settle"])
        self.assertEqual(res["docs"], [])
        self.assertEqual(calls_of(res, "storeQuery"),
                         [["storeQuery", "label"], ["storeQuery", "broad"]])

    def test_desktop_route_replaces_spinner_during_feed_ownership_handoff(self):
        """The route callback is authoritative for its first paint. PCOS ownership bookkeeping can
        lag that callback by one turn; treating it like a background repaint leaves the spinner even
        though the encrypted archive has loaded successfully."""
        cached = [ev("pcai:sms:visible", {"address": "+15550100", "body": "loaded",
                                          "date": 1, "incoming": True})]
        res = run(isPhone=False, cached=cached, relayEmpty=True, desktopOwnershipRace=True,
                  steps=["render", "settle"])
        self.assertIn("loaded", res["feedHtml"])
        self.assertNotIn('class="spinner"', res["feedHtml"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Mirror(unittest.TestCase):

    def test_the_phone_publishes_its_messages(self):
        res = run(rows=[msg(1), msg(2)], steps=["load", "mirror"])
        self.assertEqual(sorted(res["relay"]),
                         ["pcai:sms:%024d" % 1, "pcai:sms:%024d" % 2])

    def test_a_device_that_cannot_read_publishes_nothing(self):
        """Every device reads the archive; only a device that can read the phone's own message store
        writes it. A laptop republishing what it read would fight the handset over every message's
        newest version — and a laptop has no plugin, no permission, and so no way in."""
        res = run(isPhone=False, rows=[msg(1)], steps=["load", "mirror"])
        self.assertEqual(res["relay"], [])
        self.assertEqual(calls_of(res, "list"), [])

    def test_the_high_water_mark_only_moves_past_what_landed(self):
        """THE RULE. A relay that takes one message and then refuses must leave the mark at that
        message: the next mirror resumes from there. A mark advanced over the whole batch would skip
        the rest of somebody's history for ever, with nothing anywhere to say so."""
        rows = [msg(1), msg(2), msg(3)]
        res = run(rows=rows, refuseAfter=1, steps=["load", "mirror"])
        self.assertEqual(res["relay"], ["pcai:sms:%024d" % 1])
        self.assertEqual(res["hwm"], rows[0]["date"])

        # …and the next attempt, once the relay is taking writes again, picks up message 2.
        res = run(rows=rows, refuseAfter=1,
                  steps=["load", "mirror", "allow", "mirror"])
        self.assertEqual(sorted(res["relay"]), sorted(m["doc"] for m in rows))
        self.assertEqual(res["hwm"], rows[-1]["date"])

    def test_an_inline_archive_message_is_upgraded_to_encrypted_blossom_once(self):
        rows = [msg(1)]
        res = run(rows=rows,
                  relay=[ev(rows[0]["doc"], {"address": "+15550100", "body": "message 1",
                                             "date": rows[0]["date"], "incoming": True})],
                  steps=["load", "mirror"])
        upgrades = [p for p in res["published"] if p["kind"] == 30078]
        self.assertEqual(len(upgrades), 1)
        self.assertIn('"blob":', upgrades[0]["content"])

    def test_an_unreachable_relay_leaves_the_local_archive_alone(self):
        """The anti-wipe rule this codebase keeps relearning. On a laptop this copy is the only one —
        there is no system message store there to fall back on."""
        rows = [msg(1)]
        cached = [ev(rows[0]["doc"], {"address": "+15550100", "body": "message 1",
                                      "date": rows[0]["date"], "incoming": True})]
        res = run(isPhone=False, cached=cached, relayDown=True, steps=["load", "settle"])
        self.assertEqual(res["docs"], [rows[0]["doc"]])

    def test_an_empty_relay_answer_does_not_empty_the_archive_either(self):
        rows = [msg(1)]
        cached = [ev(rows[0]["doc"], {"address": "+15550100", "body": "message 1",
                                      "date": rows[0]["date"], "incoming": True})]
        res = run(isPhone=False, cached=cached, relayEmpty=True, steps=["load", "settle"])
        self.assertEqual(res["docs"], [rows[0]["doc"]])

    def test_first_open_skips_one_malformed_cache_record_and_renders_the_rest(self):
        """A damaged newest cache row used to reject render(), leaving Texts on its loading view.
        Closing and reopening retried the route, which is the exact user-visible workaround. The
        first open must isolate that row and paint the valid archive immediately."""
        good = ev("pcai:sms:first-open", {
            "address": "+15550100", "body": "visible on first open", "date": 2,
            "incoming": True,
        }, at=1999)
        malformed = {"kind": 30078, "content": "enc:{}", "created_at": 2000,
                     "pubkey": "me", "id": "bad", "tags": {"not": "an array"}}
        res = run(isPhone=False, cached=[malformed, good], relayEmpty=True,
                  steps=["concurrentRenderFocus", "settle"])
        self.assertEqual(res["docs"], ["pcai:sms:first-open"])
        self.assertEqual(calls_of(res, "storeQuery"), [["storeQuery", "label"]],
                         "cold route and focus started two competing archive loads")

    def test_old_phone_archive_loads_when_label_index_is_missing(self):
        """The d-address is authoritative. An older archive or broken generic-tag index must not
        turn a populated phone history into the desktop's no-SIM empty state."""
        text = ev("pcai:sms:older-phone", {
            "address": "+15550100", "body": "survives missing label index", "date": 2,
            "incoming": True,
        })
        unrelated = ev("pcai:note:not-texts", {"body": "private unrelated document"})
        res = run(isPhone=False, cached=[text, unrelated], relayEmpty=True,
                  missingLabelIndex=True, steps=["load", "settle"])
        self.assertEqual(res["docs"], ["pcai:sms:older-phone"])
        self.assertEqual(calls_of(res, "storeQuery"),
                         [["storeQuery", "label"], ["storeQuery", "broad"]])

    def test_one_malformed_cache_record_cannot_hide_the_older_media_tail(self):
        """The background cache drain used to abandon its remaining 128-row batch on one throw.
        Old MMS attachments sit behind newer texts, so this presented as complete recent history
        with old media missing on Web/OS."""
        cached = []
        expected = []
        for i in range(96):
            doc = "pcai:sms:tail-%03d" % i
            expected.append(doc)
            payload = {"address": "+15550100", "body": "message %d" % i,
                       "date": 1000 + i, "incoming": True}
            if i == 95:
                payload["att"] = [{"ct": "image/jpeg", "name": "old.jpg", "bytes": 123,
                                   "sha": "a" * 64, "thumb": "b" * 64}]
            cached.append(ev(doc, payload, at=3000 - i))
        cached.append({"kind": 30078, "content": "enc:{}", "created_at": 2960,
                       "pubkey": "me", "id": "bad-tail", "tags": {"not": "an array"}})
        res = run(isPhone=False, cached=cached, relayEmpty=True,
                  steps=["load", "settle"])
        self.assertEqual(res["docs"], sorted(expected))
        tail = next(t for t in res["threads"] if "pcai:sms:tail-095" in t["order"])
        self.assertEqual(tail["parts"][tail["order"].index("pcai:sms:tail-095")], 1)

    def test_one_person_written_two_ways_is_one_conversation(self):
        """The same rule the phone uses (SmsKeys.matchKey): the last seven digits. A thread that
        splits in two because one app writes `+1 555 010 4477` and another writes `5550104477` is a
        thread nobody can read."""
        cached = [
            ev("pcai:sms:a", {"address": "+1 555 010 4477", "body": "one", "date": 1, "incoming": True}),
            ev("pcai:sms:b", {"address": "5550104477", "body": "two", "date": 2, "incoming": False}),
        ]
        res = run(isPhone=False, cached=cached, relayEmpty=True, steps=["load", "settle"])
        self.assertEqual(len(res["threads"]), 1)
        self.assertEqual(res["threads"][0]["n"], 2)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Deleting(unittest.TestCase):

    def test_a_delete_removes_both_copies(self):
        rows = [msg(1), msg(2)]
        res = run(rows=rows, steps=["load", "mirror", "remove:" + rows[0]["doc"]])
        self.assertEqual(res["relay"], [rows[1]["doc"]], "the archive copy survived")
        self.assertEqual(res["rows"], [rows[1]["doc"]], "the phone's copy survived")
        # A tombstone at the same address AND a NIP-09 delete beside it. The tombstone is what makes
        # it gone for every client; the kind 5 is the polite half.
        self.assertIn({"kind": 30078, "d": rows[0]["doc"], "content": ""}, res["published"])
        self.assertTrue(any(p["kind"] == 5 for p in res["published"]))

    def test_the_phones_copy_goes_first(self):
        """ORDER, and it is the whole guard. Tombstone first and a failing provider delete leaves the
        message on the phone with no archive document — which the next mirror publishes straight back,
        so the delete undoes itself and reports success."""
        rows = [msg(1)]
        res = run(rows=rows, steps=["load", "mirror", "remove:" + rows[0]["doc"]])
        order = [c[0] for c in res["calls"]]
        seq = [p for p in res["published"] if p["d"] == rows[0]["doc"] and p["content"] == ""]
        self.assertTrue(seq, "no tombstone was published")
        self.assertIn("delete", order)

    def test_a_provider_delete_that_failed_does_not_tombstone_the_archive(self):
        """Otherwise the message is on the phone and gone from the archive, and the next mirror
        republishes it — a delete that quietly undoes itself."""
        rows = [msg(1)]
        res = run(rows=rows, deleteFails=True,
                  steps=["load", "mirror", "remove:" + rows[0]["doc"]])
        self.assertEqual(res["relay"], [rows[0]["doc"]], "the archive was tombstoned anyway")
        self.assertEqual(res["rows"], [rows[0]["doc"]])
        result = calls_of(res, "removeResult")[0]
        self.assertEqual(result[1], 0, "it claimed to have deleted the archive copy")

    def test_a_partial_provider_delete_does_not_tombstone_the_whole_archive(self):
        """Some OEMs accept the SMS URI and refuse MMS (or vice versa). A non-zero aggregate is
        not proof that every selected message disappeared; tombstoning all of them makes the
        survivor reappear on the next mirror after the UI claimed success."""
        rows = [msg(1), msg(2)]
        res = run(rows=rows, deleteLimit=1,
                  steps=["load", "mirror", "remove:" + rows[0]["doc"] + "," + rows[1]["doc"]])
        self.assertEqual(sorted(res["relay"]), sorted(r["doc"] for r in rows),
                         "a partial phone delete tombstoned the complete archive selection")
        result = calls_of(res, "removeResult")[0]
        self.assertEqual(result[1], 0, "a partial provider result claimed archive deletion")

    def test_a_tombstone_removes_the_message_everywhere_it_is_read(self):
        cached = [
            ev("pcai:sms:a", {"address": "+15550100", "body": "one", "date": 1, "incoming": True}, at=10),
            {"kind": 30078, "content": "", "created_at": 20, "pubkey": "me", "id": "t",
             "tags": [["d", "pcai:sms:a"], ["l", "pcai-sms"]]},
        ]
        res = run(isPhone=False, cached=cached, relayEmpty=True, steps=["load", "settle"])
        self.assertEqual(res["docs"], [])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class SendingFromAnotherDevice(unittest.TestCase):

    def test_foreground_and_background_drains_share_one_persistent_atomic_claim(self):
        """Opening Android must not let JS and its relay service transmit one request twice."""
        js = (ROOT / "static/js/client/sms.js").read_text()
        plugin = (ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java").read_text()
        outbox = (ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsOutbox.java").read_text()
        self.assertIn("outbox:d", js)
        self.assertIn("if(r && r.claimed === false) continue", js)
        self.assertIn("SmsOutbox.claim(getContext(), outbox)", plugin)
        self.assertIn("public static synchronized boolean claim", outbox)
        self.assertIn("getSharedPreferences(CLAIMS, Context.MODE_PRIVATE)", outbox)
        self.assertIn("if (!claim(ctx, doc)) return null", outbox)

    def test_a_success_marker_carries_the_sent_message_back_to_the_desktop(self):
        """A radio success with no address/body/time leaves the web thread blank forever."""
        src = (ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsOutbox.java").read_text()
        web = (ROOT / "static/js/client/sms.js").read_text()
        self.assertIn('o.put("to", to)', src)
        self.assertIn('o.put("body", body)', src)
        self.assertIn('o.put("at", asked)', src)
        self.assertIn("d.startsWith(D_OUT)", web)
        self.assertIn("ack.done && ack.ok", web)

    def test_deleting_a_pending_message_cancels_its_outbox_command(self):
        """Deleting a queued bubble must not leave a command that the phone sends later."""
        web = (ROOT / "static/js/client/sms.js").read_text()
        remove = web[web.index("async function remove(docs)"):web.index("async function askForRead")]
        self.assertIn("m.pending && m.outbox", remove)
        self.assertIn("cancelled:true", remove)
        self.assertIn("await mark(d", remove)
        self.assertIn("if(!cancelled) return", remove)

        res = run(isPhone=False, telephony=False,
                  steps=["load", "send:+15550100:not yet", "removePending"])
        cancelled = calls_of(res, "removePendingResult")[0]
        self.assertEqual(cancelled[3], 1)
        out = next(e for e in res["relayEvents"] if e["d"].startswith("pcai:smsout:"))
        self.assertIn('"cancelled":true', out["content"],
                      "the live outbox document was not replaced by a cancellation")
        self.assertEqual(res["docs"], [], "the pending placeholder is still visible")

    def test_cancel_receipt_is_not_rendered_as_a_failed_attachment(self):
        web = (ROOT / "static/js/client/sms.js").read_text()
        absorb = web[web.index("async function absorb(evs)"):web.index("async function publishOne")]
        cancel = absorb.index("ack && ack.done && ack.cancelled")
        decode = absorb.index("const sent =")
        self.assertLess(cancel, decode)
        self.assertIn("if(old && old.outbox === d)", absorb)

    def test_older_relay_request_cannot_resurrect_a_cancelled_send(self):
        """A relay pool may deliver the cancellation first and a lagging request on a later pass.
        Cancellation must remain a tombstone for that outbox id, not merely remove today's bubble."""
        doc = "pcai:smsout:cancel-watermark"
        cancel = {
            "kind": 30078, "created_at": 1700000020, "pubkey": "me",
            "tags": [["d", doc], ["l", "pcai-sms"]],
            "content": 'enc:{"done":true,"ok":false,"cancelled":true}',
        }
        stale = {
            "kind": 30078, "created_at": 1700000010, "pubkey": "me",
            "tags": [["d", doc], ["l", "pcai-sms"]],
            "content": 'enc:{"to":"+15550100","body":"do not send","at":1700000000000}',
        }
        res = run(isPhone=False, telephony=False, relay=[cancel], rawEvents=[stale],
                  steps=["load", "settle", "absorbRaw"])
        self.assertEqual(res["docs"], [])
        self.assertFalse(any(t["pending"] for t in res["threads"]))

    def test_live_cancellation_repaints_even_when_map_size_is_unchanged(self):
        web = (ROOT / "static/js/client/sms.js").read_text()
        watch = web[web.index("function watch()") : web.index("async function notifyNew")]
        repaint = watch.index("if(textsOnScreen()) paint()")
        size_gate = watch.index("if(S.msgs.size !== before)")
        self.assertLess(repaint, size_gate)

    def test_desktop_live_repaint_requires_the_texts_window_to_own_the_feed(self):
        web = (ROOT / "static/js/client/sms.js").read_text()
        visible = web[web.index("function textsOnScreen()") : web.index("function paint(force)")]
        self.assertIn("PCOS.ownsFeedView('texts')", visible)
        self.assertIn("PCOS.isOn()", visible)

    def test_returning_to_an_open_desktop_texts_window_catches_up(self):
        """PosterChanOS has no SMS plugin, so a phone-gated foreground handler returned before its
        relay query. Miss one live event during suspend/reconnect and only reopening Texts fixed it."""
        web = (ROOT / "static/js/client/sms.js").read_text()
        foreground = web[web.index("async function foreground()"):
                         web.index("document.addEventListener('visibilitychange', foreground)")]
        self.assertIn("await load()", foreground)
        self.assertIn("await refresh()", foreground)
        self.assertLess(foreground.index("await refresh()"), foreground.index("const st = await phoneState()"))
        self.assertNotIn("if(!st.canRead && !st.telephony) return", foreground)

    def test_window_focus_and_visibility_share_one_foreground_sync(self):
        web = (ROOT / "static/js/client/sms.js").read_text()
        self.assertIn("if(foregrounding) return foregrounding", web)
        self.assertIn("document.addEventListener('visibilitychange', foreground)", web)
        self.assertIn("if(window.addEventListener) window.addEventListener('focus', foreground)", web)

    def test_deleting_a_failed_remote_send_tombstones_its_source_receipt(self):
        web = (ROOT / "static/js/client/sms.js").read_text()
        remove = web[web.index("async function remove(docs)"):web.index("async function askForRead")]
        self.assertIn("selectedOutboxes", remove)
        self.assertIn("selectedOutboxes.has(m.outbox)", remove)
        self.assertIn("could not delete send receipt", remove)

    def test_failed_receipt_rebuilds_the_bubble_on_a_fresh_desktop(self):
        """The ask-time placeholder is local and may not exist after reload; the durable receipt
        must still show what failed instead of silently losing the message and its attachment."""
        receipt = {
            "kind": 30078,
            "created_at": 1700000010,
            "pubkey": "me",
            "tags": [["d", "pcai:smsout:failed-one"], ["l", "pcai-sms"]],
            "content": 'enc:{"done":true,"ok":false,"error":"radio rejected it",'
                       '"request":{"to":"+15550100","body":"photo",'
                       '"at":1700000000000,"attachment":{"sha":"' + "a" * 64 + '",'
                       '"mime":"image/jpeg","name":"photo.jpg","bytes":1234}}}',
        }
        res = run(isPhone=False, telephony=False, relay=[receipt], steps=["load"])
        self.assertEqual(len(res["threads"]), 1, "the failed send disappeared after reload")
        self.assertEqual(res["threads"][0]["n"], 1)
        self.assertEqual(res["threads"][0]["parts"], [1],
                         "the failed MMS came back without its attachment")

        web = (ROOT / "static/js/client/sms.js").read_text()
        absorb = web[web.index("async function absorb(evs)"):web.index("function whoIs")]
        self.assertIn("failed:true, outbox:d", absorb,
                      "the rebuilt bubble cannot delete its source receipt")

    def test_a_failed_remote_send_can_be_explicitly_retried_without_duplicating_its_receipt(self):
        """Retry is a deliberate new request.  The failed receipt must remain until that request
        is accepted, then disappear so reload cannot reconstruct both the failed and pending copy."""
        receipt = {
            "kind": 30078,
            "created_at": 1700000010,
            "pubkey": "me",
            "tags": [["d", "pcai:smsout:failed-retry"], ["l", "pcai-sms"]],
            "content": 'enc:{"done":true,"ok":false,"error":"radio rejected it",'
                       '"request":{"to":"+15550100","body":"try again",'
                       '"at":1700000000000}}',
        }
        res = run(isPhone=False, telephony=False, relay=[receipt],
                  steps=["load", "settle", "retryFailed"])
        self.assertEqual(calls_of(res, "retryFailedResult")[0][1:3], [True, "queued"])
        self.assertNotIn("pcai:smsout:failed-retry", res["relay"],
                         "the old failed receipt can recreate a ghost after retry")
        self.assertEqual(res["threads"][0]["failed"], [False])
        self.assertEqual(res["threads"][0]["pending"], [True])

    def test_web_retry_resolves_archived_attachment_through_encrypted_media_url(self):
        """The portable hash is useful only if Web/OS turns it back into browser-readable bytes.
        Exercise the same partData path used by old MMS hydration, without a handset provider."""
        sha = "a" * 64
        receipt = {
            "kind": 30078, "created_at": 1700000010, "pubkey": "me",
            "tags": [["d", "pcai:smsout:failed-media"], ["l", "pcai-sms"]],
            "content": 'enc:{"done":true,"ok":false,"error":"radio rejected it",'
                       '"request":{"to":"+15550100","body":"photo",'
                       '"at":1700000000000,"attachment":{"sha":"' + sha + '",'
                       '"mime":"image/jpeg","name":"old.jpg","bytes":1}}}',
        }
        res = run(isPhone=False, telephony=False, relay=[receipt],
                  uploads={sha: {"text": "x", "type": "image/jpeg"}},
                  steps=["render", "settle", "retryFailed"])
        self.assertEqual(calls_of(res, "retryFailedResult")[0][1:3], [True, "queued"])
        self.assertIn(["encFileUrl", sha], res["calls"])
        self.assertFalse(any(c[0] == "attachment" for c in res["calls"]),
                         "web/OS tried to read a handset-only provider part")

    def test_retry_never_repeats_an_ambiguous_carrier_outcome(self):
        receipt = {
            "kind": 30078,
            "created_at": 1700000010,
            "pubkey": "me",
            "tags": [["d", "pcai:smsout:unknown-retry"], ["l", "pcai-sms"]],
            "content": 'enc:{"done":true,"ok":false,"error":"delivery unknown: callback timed out",'
                       '"request":{"to":"+15550100","body":"maybe delivered",'
                       '"at":1700000000000}}',
        }
        res = run(isPhone=False, telephony=False, relay=[receipt],
                  steps=["load", "settle", "retryFailed"])
        retry = calls_of(res, "retryFailedResult")[0]
        self.assertEqual(retry[1:], [False, "this message is not safe to retry", ""])
        self.assertEqual(res["relay"], ["pcai:smsout:unknown-retry"])
        self.assertFalse(any(p["d"] != "pcai:smsout:unknown-retry"
                             and p["d"].startswith("pcai:smsout:")
                             for p in res["published"]))

    def test_texts_uses_the_instances_gif_picker(self):
        """The server-side picker keeps the connected instance's Giphy/Tenor key out of clients."""
        web = (ROOT / "static/js/client/sms.js").read_text()
        app = (ROOT / "static/js/client/app.js").read_text()
        self.assertIn('id="sms-gif"', web)
        self.assertIn("PC.gifPicker(input)", web)
        self.assertIn("gifEnabled: () => !!CFG.gif_enabled", app)
        picker = app[app.index("function gifPicker(ta)"):
                     app.index("function blossomPicker", app.index("function gifPicker(ta)"))]
        self.assertIn("const base=_instanceBase()", picker)
        self.assertIn("fetch(base+'/client/gif?q='", picker)
        self.assertIn("{credentials:'include'}", picker)
        self.assertNotIn("fetch('/client/gif", picker,
                         "the APK's local bundle origin can accidentally own GIF search")
        self.assertIn("Giphy or Tenor key", picker)

    def test_a_laptop_queues_a_request_and_says_so(self):
        """It cannot reach a radio. Reporting the message as sent would be a lie the person only
        discovers when the reply never comes.

        `telephony=False` is what makes this a LAPTOP. It used to rely on `isPhone=False`, which
        means "does not hold the SMS role" — a different thing, and true of plenty of phones. The
        distinction did not matter while sending was gated on the role; it decides everything now
        that a phone without the role sends its own texts (see the case below)."""
        res = run(isPhone=False, telephony=False, steps=["load", "send:+15550100:on my way"])
        result = calls_of(res, "sendResult")[0]
        self.assertEqual([result[1], result[2]], [True, "queued"])
        queued = next(p for p in res["published"] if p["d"].startswith("pcai:smsout:"))
        self.assertIn('"to":"+15550100"', queued["content"])
        self.assertIn('"body":"on my way"', queued["content"])
        self.assertNotIn('"blob":', queued["content"],
                         "Android cannot perform a web outbox command hidden behind Blossom")
        self.assertEqual(calls_of(res, "send"), [], "a laptop tried to use a radio")

    def test_a_phone_without_the_role_still_sends_its_own_text(self):
        """The reported bug: "POsterchan is not the this phones messaging app when i send message".

        A phone that has not been made the default has a radio and SEND_SMS; only WRITING the
        phone's own message store needs the role. Queuing the message as a request for "your phone"
        to perform — on the phone holding it — meant a text typed on the handset sat in a queue
        addressed to itself."""
        res = run(isPhone=False, telephony=True, steps=["load", "send:+15550100:on my way"])
        result = calls_of(res, "sendResult")[0]
        self.assertEqual(result[1], True)
        self.assertEqual(result[2], "phone", "a phone with a radio queued its own text for itself")
        self.assertTrue(calls_of(res, "send"), "the radio was never asked")
        self.assertFalse(any(p["d"].startswith("pcai:smsout:") for p in res["published"]),
                         "it published a request as well as sending")

    def test_the_phone_sends_it_and_marks_it_done(self):
        req = {"to": "+15550100", "body": "on my way", "at": None}
        res = run(rows=[], relay=[ev("pcai:smsout:abc", {"to": "+15550100", "body": "on my way",
                                                         "at": NOW - 60000})],
                  steps=["load", "drain"])
        self.assertEqual(calls_of(res, "send"), [["send", "+15550100", "on my way"]])
        marks = [p for p in res["published"] if p["d"] == "pcai:smsout:abc"]
        self.assertTrue(marks, "the request was performed and never marked")
        self.assertIn('"done":true', marks[-1]["content"])

    def test_a_failed_send_is_still_marked_done(self):
        """THE ONE THAT MATTERS. Retrying blindly means the phone performs it again on every drain,
        and there is no undo for a text that went out."""
        res = run(sendFails=True,
                  relay=[ev("pcai:smsout:abc", {"to": "+15550100", "body": "hi",
                                                "at": NOW - 60000})],
                  steps=["load", "drain"])
        marks = [p for p in res["published"] if p["d"] == "pcai:smsout:abc"]
        self.assertTrue(marks)
        self.assertIn('"done":true', marks[-1]["content"])
        self.assertIn('"ok":false', marks[-1]["content"])

    def test_a_request_already_marked_done_is_not_performed_again(self):
        res = run(relay=[ev("pcai:smsout:abc", {"to": "+15550100", "body": "hi",
                                                "at": NOW - 60000, "done": True})],
                  steps=["load", "drain"])
        self.assertEqual(calls_of(res, "send"), [])

    def test_a_stale_request_is_dropped_rather_than_sent(self):
        """A phone that was off for a week must not wake up and deliver a week of messages whose
        moment has passed."""
        res = run(relay=[ev("pcai:smsout:old", {"to": "+15550100", "body": "running late",
                                                "at": NOW - 3 * DAY})],
                  # A second client observes the terminal relay replacement on its next foreground
                  # refresh. `settle` lets load's intentionally backgrounded initial refresh finish.
                  steps=["load", "drain", "settle", "foreground", "settle"])
        self.assertEqual(calls_of(res, "send"), [])
        marks = [p for p in res["published"] if p["d"] == "pcai:smsout:old"]
        self.assertTrue(marks)
        self.assertIn("too old", marks[-1]["content"])
        self.assertIn('"request":', marks[-1]["content"],
                      "the terminal receipt cannot identify the pending bubble")
        self.assertEqual(res["threads"][0]["pending"], [False],
                         "the expired request remained stuck at sending")
        self.assertEqual(res["threads"][0]["failed"], [True],
                         "the safely dropped request was hidden instead of made actionable")

    def test_a_device_with_no_radio_never_drains(self):
        """`telephony=False` is what makes this a laptop. It used to say `isPhone=False`, which
        means "does not hold the SMS role" — a different thing, and true of plenty of phones,
        including one where Android granted the role but the message store's default-app row still
        names another app. Performing a send needs a RADIO; gated on the role, a laptop's request
        sat unperformed on a handset perfectly able to send it."""
        res = run(isPhone=False, telephony=False,
                  relay=[ev("pcai:smsout:abc", {"to": "+15550100", "body": "hi",
                                                "at": NOW - 60000})],
                  steps=["load", "drain"])
        self.assertEqual(calls_of(res, "send"), [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Permission(unittest.TestCase):
    """READING SOMEBODY'S TEXTS NEEDS READ_SMS, AND NOTHING EVER ASKED FOR IT.

    "still missing a nice sms app on android", after "i see 0 of my sms messages in Text".

    A dangerous permission is not granted by being declared in a manifest, and it is not granted by
    holding the default-SMS role either — those are two separate switches, and Android offers only
    the second one on its own. `SmsPlugin`'s `@CapacitorPlugin(permissions = ...)` block names the
    permissions its "sms" alias covers; it does not request them, and no call anywhere did. So every
    provider read was refused, `SmsStore.query` turned the refusal into an empty list, and the Texts
    screen said "No messages on this phone" over a full inbox.

    Underneath that sat the same circularity the Messages tile had: reading was gated on `isPhone()`,
    the default-SMS ROLE. A person trying the app out can be allowed to read their texts long before
    they hand over their messaging — and the screen told them the opposite in a sentence that was
    simply untrue.

    Each assertion below was checked to fail with its rule removed.
    """

    def test_a_phone_that_has_not_been_asked_says_so_and_offers_the_ask(self):
        """The one kind of empty a tap can fix is the one that was never named. Three kinds of empty
        were one sentence; the permission is the only one the person reading it can do anything
        about, so it comes first and it comes with a button."""
        res = run(rows=[msg(1)], canRead=False, steps=["load", "why"])
        why = calls_of(res, "why")[0]
        self.assertEqual(why[1], "perm", why)
        self.assertIn("allowed to read", why[2])

    def test_the_ask_actually_happens_and_the_messages_arrive(self):
        """The whole bug in one line: with the grant, the phone's own inbox is read and shown."""
        res = run(rows=[msg(1), msg(2)], canRead=False, grantOnAsk=True, steps=["render", "settle"])
        self.assertTrue(calls_of(res, "ensureRead"), res["calls"])
        self.assertEqual(sorted(res["docs"]),
                         ["pcai:sms:%024d" % 1, "pcai:sms:%024d" % 2])

    def test_reading_is_not_gated_on_being_the_default_sms_app(self):
        """A phone that may READ shows its messages whether or not it RECEIVES them. Publishing the
        archive does not need the role: it reads the provider and publishes encrypted account data.
        Only writing back into Android's provider needs the default-SMS role."""
        res = run(rows=[msg(1)], isPhone=False, canRead=True, steps=["render", "settle"])
        self.assertEqual(res["docs"], ["pcai:sms:%024d" % 1])
        self.assertEqual(res["relay"], ["pcai:sms:%024d" % 1])

    def test_a_refusal_is_not_retried_into_a_wall(self):
        """Declining leaves the screen saying what is missing rather than an empty list — and the
        messages stay unread, which is the honest outcome."""
        res = run(rows=[msg(1)], canRead=False, grantOnAsk=False, steps=["render", "settle", "why"])
        self.assertTrue(calls_of(res, "ensureRead"), res["calls"])
        self.assertEqual(res["docs"], [])
        self.assertEqual(calls_of(res, "why")[0][1], "perm")

    def test_an_older_apk_is_not_locked_out_by_a_method_it_does_not_have(self):
        """`status` with no `canRead` is a build from before this existed, where reading was gated on
        the role. Reading `undefined` as "not allowed" would hide a working screen behind a button
        that cannot do anything, on every APK already installed."""
        res = run(rows=[msg(1)], oldApk=True, steps=["render", "settle"])
        self.assertEqual(res["docs"], ["pcai:sms:%024d" % 1])
        self.assertEqual(calls_of(res, "ensureRead"), [])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PublishingIsNotTheRole(unittest.TestCase):
    """"phone conversations not on the other posterchan apps either" — from a handset whose own Texts
    screen was showing every message.

    The archive exists to get THIS device's messages to the devices that cannot read a SIM, and what
    makes a device able to do that is READ_SMS. The ROLE decides whether new messages arrive here and
    whether a send another device asked for may be performed; neither of those is publishing. Gated
    on the role, a phone that had granted the permission and not handed over its messaging published
    nothing, for ever, with a full inbox in front of the person and nothing anywhere to say why.
    """

    def test_a_phone_that_may_read_publishes_even_without_the_role(self):
        res = run(rows=[msg(1), msg(2)], isPhone=False, canRead=True, steps=["load", "mirror"])
        self.assertEqual(sorted(res["relay"]),
                         ["pcai:sms:%024d" % 1, "pcai:sms:%024d" % 2])

    def test_the_notice_names_who_android_named(self):
        """A bare verdict is unanswerable — a role never granted, a role in another profile and a
        device with no telephony all read the same. The package Android reports is the measurement
        the verdict comes from, so quoting it cannot contradict it."""
        res = run(rows=[], isPhone=False, canRead=True,
                  defaultPkg="com.google.android.apps.messaging", steps=["load", "why"])
        why = calls_of(res, "why")[0]
        self.assertEqual(why[1], "role", why)
        self.assertIn("com.google.android.apps.messaging", why[2])

    def test_a_device_with_no_sim_is_not_told_to_set_a_messages_app(self):
        """Advice somebody cannot take is worse than none: a tablet cannot be an SMS app."""
        res = run(rows=[], isPhone=False, canRead=True, telephony=False,
                  defaultPkg="", steps=["load", "why"])
        self.assertIn("no SIM", calls_of(res, "why")[0][2])

    def test_a_role_the_provider_disagrees_with_is_named_as_such(self):
        """"posterchan still not working as default Messenger app despite being set as default
        messenger". Android keeps the SMS ROLE and the message store's default-app row in two
        different tables, and on some builds granting the role does not move the row. The row is the
        one that decides what is delivered, so the app must not simply believe the role — but "you
        are not the default" to somebody who has just set it is unanswerable, and this is not."""
        res = run(rows=[], isPhone=False, canRead=True, roleHeld=True,
                  defaultPkg="com.samsung.android.messaging", steps=["load", "why"])
        why = calls_of(res, "why")[0][2]
        self.assertIn("messages role", why)
        self.assertIn("com.samsung.android.messaging", why)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TheRoleCanBeAskedForFromTheScreenThatNeedsIt(unittest.TestCase):
    """"posterchan still not working as default Messenger app despite being set as default
    messenger" — three times, against a screen whose only advice was to go and do it again in
    Android's own settings.

    `fix: 'role'` printed a sentence and offered NOTHING. Every other kind of empty on this screen
    has a button; this one, the one people were actually hitting, had none. Android's role dialog is
    one plugin call away and this is the screen somebody is standing on when they want it.

    It was also gated on the list being EMPTY, so the people reporting it — who can see their texts
    and cannot receive new ones — were the exact set the offer never reached.
    """

    def test_a_phone_that_is_not_the_default_is_offered_the_dialog(self):
        res = run(rows=[msg(1)], isPhone=False, canRead=True,
                  defaultPkg="com.samsung.android.messaging", steps=["load", "why"])
        self.assertEqual(calls_of(res, "why")[0][1], "role")

    def test_it_is_offered_even_when_there_are_messages_on_screen(self):
        """THE RULE THE REPORT TURNS ON. Being able to READ texts and being the app that RECEIVES
        them are different states, and somebody in the first one has a full screen of messages."""
        # `render` is what actually reads the phone's own inbox, so the screen really is holding
        # messages when the question is asked. Without it S.msgs is empty either way and the test
        # cannot tell the gate from its absence — it passed against the bug.
        res = run(rows=[msg(1), msg(2), msg(3)], isPhone=False, canRead=True,
                  defaultPkg="com.google.android.apps.messaging",
                  steps=["render", "settle", "why"])
        self.assertEqual(len(res["docs"]), 3, "the screen is not holding any messages: %s" % res)
        self.assertEqual(calls_of(res, "why")[0][1], "role")

    def test_the_button_exists_and_names_the_right_plugin(self):
        js = (ROOT / "static/js/client/sms.js").read_text()
        self.assertIn("sms-role", js, "there is no button to ask for the role")
        self.assertIn("'HomeScreen', 'requestSms'", js,
                      "the role dialog belongs to the home-screen plugin; asking the Sms plugin "
                      "returns a proxy that answers every name and then rejects")
        self.assertIn("openDefaultApps", js,
                      "no way through on an OEM build that suppresses the role dialog")
