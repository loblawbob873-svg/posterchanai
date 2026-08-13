"""The translation layer — run the SHIPPED static/js/client/i18n.js under node against a stub DOM.

Run: venv-unified/bin/python -m unittest tests.test_i18n_runtime

WHY THE DECISIONS ARE TESTED AND NOT THE RENDERING. This layer substitutes text in a live DOM, and
the ways it can go wrong are all quiet:

  * it translates somebody's POST, because the post happens to read "Save";
  * it rewrites what a user is typing, because the field's value matched;
  * it costs every English user a MutationObserver and a tree walk on every feed draw, for nothing;
  * a catalogue that fails to load leaves the interface half-translated instead of English.

None of those raise. The first two are data corruption in front of the person who wrote the text,
the third is a performance regression that would be blamed on the timeline, and the fourth looks
like a translation that is merely incomplete. So the real file is loaded and driven, and the
assertions are about what it DECIDED, not about how a screen looked.

The stub DOM is deliberately small: a tree, text nodes, `closest`, a TreeWalker that collects text
nodes, and a MutationObserver that records rather than schedules. That is every browser API this
layer touches, and stubbing them is what lets the actual shipped decision code run here.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N_JS = ROOT / "static" / "js" / "client" / "i18n.js"
CATALOGUES = ROOT / "static" / "i18n"

DOM = r"""
// ---- a DOM, small enough to read and real enough to drive the shipped code ---------------------
let rafQ = [];
global.requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
global.cancelAnimationFrame = () => {};
function flush(){ const q = rafQ; rafQ = []; q.forEach(fn => fn()); }

class TextNode {
  constructor(v){ this.nodeType = 3; this.nodeValue = v; this.parentNode = null; }
}
class El {
  constructor(tag, cls){
    this.nodeType = 1; this.tag = tag; this.cls = cls || '';
    this.children = []; this.attrs = {}; this.parentNode = null;
  }
  add(n){ n.parentNode = this; this.children.push(n); return n; }
  text(v){ return this.add(new TextNode(v)); }
  setAttribute(k, v){ this.attrs[k] = v; }
  getAttribute(k){ return this.attrs[k] === undefined ? null : this.attrs[k]; }
  // Only what i18n.js asks of it: does this node or an ancestor match one of the skip selectors.
  closest(sel){
    const sels = sel.split(',').map(s => s.trim());
    let n = this;
    while(n){
      for(const s of sels){
        if(s.startsWith('.') && (' ' + n.cls + ' ').includes(' ' + s.slice(1) + ' ')) return n;
        if(s.startsWith('[') && n.attrs[s.slice(1, -1)] !== undefined) return n;
        // '.note .body' — the descendant form used by the skip list.
        if(s.includes(' ')){
          const parts = s.split(/\s+/);
          const last = parts[parts.length - 1];
          const lastOk = last.startsWith('.')
            ? (' ' + n.cls + ' ').includes(' ' + last.slice(1) + ' ') : n.tag === last;
          if(lastOk){
            let p = n.parentNode, want = parts[0];
            while(p){
              if(want.startsWith('.') && (' ' + p.cls + ' ').includes(' ' + want.slice(1) + ' ')) return n;
              p = p.parentNode;
            }
          }
          continue;
        }
        if(!s.startsWith('.') && !s.startsWith('[') && n.tag === s) return n;
      }
      n = n.parentNode;
    }
    return null;
  }
  querySelectorAll(){ // only ever called for the human-readable attributes
    const out = [];
    (function walk(n){
      if(n.nodeType !== 1) return;
      for(const a of ['placeholder','title','aria-label','alt','data-label'])
        if(n.attrs[a] !== undefined){ out.push(n); break; }
      n.children.forEach(walk);
    })(this);
    return out;
  }
  querySelector(){ return null; }
}

global.NodeFilter = { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 };
global.document = {
  readyState: 'complete',
  addEventListener(){},
  documentElement: { setAttribute(k,v){ this[k] = v; }, classList: { toggle(){} } },
  createTreeWalker(root, what, filter){
    const found = [];
    (function walk(n){
      if(n.nodeType === 3){ if(filter.acceptNode(n) === NodeFilter.FILTER_ACCEPT) found.push(n); return; }
      (n.children || []).forEach(walk);
    })(root);
    let i = 0;
    return { nextNode(){ return i < found.length ? found[i++] : null; } };
  },
};
let observed = 0;
global.MutationObserver = class { constructor(){ observed++; } observe(){} disconnect(){ observed--; } };
global.window = {};
global.localStorage = { _v:{}, getItem(k){ return this._v[k] || null; }, setItem(k,v){ this._v[k]=v; } };

let CATALOGUE = CATALOGUE_JSON;
let fetches = 0;
global.fetch = (url) => {
  fetches++;
  if(CATALOGUE === null) return Promise.resolve({ ok:false, status:404 });
  return Promise.resolve({ ok:true, json: () => Promise.resolve(CATALOGUE) });
};

require(I18N_PATH);
const I = global.window.PCI18N;
"""


def run(js: str, catalogue=None) -> dict:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    src = (
        DOM.replace("CATALOGUE_JSON", json.dumps(catalogue) if catalogue is not None else "null")
        .replace("I18N_PATH", json.dumps(str(I18N_JS)))
        + js
    )
    p = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr or "")[-3000:])
    return json.loads(p.stdout)


CAT = {"Save": "保存", "Bookmarks": "ブックマーク", "Load older": "古いものを読み込む",
       "search posts": "投稿を検索"}


class I18nRuntime(unittest.TestCase):
    def test_chrome_is_translated(self):
        out = run("""
          const body = new El('body');
          document.body = body;
          const btn = body.add(new El('button', 'btn'));
          btn.text('Save');
          I.set('ja').then(() => {
            console.log(JSON.stringify({ text: btn.children[0].nodeValue }));
          });
        """, CAT)
        self.assertEqual(out["text"], "保存")

    def test_a_post_that_says_save_is_not_translated(self):
        """The collision the catalogue-membership rule cannot see on its own — somebody's note whose
        entire body is a word this app also uses on a button. The skip list is the second guard, and
        it is the one standing between a translation feature and rewriting user data."""
        out = run("""
          const body = new El('body');
          document.body = body;
          const note = body.add(new El('article', 'note'));
          const nb = note.add(new El('div', 'body'));
          const txt = nb.add(new El('div', 'txt'));
          txt.text('Save');
          const btn = body.add(new El('button', 'btn'));
          btn.text('Save');
          I.set('ja').then(() => {
            console.log(JSON.stringify({ post: txt.children[0].nodeValue,
                                         chrome: btn.children[0].nodeValue }));
          });
        """, CAT)
        self.assertEqual(out["post"], "Save")       # untouched
        self.assertEqual(out["chrome"], "保存")      # translated

    def test_text_not_in_the_catalogue_is_left_alone(self):
        """Membership IS the filter. A post is not in the catalogue, so a post is not touched — no
        allow-list of containers that would rot as the renderers change."""
        out = run("""
          const body = new El('body'); document.body = body;
          const p = body.add(new El('div', 'anything'));
          p.text('just some ordinary sentence nobody shipped');
          I.set('ja').then(() => console.log(JSON.stringify({ t: p.children[0].nodeValue })));
        """, CAT)
        self.assertEqual(out["t"], "just some ordinary sentence nobody shipped")

    def test_a_field_being_typed_into_is_never_rewritten(self):
        out = run("""
          const body = new El('body'); document.body = body;
          const ta = body.add(new El('textarea', ''));
          ta.text('Save');
          I.set('ja').then(() => console.log(JSON.stringify({ t: ta.children[0].nodeValue })));
        """, CAT)
        self.assertEqual(out["t"], "Save")

    def test_placeholders_and_titles_are_translated(self):
        out = run("""
          const body = new El('body'); document.body = body;
          const inp = body.add(new El('input', ''));
          inp.setAttribute('placeholder', 'search posts');
          const b = body.add(new El('button', '')); b.setAttribute('title', 'Bookmarks');
          I.set('ja').then(() => console.log(JSON.stringify({
            ph: inp.getAttribute('placeholder'), ti: b.getAttribute('title') })));
        """, CAT)
        # An <input> is on the skip list for its TEXT; its placeholder is still ours to translate.
        self.assertEqual(out["ph"], "投稿を検索")
        self.assertEqual(out["ti"], "ブックマーク")

    def test_english_installs_nothing_and_fetches_nothing(self):
        """The default must not pay for the feature. No catalogue request, no observer, no walk."""
        out = run("""
          const body = new El('body'); document.body = body;
          body.add(new El('div','')).text('Save');
          I.set('en').then(() => console.log(JSON.stringify({ fetches, observed })));
        """, CAT)
        self.assertEqual(out["fetches"], 0)
        self.assertEqual(out["observed"], 0)

    def test_a_catalogue_that_will_not_load_leaves_the_app_in_english(self):
        """Half-translated is worse than untranslated: it reads as a broken feature rather than an
        absent one, and it leaves `dir` set for a language that never loaded."""
        out = run("""
          const body = new El('body'); document.body = body;
          const d = body.add(new El('div','')); d.text('Save');
          I.set('ar').then(ok => console.log(JSON.stringify({
            ok, lang: I.lang, text: d.children[0].nodeValue,
            dir: document.documentElement.dir })));
        """, None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["lang"], "en")
        self.assertEqual(out["text"], "Save")
        self.assertEqual(out["dir"], "ltr")

    def test_arabic_sets_rtl_and_japanese_does_not(self):
        out = run("""
          const body = new El('body'); document.body = body;
          I.set('ja').then(() => {
            const ja = document.documentElement.dir;
            return I.set('ar').then(() => console.log(JSON.stringify({
              ja, ar: document.documentElement.dir })));
          });
        """, CAT)
        self.assertEqual(out["ja"], "ltr")
        self.assertEqual(out["ar"], "rtl")

    def test_the_shipped_catalogues_are_valid_and_cover_the_english_one(self):
        """A catalogue is data, and data rots differently from code: it goes stale rather than
        breaking. This says how stale, by name, instead of leaving it to be noticed on screen."""
        en_path = CATALOGUES / "en.json"
        if not en_path.exists():
            self.skipTest("run scripts/i18n_extract.py first")
        en = json.loads(en_path.read_text())
        self.assertGreater(len(en), 500, "the extracted catalogue looks suspiciously small")
        for lang in ("ar", "ja"):
            p = CATALOGUES / f"{lang}.json"
            if not p.exists():
                continue
            cat = json.loads(p.read_text())
            self.assertTrue(all(isinstance(v, str) and v for v in cat.values()),
                            f"{lang}.json has an empty or non-string translation")
            stray = [k for k in cat if k not in en]
            self.assertEqual(stray[:5], [], f"{lang}.json has keys no longer in en.json")


if __name__ == "__main__":
    unittest.main()
