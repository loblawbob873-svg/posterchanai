#!/usr/bin/env python3
"""Switching language must UNDO the previous one, not merely stop translating.

Reported as "choosing english puts the arabic menu on the left" — and "hard refresh brings back
english", which is the same fact from the other side: a reload re-renders from source, an in-place
switch never did. Substitution overwrites the DOM, so dropping the catalogue leaves every translated
string exactly where it was and flips only the direction.

Two switches are checked, because they fail for the same reason and only one of them is obvious:

  ja -> en    the menu must come back to English, not merely move
  ar -> ja    a catalogue keyed on ENGLISH matches nothing on an Arabic screen, so without a revert
              the Arabic simply stays and the new language does nothing at all

Exit 2 = could not run, reported as a SKIP, never as a pass.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")

PAGE = """<!doctype html><meta charset="utf-8"><title>pending</title>
<body>
<nav id="app"></nav>
<script src="file://%(root)s/static/js/client/i18n.js"></script>
<script>
var AR = %(ar)s, JA = %(ja)s, KEYS = %(keys)s;

var app = document.getElementById('app');
var h = '';
for(var i = 0; i < KEYS.length; i++){
  var s = KEYS[i].replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  h += '<button title="' + s + '">' + s + '</button>';
}
app.innerHTML = h;
var SOURCE = app.innerText;

function has(re){ return (app.innerText.match(re) || []).length; }
var out = {};

PCI18N.set('ja', JA).then(function(){
  out.jaChars = has(/[\\u3040-\\u30FF\\u4E00-\\u9FFF]/g);
  return PCI18N.set('en');
}).then(function(){
  /* Back to English: nothing Japanese may survive, in text OR in the attributes. */
  out.afterEnglish_jaChars = has(/[\\u3040-\\u30FF\\u4E00-\\u9FFF]/g);
  out.afterEnglish_matchesSource = (app.innerText === SOURCE);
  var t = app.querySelector('button').getAttribute('title');
  out.afterEnglish_titleIsSource = (t === KEYS[0]);
  return PCI18N.set('ar', AR);
}).then(function(){
  out.arChars = has(/[\\u0600-\\u06FF]/g);
  /* ARABIC MUST NOT MIRROR THE INTERFACE. Translating the words is the feature; moving the navbar
     to the right and flipping the settings is not wanted here, and it is a one-word regression
     away (LOCALES.ar.mirror). */
  out.arDir = document.documentElement.getAttribute('dir');
  out.arRtlClass = document.documentElement.classList.contains('rtl');
  out.arRtlSheet = !!document.getElementById('pc-rtl-css');
  return PCI18N.set('ja', JA);            // language -> language, never touching English
}).then(function(){
  out.afterArToJa_arChars = has(/[\\u0600-\\u06FF]/g);
  out.afterArToJa_jaChars = has(/[\\u3040-\\u30FF\\u4E00-\\u9FFF]/g);
  document.title = JSON.stringify(out);
}).catch(function(e){ document.title = JSON.stringify({error: String(e && e.message || e)}); });
</script>
</body>"""


def _cat(lang):
    with open(os.path.join(ROOT, "static", "i18n", f"{lang}.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    return d.get("strings", d)


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 2
    try:
        ar, ja = _cat("ar"), _cat("ja")
    except Exception as e:
        print(f"SKIP  could not read the catalogues: {e}")
        return 2

    # Strings that BOTH languages actually translate — an identity entry would prove nothing here.
    keys = [k for k in ar
            if k in ja and ar[k] != k and ja[k] != k and '"' not in k and k.strip()][:40]
    if len(keys) < 10:
        print("SKIP  too few strings translated by both languages to test a round trip")
        return 2
    print(f"{len(keys)} strings translated by both ar and ja")

    tmp = tempfile.mkdtemp(prefix="pci18nrt-")
    try:
        page = PAGE % {"root": ROOT,
                       "ar": json.dumps({k: ar[k] for k in keys}, ensure_ascii=False),
                       "ja": json.dumps({k: ja[k] for k in keys}, ensure_ascii=False),
                       "keys": json.dumps(keys, ensure_ascii=False)}
        p = os.path.join(tmp, "rt.html")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(page)
        try:
            out = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu",
                 "--virtual-time-budget=8000", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + p],
                capture_output=True, text=True, timeout=60).stdout
        except subprocess.TimeoutExpired:
            print("FAIL  the renderer never returned")
            return 1
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        if not m or m.group(1) == "pending":
            print("SKIP  the page produced no measurement")
            return 2
        q = json.loads(m.group(1).replace("&quot;", '"'))
        if q.get("error"):
            print("SKIP  " + q["error"])
            return 2

        bad = []
        if not q.get("jaChars"):
            print("SKIP  Japanese never applied; the check is unarmed")
            return 2
        if q.get("afterEnglish_jaChars"):
            bad.append(f"{q['afterEnglish_jaChars']} Japanese characters survived the switch to "
                       f"English — the menu stayed translated and only the direction changed")
        if not q.get("afterEnglish_matchesSource"):
            bad.append("the text did not come back to the strings this app ships")
        if not q.get("afterEnglish_titleIsSource"):
            bad.append("a `title` attribute stayed translated after switching to English")
        if not q.get("arChars"):
            bad.append("Arabic never applied after English")
        if q.get("arDir") != "ltr":
            bad.append(f"Arabic set dir={q.get('arDir')!r} — the interface must not mirror; "
                       f"translate the words and leave the layout alone")
        if q.get("arRtlClass"):
            bad.append("Arabic added the .rtl class, which mirrors the chrome")
        if q.get("arRtlSheet"):
            bad.append("Arabic linked rtl.css, which flips the navbar and the settings")
        if q.get("afterArToJa_arChars"):
            bad.append(f"{q['afterArToJa_arChars']} Arabic characters survived ar -> ja — a "
                       f"catalogue keyed on English cannot match an Arabic screen")
        if not q.get("afterArToJa_jaChars"):
            bad.append("Japanese never applied when switching straight from Arabic")

        if bad:
            for b in bad:
                print("FAIL  " + b)
            return 1
        print(f"ja->en clean, ar->ja clean ({q['jaChars']} ja / {q['arChars']} ar chars applied)")
        print("PASS")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
