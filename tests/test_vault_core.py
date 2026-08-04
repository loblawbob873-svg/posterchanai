"""The password vault's shared core (static/js/client/vaultcore.js), run under node.

Run: venv-unified/bin/python -m unittest tests.test_vault_core

This file is shared VERBATIM by the web client, the Firefox extension and (through the web layer)
the Android autofill snapshot, so a bug here is a bug in three places at once — and in the two that
are hardest to look at while they are wrong. What it has to get right:

  TOTP        against the RFC 6238 test vectors, not against itself. An implementation checked only
              for self-consistency is exactly the one that ships an off-by-one counter or a 32-bit
              truncation of the 64-bit counter and generates codes that work nowhere. SHA1, SHA256
              and SHA512, including a timestamp past 2**31 seconds, which is where a naive
              `counter >>> 32` silently folds the high word onto the low one.
  generator   uniform over the alphabet (no `% len` bias, which measurably weakens every password),
              and every enabled character class actually present — sites reject a password for
              missing one, and a generator you have to run five times is one people stop using.
  matching    a credential is offered on the right site and NOT on a lookalike. `exact` for the same
              host, `domain` for the same registrable domain (which the UI may suggest but must
              never silently fill), nothing for paypal.com.evil.com.
  Bitwarden   a real export imports; an ENCRYPTED export is refused loudly rather than imported as a
              wall of blank entries (the same call joplin.js makes about an E2EE Joplin export);
              a CSV with quoted commas and embedded newlines parses.
  sealing     round-trips, and a wrong key FAILS rather than returning garbage.
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO, "static", "js", "client", "vaultcore.js")


def _node(body, fixtures=None):
    """Run `body` with V = the core module and F = the fixtures, printing a JSON result."""
    js = (
        "const V = require(%s);\n" % json.dumps(CORE)
        + "const F = %s;\n" % json.dumps(fixtures or {})
        + "(async () => { const out = await (async () => {\n" + body + "\n})();"
        + " console.log(JSON.stringify(out)); })()"
        + ".catch(e => { console.log(JSON.stringify({__error: String(e && e.message || e)})); });"
    )
    p = subprocess.run([shutil.which("node") or "node", "-e", js],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr.strip()[:800] or "?"))
    return json.loads(p.stdout.strip().splitlines()[-1])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Totp(unittest.TestCase):
    # RFC 6238 Appendix B. The seeds are the ASCII strings "12345678901234567890" (repeated to the
    # hash's block size), base32-encoded here because that is the form a user ever pastes.
    SEED1 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"                                          # SHA1,   20 bytes
    SEED256 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA"                    # SHA256, 32 bytes
    SEED512 = ("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
               "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNA=")                              # SHA512, 64 bytes

    def _codes(self, secret, algorithm, times):
        return _node(
            "const out = [];"
            "for (const t of F.times) out.push(await V.totp(F.secret, "
            "  {algorithm: F.algorithm, digits: 8, at: t}));"
            "return out;",
            {"secret": secret, "algorithm": algorithm, "times": times},
        )

    def test_rfc6238_sha1(self):
        """The vector at 2000000000 is past 2**31: a counter built with 32-bit shifts folds the high
        word onto the low one and produces a confidently wrong code."""
        times = [59, 1111111109, 1111111111, 1234567890, 2000000000, 20000000000]
        want = ["94287082", "07081804", "14050471", "89005924", "69279037", "65353130"]
        self.assertEqual(self._codes(self.SEED1, "SHA1", times), want)

    def test_rfc6238_sha256(self):
        times = [59, 1111111109, 1234567890, 2000000000, 20000000000]
        want = ["46119246", "68084774", "91819424", "90698825", "77737706"]
        self.assertEqual(self._codes(self.SEED256, "SHA256", times), want)

    def test_rfc6238_sha512(self):
        times = [59, 1111111109, 1234567890, 2000000000, 20000000000]
        want = ["90693936", "25091201", "93441116", "38618901", "47863826"]
        self.assertEqual(self._codes(self.SEED512, "SHA512", times), want)

    def test_six_digits_are_the_last_six(self):
        got = _node("return await V.totp(F.s, {at: 59, digits: 6});", {"s": self.SEED1})
        self.assertEqual(got, "287082")

    def test_a_pasted_secret_survives_spaces_and_lowercase(self):
        """What sites actually print under the QR code, and what a phone keyboard produces."""
        got = _node("return [await V.totp(F.a, {at: 59, digits: 8}),"
                    "        await V.totp(F.b, {at: 59, digits: 8})];",
                    {"a": "gezd gnbv gy3t qojq gezd gnbv gy3t qojq", "b": self.SEED1})
        self.assertEqual(got[0], got[1])

    def test_a_bad_secret_is_an_error_not_a_wrong_code(self):
        got = _node("try { await V.totp('not base 32!!'); return 'no error'; }"
                    "catch (e) { return 'threw'; }")
        self.assertEqual(got, "threw")

    def test_otpauth_uri_is_parsed(self):
        got = _node("return V.totpConfig(F.u);", {
            "u": "otpauth://totp/GitHub:you%40example.com?secret=" + self.SEED1 +
                 "&issuer=GitHub&digits=8&period=60&algorithm=SHA256"})
        self.assertEqual(got["digits"], 8)
        self.assertEqual(got["period"], 60)
        self.assertEqual(got["algorithm"], "SHA256")
        self.assertEqual(got["secret"], self.SEED1)

    def test_a_bare_secret_gets_the_defaults(self):
        got = _node("return V.totpConfig(F.s);", {"s": self.SEED1})
        self.assertEqual((got["digits"], got["period"], got["algorithm"]), (6, 30, "SHA1"))

    def test_unparseable_totp_is_null_not_a_broken_entry(self):
        self.assertIsNone(_node("return V.totpConfig('hello world 1');"))

    def test_countdown(self):
        self.assertEqual(_node("return V.totpRemaining(30, 100);"), 20)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Generator(unittest.TestCase):
    def test_every_enabled_class_appears(self):
        """A site that demands a symbol rejects the password, and the user regenerates until it
        happens to contain one. Guarantee it instead."""
        got = _node(
            "const bad = [];"
            "for (let i = 0; i < 200; i++) {"
            "  const p = V.generate({length: 8, lower: true, upper: true, digits: true, symbols: true});"
            "  if (p.length !== 8) bad.push('length ' + p.length);"
            "  if (!/[a-z]/.test(p) || !/[A-Z]/.test(p) || !/[0-9]/.test(p) || !/[^a-zA-Z0-9]/.test(p))"
            "    bad.push(p);"
            "} return bad;")
        self.assertEqual(got, [])

    def test_length_is_honoured_and_bounded(self):
        got = _node("return [V.generate({length: 64}).length, V.generate({length: 999}).length,"
                    "        V.generate({length: 1, lower: true, upper: true, digits: true, symbols: true}).length];")
        # A length below the number of enabled classes is raised to fit them, never silently dropped.
        self.assertEqual(got, [64, 128, 4])

    def test_disabled_classes_are_absent(self):
        got = _node("const p = V.generate({length: 40, lower: true, upper: false, digits: false, symbols: false});"
                    "return /^[a-z]+$/.test(p);")
        self.assertTrue(got)

    def test_ambiguous_characters_can_be_excluded(self):
        got = _node("for (let i = 0; i < 100; i++) {"
                    "  const p = V.generate({length: 40, avoidAmbiguous: true});"
                    "  if (/[1lI0O]/.test(p)) return p;"
                    "} return '';")
        self.assertEqual(got, "")

    def test_no_character_sets_is_an_error(self):
        got = _node("try { V.generate({lower: false, upper: false, digits: false, symbols: false});"
                    "      return 'no error'; } catch (e) { return 'threw'; }")
        self.assertEqual(got, "threw")

    def test_the_alphabet_is_uniform(self):
        """`% len` biases toward the first (256 % len) characters. Over 60k draws from a 26-letter
        alphabet that bias is unmissable; true uniformity keeps every count near the mean."""
        got = _node(
            "const counts = new Map();"
            "for (let i = 0; i < 60000; i++) { const c = V.randInt(26); counts.set(c, (counts.get(c)||0)+1); }"
            "const vals = [...counts.values()]; const mean = 60000/26;"
            "return { n: counts.size, worst: Math.max(...vals.map(v => Math.abs(v-mean)/mean)) };")
        self.assertEqual(got["n"], 26)
        self.assertLess(got["worst"], 0.12)

    def test_entropy_is_reported_honestly(self):
        got = _node("return [V.entropyBits({length: 20}), V.entropyBits({length: 4, lower: true,"
                    " upper: false, digits: false, symbols: false})];")
        self.assertGreater(got[0], 120)          # 20 chars over ~85 symbols
        self.assertEqual(got[1], 19)             # 4 * log2(26)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Matching(unittest.TestCase):
    ITEMS = [
        {"id": "a", "kind": "login", "title": "GitHub", "username": "me",
         "uris": ["https://github.com/login"], "updated": 100},
        {"id": "b", "kind": "login", "title": "GitHub work", "username": "work",
         "uris": ["https://github.com"], "updated": 200},
        {"id": "c", "kind": "login", "title": "Gist", "username": "me",
         "uris": ["https://gist.github.com"], "updated": 300},
        {"id": "d", "kind": "login", "title": "Bank", "username": "me",
         "uris": ["https://hsbc.co.uk"], "updated": 400},
    ]

    def _lvl(self, uris, page):
        return _node("return V.matchLevel({uris: F.uris}, F.page);", {"uris": uris, "page": page})

    def test_exact_host(self):
        self.assertEqual(self._lvl(["https://github.com/login"], "https://github.com/settings"), "exact")

    def test_www_is_the_same_host(self):
        self.assertEqual(self._lvl(["https://www.github.com"], "https://github.com/x"), "exact")

    def test_a_subdomain_is_only_a_domain_match(self):
        """Suggestable, never silently fillable — the UI has to keep that distinction."""
        self.assertEqual(self._lvl(["https://gist.github.com"], "https://github.com"), "domain")

    def test_a_lookalike_domain_does_not_match(self):
        """The attack this function exists to refuse."""
        self.assertEqual(self._lvl(["https://paypal.com"], "https://paypal.com.evil.com/login"), "")
        self.assertEqual(self._lvl(["https://paypal.com"], "https://paypa1.com"), "")

    def test_multi_label_suffixes_are_not_one_site(self):
        """Without a public-suffix rule, hsbc.co.uk and barclays.co.uk both reduce to 'co.uk' and
        every British bank becomes the same site."""
        self.assertEqual(self._lvl(["https://hsbc.co.uk"], "https://barclays.co.uk"), "")
        self.assertEqual(self._lvl(["https://login.hsbc.co.uk"], "https://hsbc.co.uk"), "domain")

    def test_ip_addresses_are_whole_hosts(self):
        self.assertEqual(self._lvl(["http://192.168.0.10:8080"], "http://192.168.0.10/admin"), "exact")
        self.assertEqual(self._lvl(["http://192.168.0.10"], "http://192.168.0.11"), "")

    def test_matches_are_ranked_exact_first_then_recent(self):
        got = _node("return V.matchesFor(F.items, 'https://github.com/login')"
                    "        .map(i => i.id + ':' + i._match);", {"items": self.ITEMS})
        self.assertEqual(got, ["b:exact", "a:exact", "c:domain"])

    def test_nothing_matches_an_unrelated_site(self):
        got = _node("return V.matchesFor(F.items, 'https://example.org');", {"items": self.ITEMS})
        self.assertEqual(got, [])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Sealing(unittest.TestCase):
    def test_round_trip(self):
        got = _node("const k = V.newVaultKey();"
                    "const b = await V.seal(k, {password: 'hunter2', n: 5});"
                    "const o = await V.open(k, b);"
                    "return [o.password, o.n, b.length > 20];")
        self.assertEqual(got, ["hunter2", 5, True])

    def test_the_wrong_key_fails_rather_than_returning_garbage(self):
        got = _node("const b = await V.seal(V.newVaultKey(), {password: 'hunter2'});"
                    "try { await V.open(V.newVaultKey(), b); return 'opened'; }"
                    "catch (e) { return 'threw'; }")
        self.assertEqual(got, "threw")

    def test_the_same_value_seals_differently_each_time(self):
        """A deterministic IV would leak that an unchanged password had been re-saved."""
        got = _node("const k = V.newVaultKey();"
                    "return (await V.seal(k, {p: 1})) === (await V.seal(k, {p: 1}));")
        self.assertFalse(got)

    def test_the_ciphertext_does_not_contain_the_plaintext(self):
        got = _node("const b = await V.seal(V.newVaultKey(), {password: 'correct horse'});"
                    "return Buffer.from(b, 'base64').toString('latin1').includes('correct horse');")
        self.assertFalse(got)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class BitwardenImport(unittest.TestCase):
    EXPORT = {
        "encrypted": False,
        "folders": [{"id": "f1", "name": "Work"}],
        "items": [
            {"id": "i1", "type": 1, "name": "GitHub", "folderId": "f1", "favorite": True,
             "notes": "recovery codes in the safe",
             "login": {"username": "me@example.com", "password": "hunter2",
                       "totp": "otpauth://totp/GitHub:me?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
                               "&issuer=GitHub",
                       "uris": [{"uri": "https://github.com"}, {"uri": "https://gist.github.com"}]},
             "fields": [{"name": "PIN", "value": "1234", "type": 1}]},
            {"id": "i2", "type": 2, "name": "Wifi", "notes": "ssid: home\npass: abc"},
            {"id": "i3", "type": 3, "name": "Visa",
             "card": {"cardholderName": "A Person", "number": "4111111111111111",
                      "expMonth": "4", "expYear": "2030", "code": "123"}},
            {"id": "i4", "type": 4, "name": "Me", "identity": {"firstName": "A", "lastName": "Person"}},
        ],
    }

    def _parse(self, text):
        return _node("return V.parseBitwarden(F.t);", {"t": text})

    def test_a_login_comes_across_whole(self):
        got = self._parse(json.dumps(self.EXPORT))
        it = [i for i in got["items"] if i["title"] == "GitHub"][0]
        self.assertEqual(it["kind"], "login")
        self.assertEqual(it["username"], "me@example.com")
        self.assertEqual(it["password"], "hunter2")
        self.assertEqual(it["uris"], ["https://github.com", "https://gist.github.com"])
        self.assertEqual(it["folder"], "Work")
        self.assertTrue(it["favorite"])
        self.assertIn("recovery codes", it["notes"])
        self.assertEqual(it["fields"][0]["name"], "PIN")
        self.assertTrue(it["fields"][0]["hidden"])

    def test_the_totp_survives_as_a_usable_secret(self):
        """Bitwarden stores the whole otpauth:// URI in that field about as often as a bare secret;
        an importer that keeps the string but can't turn it into a code has imported nothing."""
        got = _node("const p = V.parseBitwarden(F.t);"
                    "const it = p.items.find(i => i.title === 'GitHub');"
                    "const cfg = V.totpConfig(it.totp);"
                    "return cfg && await V.totp(cfg.secret, {at: 59, digits: cfg.digits});",
                    {"t": json.dumps(self.EXPORT)})
        self.assertEqual(got, "287082")

    def test_notes_cards_and_identities_are_not_dropped(self):
        got = self._parse(json.dumps(self.EXPORT))
        kinds = sorted(i["kind"] for i in got["items"])
        self.assertEqual(kinds, ["card", "identity", "login", "note"])
        card = [i for i in got["items"] if i["kind"] == "card"][0]
        self.assertEqual(card["card"]["number"], "4111111111111111")

    def test_folders_are_collected(self):
        self.assertEqual(self._parse(json.dumps(self.EXPORT))["folders"], ["Work"])

    def test_bitwarden_ids_are_kept_so_a_reimport_updates(self):
        got = self._parse(json.dumps(self.EXPORT))
        self.assertEqual([i["src"]["id"] for i in got["items"]], ["i1", "i2", "i3", "i4"])

    def test_an_encrypted_export_is_refused_loudly(self):
        got = _node("try { V.parseBitwarden(F.t); return 'imported'; }"
                    "catch (e) { return e.message; }",
                    {"t": json.dumps({"encrypted": True, "data": "2.aBcD…"})})
        self.assertIn("ENCRYPTED", got)

    def test_something_that_is_not_an_export_is_refused(self):
        got = _node("try { V.parseBitwarden('hello'); return 'imported'; } catch (e) { return 'threw'; }")
        self.assertEqual(got, "threw")

    CSV = ('folder,favorite,type,name,notes,fields,login_uri,login_username,login_password,login_totp\n'
           'Work,1,login,GitHub,"line one\nline two",,https://github.com,me,hunter2,GEZDGNBVGY3TQOJQ\n'
           ',0,login,"Bank, the",,,https://hsbc.co.uk,acct,"pa,ss",\n')

    def test_csv_with_quoted_commas_and_newlines(self):
        got = self._parse(self.CSV)
        self.assertEqual(len(got["items"]), 2)
        a, b = got["items"]
        self.assertEqual(a["title"], "GitHub")
        self.assertEqual(a["notes"], "line one\nline two")
        self.assertEqual(a["folder"], "Work")
        self.assertTrue(a["favorite"])
        self.assertEqual(b["title"], "Bank, the")
        self.assertEqual(b["password"], "pa,ss")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Audit(unittest.TestCase):
    def test_reuse_is_across_sites_not_across_one_account(self):
        """Two URIs of the same account sharing a password is not reuse. Flagging it teaches people
        to ignore the warning, which costs more than the warning was worth."""
        items = [
            {"kind": "login", "password": "same", "uris": ["https://a.com"], "updated": 10},
            {"kind": "login", "password": "same", "uris": ["https://b.com"], "updated": 10},
            {"kind": "login", "password": "solo", "uris": ["https://c.com"], "updated": 10},
            {"kind": "login", "password": "one", "uris": ["https://d.com", "https://login.d.com"], "updated": 10},
        ]
        got = _node("const a = V.audit(F.items, 1000);"
                    "return {reused: a.reused.length, weak: a.weak.length, total: a.total};",
                    {"items": items})
        self.assertEqual(got["reused"], 2)
        self.assertEqual(got["total"], 4)

    def test_old_is_measured_from_the_given_clock(self):
        items = [{"kind": "login", "password": "x" * 20, "uris": ["https://a.com"], "updated": 1}]
        got = _node("return V.audit(F.items, F.now).old.length;",
                    {"items": items, "now": 400 * 86400})
        self.assertEqual(got, 1)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class RealExportShapes(unittest.TestCase):
    """Shapes seen in a real 117-entry Bitwarden CSV, reduced to fixtures.

    Everything here was measured against an actual export rather than imagined: the 12-column header
    Bitwarden emits today (which has `reprompt` and `archivedDate` that older docs don't mention),
    and — the one that mattered — a `login_totp` column holding a 15-character value with an `&` in
    it, i.e. something typed into the wrong box in Bitwarden. Two of fifteen. The importer used to
    delete those on the way in; they must survive, because deleting what someone wrote during an
    import they cannot audit is the worst thing this feature could do quietly.
    """
    HEAD = ("folder,favorite,type,name,notes,fields,reprompt,archivedDate,"
            "login_uri,login_username,login_password,login_totp\n")

    def test_the_current_twelve_column_header_is_read(self):
        csv = self.HEAD + ",,login,GitHub,,,,,https://github.com,me,pw,GEZDGNBVGY3TQOJQ\n"
        got = _node("return V.parseBitwarden(F.t);", {"t": csv})
        it = got["items"][0]
        self.assertEqual((it["title"], it["username"], it["password"]), ("GitHub", "me", "pw"))
        self.assertEqual(it["uris"], ["https://github.com"])

    def test_an_unreadable_totp_is_kept_not_dropped(self):
        """It is not a code, and totpConfig says so — but the value is the user's."""
        csv = self.HEAD + ",,login,Thing,,,,,https://x.com,me,pw,aB3&xY9zQ1wE2r\n"
        got = _node("const p = V.parseBitwarden(F.t);"
                    "return {totp: p.items[0].totp, cfg: V.totpConfig(p.items[0].totp)};", {"t": csv})
        self.assertEqual(got["totp"], "aB3&xY9zQ1wE2r")
        self.assertIsNone(got["cfg"], "an unreadable secret must report as unreadable")

    def test_an_otpauth_uri_in_the_csv_column_still_makes_a_code(self):
        """Six of the fifteen real ones were full URIs in that column, not bare secrets."""
        csv = (self.HEAD + ',,login,Site,,,,,https://x.com,me,pw,'
               '"otpauth://totp/Site:me?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Site"\n')
        got = _node("const p = V.parseBitwarden(F.t);"
                    "const c = V.totpConfig(p.items[0].totp);"
                    "return c && await V.totp(c.secret, {at: 59, digits: 8});", {"t": csv})
        self.assertEqual(got, "94287082")

    def test_every_row_keeps_its_columns_aligned(self):
        """A shifted row would put a PASSWORD in the totp column (or worse). Checked against the
        real file by asserting a constant field count; here, with the awkward values inline."""
        csv = (self.HEAD
               + 'Work,1,login,"Bank, the","note, with comma",,,,https://a.com,u1,"pa,ss",\n'
               + ',,login,Multi,"line one\nline two",,,,https://b.com,u2,p2,\n')
        got = _node("const rows = V.parseCsv(F.t); return rows.map(r => r.length);", {"t": csv})
        self.assertEqual(got, [12, 12, 12], f"a row lost or gained a column: {got}")
        items = _node("return V.parseBitwarden(F.t).items;", {"t": csv})
        self.assertEqual(items[0]["password"], "pa,ss")
        self.assertEqual(items[1]["notes"], "line one\nline two")
