"""What `linkify` does to a URL, checked on the DOM it actually produces.

A note body goes through several passes here, and each one rewrites PLAIN TEXT into HTML. Once the
first pass has emitted an `<a>`, the URL inside it is no longer text — it is markup, twice over (the
href, and the link's own label) — so a later pass that matches inside it does not decorate anything.
It shatters the tag.

That is not hypothetical. `https://kehto.github.io/web/paja/?pointer=naddr1…` came out of the
timeline as a ~50px-wide column of one word per line with `" target="_blank" rel="noopener">https…`
showing as body text, because the nostr-entity pass matched `=naddr1…`: its "part of a URL" guard
only skips an entity preceded by a word character, `/` or `.`, and `=` is none of those.

  url-not-shattered  the anchor survives every later pass — one `<a>`, the full URL in its href, and
                     no markup leaking into the visible text
  entity-still-works the guard must not be bought by disabling the feature: a bare naddr/npub in the
                     text still becomes an embed / a mention
  fragment-not-a-tag `#anchor` inside a URL is not a hashtag, and a real #hashtag still is
  label-shortened    a ~470-character URL is not printed in full (it buries the note), while an
                     ordinary link is left exactly as it was — and the href is the whole URL either
                     way, since only the LABEL is cut

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

# The real note, from the report (nevent1qqsp6eprlq…): two URLs, each carrying a whole naddr.
KEHTO = ("https://kehto.github.io/web/paja/?pointer=naddr1qvzqqqyf8ypzqfngzhsvjggdlgeycm96x4emzj"
         "lwf8dyyzdfg4hefp89zpkdgz99qy28wumn8ghj7un9d3shjtnyv9kh2uewd9hsz9thwden5te0wfjkccte9ejx"
         "zmt4wvhxjme0qyghwumn8ghj7mn0wd68ytnhd9hx2tcprfmhxue69uhhq7tjv9kkjepwve5kzar2v9nzucm0d5"
         "hsz9nhwden5te0wfjkccte9ejxjar5duh8qatz9uqqkmtfdejhxam9v4cx2usxwj6a0")
NSITE = ("https://2tlb607yn9ai420jf20b3ge69so4zeoi0bmj6iri8u8n067hmgnapplet.nsite.lol/app/naddr1"
         "qvzqqqyf8ypzqfngzhsvjggdlgeycm96x4emzjlwf8dyyzdfg4hefp89zpkdgz99qy28wumn8ghj7un9d3shj")


def _fn(src, name, opener):
    """Pull one top-level function out of app.js by brace counting from its opening line."""
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
    with open(APP) as fh:
        src = fh.read()
    m = re.search(r"const _LINK_LABEL_MAX = \d+;", src)
    assert m, "_LINK_LABEL_MAX is gone — the label rule moved"
    stubs = r"""
const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _ENC_MARK = '#pcenc1=';
const encAttParse = () => null;
const ytId = () => null;
const _media = (u) => '<img src="' + u + '">';
const BLOBF = 'blob';
const NO_IMAGES = false;
const HEX = 'ab'.repeat(32);
// Enough of nip19 to decide what an entity IS. Everything that is not one throws, exactly as the
// real decoder does for a truncated or mid-URL string.
const NT = () => ({ nip19: {
  decode: (s) => {
    if (/^naddr1/.test(s)) return { type:'naddr', data:{ kind:30023, pubkey:HEX, identifier:'d' } };
    if (/^npub1/.test(s))  return { type:'npub',  data:HEX };
    throw new Error('not an entity');
  },
  npubEncode: (pk) => 'npub1' + pk.slice(0, 58),
}});
const Store = { profile: () => ({}), get: () => null };
const needProfile = () => {}; const needEvent = () => {}; const needAddr = () => {};
const _adCache = new Map();
const quotedDiv = () => '<div class="quoted">q</div>';
const addrDiv = () => '<div class="quoted">a</div>';
const emojiName = (pk, n) => n;
const niceNip05 = () => '';
"""
    return "\n".join([stubs, m.group(0),
                      _fn(src, "_linkLabel", "function _linkLabel(u){"),
                      _fn(src, "linkify", "function linkify(txt){")])


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
%s
const KEHTO = %s;
const NSITE = %s;
const box = document.createElement('div');
const render = t => { box.innerHTML = linkify(t); return box; };
const out = {};

// 1. the reported note, reduced to the two lines that carry the URLs
{
  const el = render('Minesweeper running in two different runtimes\\n\\n' + KEHTO
                    + '\\n\\nNostrudel branch with NIP-5D support.\\n\\n' + NSITE);
  const as = [...el.querySelectorAll('a')];
  out.anchors    = as.length;
  out.hrefs      = as.map(a => a.getAttribute('href'));
  out.quotedIn   = el.querySelectorAll('.quoted').length;
  // The tell-tale of a shattered tag: attribute text rendered as body text.
  out.leakedAttr = /target=|rel="noopener|href=/.test(el.textContent);
  out.labels     = as.map(a => a.textContent);
}
// 2. the same entities as TEXT still resolve — the guard must not have switched the feature off
{
  const el = render('look at naddr1qvzqqqyf8ypzqfngzhsvjggdlgeycm96x4emzjlwf8dyyzdfg4hefp89zpkdgz99');
  out.bareNaddrEmbeds = el.querySelectorAll('.quoted').length;
}
{
  const el = render('hi npub1' + 'q'.repeat(58) + ' there');
  out.bareNpubMentions = el.querySelectorAll('a.mention').length;
}
// 3. hashtags: a URL fragment is not one, a real tag is
{
  const el = render('see https://example.com/page#section and #nostr');
  out.tagLinks = [...el.querySelectorAll('a.hashtag')].map(a => a.textContent);
}
// 4. labels: long is cut, ordinary is untouched, href is whole either way
{
  const short = 'https://example.com/some/ordinary/article-slug-that-is-fine';
  const el = render(short);
  const a = el.querySelector('a');
  out.shortLabel = a.textContent;
  out.shortHref  = a.getAttribute('href');
}
{
  const el = render(KEHTO);
  const a = el.querySelector('a');
  out.longLabelLen = a.textContent.length;
  out.longLabelCut = a.textContent.slice(-1);
  out.longHref     = a.getAttribute('href');
  out.longTitle    = a.getAttribute('title');
}
// 5. a note cannot forge the placeholder and address a slot
{
  const el = render('\\u0000L0\\u0000 https://example.com/x');
  out.forgedSlot = el.textContent.indexOf('example.com') >= 0;
}
document.getElementById('out').textContent = JSON.stringify(out);
</script>"""


class LinkifyUrls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome — the assertions are about the parsed DOM")
        tmp = tempfile.mkdtemp(prefix="pclink-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE % (_harness(), json.dumps(KEHTO), json.dumps(NSITE)))
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

    def test_a_url_carrying_a_nostr_entity_stays_one_intact_link(self):
        """The naddr lives in a query string, so the entity pass's guard (skip anything preceded by
        a word char, `/` or `.`) does not cover it. Held in a slot instead, no later pass can see a
        URL at all."""
        self.assertEqual(self.r["anchors"], 2, "one anchor per URL")
        self.assertEqual(self.r["hrefs"][0], KEHTO, "the href must be the WHOLE url")
        self.assertEqual(self.r["hrefs"][1], NSITE)
        self.assertEqual(self.r["quotedIn"], 0, "nothing may be embedded from inside a URL")
        self.assertFalse(self.r["leakedAttr"],
                         "attribute markup showing as body text is a shattered tag")

    def test_the_entities_themselves_still_render(self):
        """The cheap fix for the above is to stop matching entities at all. These fail if that ever
        happens."""
        self.assertEqual(self.r["bareNaddrEmbeds"], 1, "a bare naddr must still embed")
        self.assertEqual(self.r["bareNpubMentions"], 1, "a bare npub must still be a mention")

    def test_a_url_fragment_is_not_a_hashtag(self):
        self.assertEqual(self.r["tagLinks"], ["#nostr"])

    def test_an_ordinary_link_is_displayed_exactly_as_written(self):
        short = "https://example.com/some/ordinary/article-slug-that-is-fine"
        self.assertEqual(self.r["shortLabel"], short)
        self.assertEqual(self.r["shortHref"], short)

    def test_a_monster_url_is_shortened_for_display_only(self):
        """~470 characters of base32 buries the note under a Show-more clamp and tells the reader
        nothing. The href and the title keep all of it."""
        self.assertLess(self.r["longLabelLen"], 80)
        self.assertEqual(self.r["longLabelCut"], "…")
        self.assertEqual(self.r["longHref"], KEHTO)
        self.assertEqual(self.r["longTitle"], KEHTO)

    def test_a_note_cannot_write_its_own_placeholder(self):
        """The URL slots use a NUL sentinel. If the input's own NULs survived, a note could address
        a slot that belongs to a link and move it — so they are stripped before anything runs."""
        self.assertTrue(self.r["forgedSlot"], "the real link must still be rendered")


if __name__ == "__main__":
    unittest.main()
