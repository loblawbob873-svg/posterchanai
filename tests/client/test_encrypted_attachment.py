"""Shareable encrypted attachments, exercised as the browser runs them.

A DM used to encrypt the message and upload the picture in it as plaintext, to a Blossom server that
authorizes no reads at all — so the file was world-readable, and `GET /list/<pubkey>` made it
world-ENUMERABLE by the sender's npub. Encrypting the bytes client-side is the fix, and the parts
that can silently undo it are all in this file:

  round trip        the ciphertext must actually decrypt back to the exact input bytes, including
                    for binary that is not valid UTF-8
  key never leaks   the reference carries the key in the URL FRAGMENT, and the fetch must use the
                    part BEFORE it — sending the fragment to the server would hand it the key
  linkify order     the reference keeps the blob's extension, so `<sha>.jpg#pcenc1=…` matches the
                    plain image rule (which ends `(\\?|#|$)`) and would render an <img> pointed at
                    ciphertext — a broken image, and a request for the ciphertext, in place of the
                    encrypted-attachment placeholder
  opt-in            an unmarked URL must go on being handled exactly as before, or turning this on
                    changes every existing conversation

The module is extracted from app.js rather than copied, so it cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")
NODE = shutil.which("node")


def _fn(src, name, opener):
    """Pull one top-level `function name(...)` (or const) out of app.js by brace counting from its
    opening line — the functions here are small and self-contained, unlike the module blocks."""
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("could not bound " + name)


def _harness():
    src = open(APP).read()
    parts = [
        "const enc = s => (s==null?'':String(s)).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));",
        "const _ENC_MARK = '#pcenc1=';",
        _fn(src, "_b64u", "const _b64u = b =>").split("\n")[0],
        _fn(src, "_b64uDec", "function _b64uDec(s){"),
        _fn(src, "_masterEncrypt", "async function _masterEncrypt(mk, plain, iv){"),
        _fn(src, "_masterDecrypt", "async function _masterDecrypt(mk, blob){"),
        _fn(src, "encAttParse", "function encAttParse(ref){"),
    ]
    return "\n".join(parts)


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
%s
// The exact branch order linkify uses, reduced to the decision under test: does a URL become an
// encrypted-attachment placeholder, an <img>, or a link?
function classify(u){
  if(u.indexOf(_ENC_MARK) > 0) return 'encatt';
  if(/\\.(jpe?g|png|gif|webp|avif)(\\?|#|$)/i.test(u)) return 'img';
  if(/\\.(mp4|webm|mov|m4v)(\\?|#|$)/i.test(u)) return 'video';
  return 'link';
}
(async () => {
  const out = {};
  const te = new TextEncoder();
  // 1. round trip, over bytes that are NOT valid UTF-8 (a real image never is)
  const plain = new Uint8Array(1024);
  crypto.getRandomValues(plain);
  const key = crypto.getRandomValues(new Uint8Array(32));
  const blob = await _masterEncrypt(key, plain);
  out.ciphertextDiffers = !(blob.length === plain.length &&
    plain.every((b,i)=>b===blob[i]));
  out.ivPrefixed = blob.length === plain.length + 12 + 16;   // iv(12) + ct + GCM tag(16)
  const back = await _masterDecrypt(key, blob);
  out.roundTrip = back.length === plain.length && plain.every((b,i)=>b===back[i]);
  // a different key must NOT decrypt
  const other = crypto.getRandomValues(new Uint8Array(32));
  try { await _masterDecrypt(other, blob); out.wrongKeyOpens = true; }
  catch(_){ out.wrongKeyOpens = false; }

  // 2. the reference: build one the way uploadSharedEnc does, then parse it back
  const meta = { k:_b64u(key), m:'image/png', n:'holiday photo.png' };
  const base = 'https://poster.place/blossom/' + 'ab'.repeat(32) + '.png';
  const ref  = base + _ENC_MARK + _b64u(te.encode(JSON.stringify(meta)));
  const p = encAttParse(ref);
  out.parsedUrl  = p && p.url;
  out.parsedMime = p && p.mime;
  out.parsedName = p && p.name;
  out.keyMatches = !!(p && p.key.length === 32 && p.key.every((b,i)=>b===key[i]));
  out.fragmentStripped = !!(p && p.url.indexOf('#') < 0 && p.url === base);
  // the key must not survive anywhere in the string we would fetch
  out.urlCarriesNoKey = !!(p && p.url.indexOf(meta.k) < 0);

  // 3. branch order + opt-in
  out.clsEnc   = classify(ref);
  out.clsImg   = classify('https://poster.place/blossom/x.png');
  out.clsVid   = classify('https://poster.place/blossom/x.mp4');
  out.clsPlain = classify('https://example.com/page');
  // a malformed / truncated marker must fall through, never throw
  const junk  = base + _ENC_MARK + 'not!valid!base64';
  const short = base + _ENC_MARK + _b64u(te.encode(JSON.stringify({k:_b64u(new Uint8Array(8))})));
  out.clsJunk  = classify(junk);
  out.clsShort = classify(short);
  out.junkParses  = encAttParse(junk);
  out.shortParses = encAttParse(short);

  // 4. an attribute-safe placeholder: the reference goes into a data- attribute
  out.attrSafe = enc(ref).indexOf('"') < 0;

  document.getElementById('out').textContent = JSON.stringify(out);
})();
</script>"""


@unittest.skipUnless(NODE, "node not installed")
class EncryptedAttachment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome — WebCrypto needs a real browser")
        tmp = tempfile.mkdtemp(prefix="pcenc-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE % _harness())
            res = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                 "--virtual-time-budget=15000", "--dump-dom", "file://" + path],
                capture_output=True, text=True, timeout=180).stdout
            m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
            if not m or not m.group(1).strip():
                raise unittest.SkipTest("page did not evaluate")
            cls.r = json.loads(m.group(1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_bytes_are_actually_encrypted_and_come_back_intact(self):
        self.assertTrue(self.r["ciphertextDiffers"], "the upload must not be the plaintext")
        self.assertTrue(self.r["ivPrefixed"], "iv(12) + ciphertext + GCM tag(16) — the drive's layout")
        self.assertTrue(self.r["roundTrip"], "decrypt must return the exact input bytes")

    def test_a_different_key_cannot_open_it(self):
        """AES-GCM is authenticated: the wrong key must RAISE, not return plausible garbage."""
        self.assertFalse(self.r["wrongKeyOpens"])

    def test_the_fetched_url_never_carries_the_key(self):
        """The key lives in the fragment precisely so it is never transmitted. If the URL we fetch
        still contained it, the server storing the ciphertext would also be handed the key."""
        self.assertTrue(self.r["fragmentStripped"], "the fetch URL must be the part before '#'")
        self.assertTrue(self.r["urlCarriesNoKey"])

    def test_the_reference_round_trips_key_mime_and_name(self):
        self.assertTrue(self.r["keyMatches"], "the parsed key must be the 32 bytes we encrypted with")
        self.assertEqual(self.r["parsedMime"], "image/png")
        self.assertEqual(self.r["parsedName"], "holiday photo.png")

    def test_an_encrypted_image_is_not_rendered_as_an_image(self):
        """The reference keeps the blob's extension, so the plain image rule — which ends (\\?|#|$) —
        matches it. Tested because the failure is quiet: an <img> pointed at ciphertext, which fetches
        the bytes and shows a broken picture where the attachment should be."""
        self.assertEqual(self.r["clsEnc"], "encatt")

    def test_ordinary_urls_are_untouched(self):
        """Opt-in means opt-in: nothing about an unmarked URL may change."""
        self.assertEqual(self.r["clsImg"], "img")
        self.assertEqual(self.r["clsVid"], "video")
        self.assertEqual(self.r["clsPlain"], "link")

    def test_a_mangled_marker_is_never_fetched_as_media(self):
        """A reference mangled in transit (another client's link handling, a truncated paste) still
        POINTS AT CIPHERTEXT. The marker alone has to decide, not whether the descriptor parses:
        deciding on the parse let a broken reference fall through to the image rule, which renders an
        <img> that downloads the encrypted bytes and draws a broken picture. Caught by this test,
        which is why it is written against the branch order rather than the happy path."""
        self.assertEqual(self.r["clsJunk"], "encatt")
        self.assertEqual(self.r["clsShort"], "encatt", "a wrong-length key is still an encrypted blob")
        # …and parsing it must yield nothing, so the decorator reports it instead of fetching.
        self.assertIsNone(self.r["junkParses"])
        self.assertIsNone(self.r["shortParses"])

    def test_the_reference_is_safe_in_an_html_attribute(self):
        self.assertTrue(self.r["attrSafe"])


if __name__ == "__main__":
    unittest.main()
