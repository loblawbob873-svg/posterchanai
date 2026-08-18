"""nostr-mail (spec v0.2.0-draft): email that stays email, bodies between Nostr keys.

The codec is LIFTED and RUN — armor parsing is exactly where a reader and a spec quietly disagree
(legacy tags, `> ` quote prefixes, npub vs hex pubkeys) — and the v1 boundaries are pinned as
boundaries: NIP-44 both ways; signed plaintext shown with "signature present" and never VERIFIED;
NIP-04 refused outright, quoting the spec's own verify-then-decrypt rule (§4.1), because decrypting
what cannot be verified first is the padding-oracle window that rule exists for.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _lift():
    src = open(APP, encoding="utf-8").read()
    at = src.index("const NMail = {")
    i = src.index("{", at)
    d = 0
    j = i
    while j < len(src):
        if src[j] == "{": d += 1
        elif src[j] == "}":
            d -= 1
            if not d: break
        j += 1
    return src[at:j + 1] + ";"


@unittest.skipIf(not NODE, "no node on this node")
class CodecTests(unittest.TestCase):
    def _run(self, body):
        js = """
        const NT = () => ({ nip19: { decode(s){ if(!/^npub1/.test(s)) throw new Error('bad');
            return { type:'npub', data: 'ab'.repeat(32) }; },
          npubEncode(){ return 'npub1self'; } } });
        %s
        %s
        """ % (_lift(), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_a_sealed_nip44_message_parses(self):
        got = self._run("""
          const t = ['hello outside', '',
            '----- BEGIN NOSTR NIP-44 ENCRYPTED BODY -----',
            'QUJDREVG',
            '----- BEGIN NOSTR SEAL -----',
            '@Alice',
            '%s',
            '----- END NOSTR MESSAGE -----'].join('\\n');
          process.stdout.write(JSON.stringify(NMail.parse(t)));
        """ % ("ab" * 32))
        self.assertEqual(got["kind"], "nip44")
        self.assertEqual(got["cipher"], "QUJDREVG")
        self.assertEqual(got["pubkey"], "ab" * 32)
        self.assertEqual(got["name"], "Alice")
        self.assertFalse(got["signed"])
        self.assertEqual(got["plainAbove"], "hello outside")

    def test_npub_and_quote_prefixes_and_legacy_tags(self):
        got = self._run("""
          const t = ['> ----- BEGIN NOSTR NIP-44 ENCRYPTED MESSAGE -----',
            '> QUJD',
            '> ----- BEGIN NOSTR SEAL -----',
            '> @Bob',
            '> npub1abcdefghijklmnopqrst',
            '> ----- END NOSTR NIP-44 ENCRYPTED MESSAGE -----'].join('\\n');
          process.stdout.write(JSON.stringify(NMail.parse(t)));
        """)
        self.assertEqual(got["kind"], "nip44")
        self.assertEqual(got["cipher"], "QUJD")
        self.assertEqual(got["pubkey"], "ab" * 32, "npub in a quoted legacy block was not read")

    def test_nip04_is_recognised_so_the_ui_can_refuse_it(self):
        got = self._run("""
          const t = ['----- BEGIN NOSTR NIP-04 ENCRYPTED BODY -----', 'QUJD',
            '----- BEGIN NOSTR SIGNATURE -----', '@Eve', '%s', '%s',
            '----- END NOSTR MESSAGE -----'].join('\\n');
          process.stdout.write(JSON.stringify(NMail.parse(t)));
        """ % ("cd" * 64, "ab" * 32))
        self.assertEqual(got["kind"], "nip04")
        self.assertTrue(got["signed"])

    def test_ordinary_mail_is_not_nostr_mail(self):
        got = self._run("""
          process.stdout.write(JSON.stringify([
            NMail.parse('just an ordinary email about BEGIN NOSTR nothing'),
            NMail.parse(''), NMail.parse(null)]));
        """)
        self.assertEqual(got, [None, None, None])

    def test_the_armor_round_trips_through_the_parser(self):
        got = self._run("""
          const a = NMail.armor('QUJDREVGSElK'.repeat(20), 'npub1abcdefghijklmnopqrst', 'Dustin');
          const back = NMail.parse('some plaintext\\n\\n' + a);
          process.stdout.write(JSON.stringify({ back, hasNew: /BEGIN NOSTR NIP-44 ENCRYPTED BODY/.test(a),
            hasLegacy: /ENCRYPTED MESSAGE/.test(a) }));
        """)
        self.assertEqual(got["back"]["kind"], "nip44")
        self.assertEqual(got["back"]["cipher"], "QUJDREVGSElK" * 20)
        self.assertEqual(got["back"]["name"], "Dustin")
        self.assertTrue(got["hasNew"])
        self.assertFalse(got["hasLegacy"], "encoders MUST produce only the new format")


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(APP, encoding="utf-8").read()

    def test_nip04_is_refused_with_the_specs_reason(self):
        at = self.src.index("_nmailHtml(nm, m){")
        body = self.src[at:at + 3000]
        self.assertIn("nip04", body)
        self.assertIn("will not decrypt", body)
        self.assertIn("4.1", body, "the refusal does not cite the rule it enforces")

    def test_no_verified_claim_is_ever_made(self):
        at = self.src.index("_nmailHtml(nm, m){")
        body = self.src[at:at + 3000]
        self.assertIn("not verified here yet", body)

    def test_decryption_goes_through_the_signer_binding(self):
        at = self.src.index("async _nmailReveal(")
        body = self.src[at:at + 1200]
        self.assertIn("signer.nip44dec", body)
        self.assertNotIn("PC.nip44dec", body, "the phantom PC binding, a third time")

    def test_the_dm_doorbell_is_optional_after_send_and_never_reaches_the_server(self):
        """The spec's optional notification: the subject as a Nostr DM. It fires only AFTER the
        mail is accepted (the email is the message, the DM is a doorbell), its checkbox can turn it
        off, and the recipient pubkey is stripped from the payload before the POST — the server has
        no business learning which npub a mail was encrypted to."""
        seg = self.src[self.src.index("cm-send').onclick"):][:3200]
        self.assertIn("delete payload._nmailDm", seg)
        self.assertLess(seg.index("delete payload._nmailDm"), seg.index("self.api(path"),
                        "the recipient npub travels to the server in the send payload")
        self.assertLess(seg.index("if(r.ok)"), seg.index("sendDm("),
                        "the DM fires even when the mail was refused")
        self.assertIn("cm-nmail-dm", self.src)

    def test_sending_produces_the_new_format_through_the_signer(self):
        at = self.src.index("cm-nmail-row")
        seg = self.src[self.src.index("cm-send').onclick"):][:2200]
        self.assertIn("signer.nip44enc", seg)
        self.assertIn("NMail.armor", seg)


if __name__ == "__main__":
    unittest.main()
