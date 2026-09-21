"""A CARRIER REFUSAL OF A PICTURE MESSAGE IS SAID, NOT SWALLOWED.

`sendMms` resolves when Android ACCEPTS a transaction. Whether the carrier then took it arrives
seconds later on the plugin's `smsSent` event — and nothing in the client listened to that event,
so the composer toasted "sent", the picture left the composer, and a refusal only ever showed up as
a quietly different bubble on some later provider read. These tests drive the SHIPPED sms.js in
tests/client/sms_sim.js with the event channel switched on.

Run: venv-unified/bin/python -m pytest tests/client/test_sms_send_result_is_shown.py -q
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "sms_sim.js")
PLUGIN = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster",
                      "app", "sms", "SmsPlugin.java")
NODE = shutil.which("node")


def run(**opts):
    opts.setdefault("canRead", True)
    opts.setdefault("nativeEvents", True)
    r = subprocess.run([NODE, SIM, json.dumps(opts)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def calls_of(res, name):
    return [c for c in res["calls"] if c[0] == name]


@unittest.skipIf(not NODE, "no node on this node")
class CarrierVerdictIsShown(unittest.TestCase):

    def test_the_client_listens_for_the_carriers_verdict(self):
        res = run(steps=["load"])
        self.assertIn(["addListener", "smsSent"], res["calls"],
                      "nothing subscribes to smsSent: a carrier refusal can never reach the screen")

    def test_a_refused_picture_message_is_toasted_with_the_phones_reason(self):
        verdict = {"row": "content://mms/41", "ok": False, "code": 8, "mms": True,
                   "error": "mobile data is unavailable"}
        res = run(steps=["load", "nativeSent:" + json.dumps(verdict)])
        toasts = [c[1] for c in calls_of(res, "toast")]
        self.assertIn("Picture message not sent: mobile data is unavailable", toasts)

    def test_a_refusal_rereads_the_phone_so_the_failed_row_appears(self):
        verdict = {"row": "content://mms/41", "ok": False, "code": 4, "mms": True}
        res = run(steps=["load", "nativeSent:" + json.dumps(verdict)])
        after = res["calls"][res["calls"].index(["addListener", "smsSent"]):]
        self.assertTrue([c for c in after if c[0] == "list"],
                        "the provider was not re-read after the carrier's answer")
        toasts = [c[1] for c in calls_of(res, "toast")]
        self.assertIn("Picture message not sent: the carrier refused it (code 4)", toasts)

    def test_a_success_says_nothing_extra(self):
        verdict = {"row": "content://mms/41", "ok": True, "code": -1, "mms": True}
        res = run(steps=["load", "nativeSent:" + json.dumps(verdict)])
        self.assertFalse([c for c in calls_of(res, "toast") if "not sent" in c[1]])

    def test_the_native_side_carries_the_reason_for_a_picture_message(self):
        src = open(PLUGIN, encoding="utf-8").read()
        body = src[src.index("static void onSendResult"):]
        body = body[:body.index("p.notifyListeners(\"smsSent\", o);")]
        self.assertIn('o.put("mms", mms)', body)
        self.assertIn('MmsFailures.reason(p.getContext(), code, 0)', body)


if __name__ == "__main__":
    unittest.main()
