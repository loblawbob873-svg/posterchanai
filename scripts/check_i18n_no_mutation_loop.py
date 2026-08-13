#!/usr/bin/env python3
"""A translation that equals its source must not wedge the browser.

Reported as "change to Japanese, go to social, then go back to settings, firefox unresponsive", and
the same for Arabic — which is what ruled out RTL: Japanese is left-to-right.

THE MECHANISM. `setAttribute` queues a MutationObserver record whether or not the value actually
changed. `subAttrs` translates `title`/`aria-label`/`placeholder`/`alt`, and `onMutations` answers an
`attributes` record by calling `subAttrs` again. So for any catalogue entry where the translation
equals the source, the write wakes the observer, which performs the same write, for ever —
synchronously, on the main thread.

Those entries are correct, not a catalogue bug: 130 of Arabic's 3,525 strings and 155 of Japanese's
3,494 are proper nouns and technical labels that must stay as they are ('API Hash', 'CPU', 'Android',
'Blob TTL'). They cluster on the settings screens, which is exactly where the report lands.

THE CATALOGUES ARE READ FROM DISK, not invented here, so this check keeps testing the real thing as
they are re-translated — and it asserts the identity entries still exist, because a catalogue that
happened to have none would make this pass while proving nothing.

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

# Generous: the loop is unbounded, so a real failure never lands near a threshold. What this
# separates is "finished" from "still going when the page was torn down".
BUDGET_MS = 4000

PAGE = """<!doctype html><meta charset="utf-8"><title>pending</title>
<body>
<div id="app"></div>
<script src="file://%(root)s/static/js/client/i18n.js"></script>
<script>
var CAT = %(cat)s;
var IDENT = %(ident)s;
var app = document.getElementById('app');

function rows(){
  var h = '';
  for(var i = 0; i < IDENT.length; i++){
    var s = IDENT[i].replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    h += '<div class="row"><label title="' + s + '" aria-label="' + s + '">' + s + '</label>'
       + '<input placeholder="' + s + '"></div>';
  }
  return h;
}

/* THE ORDER IS THE TEST. `setLang` walks the document and only THEN attaches the observer, so the
   first pass can never re-enter it — the loop needs nodes that arrive AFTER the language is set,
   which is precisely "change language, then navigate to settings". */
var t0 = performance.now();
PCI18N.set('ja', CAT).then(function(){
  app.innerHTML = rows();                 // what renderView does on every navigation
  /* Drive the walk DIRECTLY rather than waiting on the observer's requestAnimationFrame: headless
     advances timers but does not reliably produce frames, and a check that silently never reaches
     subAttrs passes while proving nothing (it did, twice). The attributes branch of onMutations is
     synchronous, so the re-entry this exists to catch needs no frame at all. */
  PCI18N.refresh(app);
  /* setTimeout, not requestAnimationFrame: headless virtual time advances timers but does not always
     produce frames, and a settle signal that cannot fire would report every run as a hang. */
  setTimeout(function(){
    document.title = JSON.stringify({ms: Math.round(performance.now() - t0), settled: true});
  }, 1200);
}, function(e){ document.title = JSON.stringify({error: String(e && e.message || e)}); });
</script>
</body>"""


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 2

    ident, cat = [], {}
    for lang in ("ar", "ja"):
        path = os.path.join(ROOT, "static", "i18n", f"{lang}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                strings = json.load(fh)
        except Exception as e:
            print(f"SKIP  could not read {lang}.json: {e}")
            return 2
        strings = strings.get("strings", strings)
        same = [k for k, v in strings.items() if k == v and k.strip() and '"' not in k]
        if not same:
            print(f"SKIP  {lang}.json has no identity entries — this check would prove nothing")
            return 2
        ident.extend(same[:60])
        cat.update({k: strings[k] for k in same[:60]})

    print(f"{len(ident)} identity strings from the real catalogues")
    tmp = tempfile.mkdtemp(prefix="pci18nloop-")
    try:
        page = PAGE % {"root": ROOT,
                       "cat": json.dumps(cat, ensure_ascii=False),
                       "ident": json.dumps(ident, ensure_ascii=False)}
        p = os.path.join(tmp, "loop.html")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(page)
        # A HANG IS THE RESULT, not an error: with the loop present the renderer never returns and
        # Chrome must be killed. Caught here so the suite reports a FAIL with the reason instead of a
        # traceback, and kept short because there is nothing to wait for once it is spinning.
        try:
            out = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu",
                 f"--virtual-time-budget={BUDGET_MS + 4000}", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + p],
                capture_output=True, text=True, timeout=60).stdout
        except subprocess.TimeoutExpired:
            print("FAIL  the renderer never returned — subAttrs is re-entering the observer on a "
                  "write that changes nothing (a translation equal to its source). This is the hang.")
            return 1
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        if not m:
            print("FAIL  the page produced no output at all")
            return 1
        raw = m.group(1)
        if raw == "pending":
            print("FAIL  the page never settled — subAttrs is re-entering the observer on a write "
                  "that changes nothing (a translation equal to its source)")
            return 1
        q = json.loads(raw.replace("&quot;", '"'))
        if q.get("error"):
            print("SKIP  " + q["error"])
            return 2
        print(f"settled in {q['ms']}ms")
        if q["ms"] > BUDGET_MS:
            print(f"FAIL  took {q['ms']}ms (budget {BUDGET_MS}ms)")
            return 1
        print("PASS")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
