"""An AI-chat artifact link has to be DOWNLOADABLE in a shell that is not a browser.

A non-media artifact reaches the user as a plain markdown link — the agent's `/workspace` backup is
the one that actually does, `[⬇️ sandbox-workspace.tar.gz](/api/files/…)` — and `mdInline` renders a
markdown link as `<a href="…">`. A ROOT-RELATIVE href resolves against the PAGE origin, and in the
bundled app that origin is https://localhost (Android) or app://posterchan (desktop), where nothing
serves `/api` at all. The shell answers with its own not-found body and no request ever leaves the
device: on the run this was reported from, the instance log shows the conversation being opened and
no GET for the file whatsoever.

Media never had the problem — `!video[]`/`!audio[]`/`![]` are absolutized (`_absUrl`) and carry the
`.ai-dlfile` row, whose fetch goes through the shim with credentials. So the fix is not a second
download path, it is the SAME one: a `/api/files/` link becomes that button.

  artifact-is-a-button   the workspace-backup message renders a `.ai-dlfile` button, and NOT an
                         `<a href="/api/files/…">` that a bundled shell cannot resolve
  name-loses-the-glyph   the ⬇️ the server writes into the label is not part of the filename the
                         download lands under
  ordinary-links-intact  an http(s) link in the same message is still an anchor — this must not
                         turn every link in AI chat into a button
  check-can-fail         the same message, run through aiFormat with the rule removed, DOES produce
                         the bare anchor — so a pass here means the rule, not the harness

The renderer is extracted from app.js rather than copied, so it cannot drift from what ships.
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

# The message the server writes (command_service/system.py) — verbatim shape, with a real artifact
# path off a run that happened.
BACKUP_MSG = (
    "\U0001f4e6 `sandbox` workspace backup (190 bytes, gzipped)\n\n"
    "[⬇️ sandbox-workspace.tar.gz]"
    "(/api/files/verita84%40poster.place/1336/enc_" + "d" * 64 + ".gz)"
)


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


def _const(src, name):
    """One single-line `const NAME = …;` declaration, as written."""
    m = re.search(r"^\s*const %s\s*=.*$" % re.escape(name), src, re.M)
    assert m, "%s is gone — the renderer moved" % name
    return m.group(0).strip()


STUBS = r"""
const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// The bundled app: a page origin that serves no /api. _absUrl repoints MEDIA at the instance; it is
// deliberately not applied to markdown hrefs, which is the whole reason this test exists.
const _absUrl = u => (String(u).charAt(0)==='/' ? 'https://instance.example'+u : u);
const _aiFileActions = () => '';
const _ai = {};
const _fcRender = () => '';
"""


def _harness(src, *, with_rule=True):
    body = "\n".join([
        STUBS,
        _fn(src, "_mdUrl", "function _mdUrl(u){"),
        _fn(src, "mdInline", "function mdInline(s){"),
        _const(src, "_mdIsDelim"),
        _fn(src, "_mdCells", "function _mdCells(row){"),
        _fn(src, "mdToHtml", "function mdToHtml(src){"),
        _const(src, "_AI_LABEL_MARKER"),
        _fn(src, "_artName", "function _artName(label, u){"),
        _fn(src, "aiFormat", "function aiFormat(src){"),
    ])
    if not with_rule:
        # Drop exactly the /api/files/ stash rule and its arrow body, leaving the rest of aiFormat
        # as it ships — this is what the renderer did before the fix.
        body, n = re.subn(
            r"src=src\.replace\(/\\\[\(\[\^\\\]\]\+\)\\\]\\\(\\s\*\(\\/api\\/files\\/.*?\n\s*\}\);\n",
            "", body, count=1, flags=re.S)
        assert n == 1, "could not find the /api/files/ rule to remove — it was renamed or moved"
    return body


PAGE = """<!doctype html><meta charset="utf-8"><script>
%s
const out = {};
const box = document.createElement('div');
const render = t => { box.innerHTML = aiFormat(t); return box; };
{
  const el = render(%s);
  const b = el.querySelector('button.ai-dlfile');
  out.buttons  = el.querySelectorAll('button.ai-dlfile').length;
  out.dataUrl  = b ? b.getAttribute('data-url') : null;
  out.dataName = b ? b.getAttribute('data-name') : null;
  out.label    = b ? b.textContent.trim() : null;
  // The failure shape: an href the bundled shell resolves against its own origin.
  out.apiAnchors = [...el.querySelectorAll('a')].map(a => a.getAttribute('href'))
                     .filter(h => h && h.indexOf('/api/files/') === 0).length;
}
{
  const el = render('see [the docs](https://example.com/docs) for more');
  const a = el.querySelector('a');
  out.plainHref  = a ? a.getAttribute('href') : null;
  out.plainLabel = a ? a.textContent : null;
}
document.title = JSON.stringify(out);
</script>"""


CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome") or shutil.which("google-chrome"))


def _run(page_src):
    """Render the page and read the result back out of <title>.

    No `--remote-debugging-port`: `--dump-dom` does not need one, and a fixed port is a collision
    waiting to happen when the suite runs six checks at once (the reason PC_CHECK_PORT exists at
    all). Its own profile dir per call, for the same reason."""
    if not CHROME:
        raise unittest.SkipTest("no chrome on this host")
    d = tempfile.mkdtemp(prefix="pc-ailink-")
    try:
        page = os.path.join(d, "t.html")
        with open(page, "w") as fh:
            fh.write(page_src)
        out = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=2500",
             "--user-data-dir=" + os.path.join(d, "profile"), "--dump-dom", "file://" + page],
            capture_output=True, text=True, timeout=180).stdout
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        if not m:
            raise AssertionError("the page did not render:\n" + out[:2000])
        return json.loads(re.sub(r"&quot;", '"', m.group(1)))
    finally:
        shutil.rmtree(d, ignore_errors=True)


class AiArtifactLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP) as fh:
            cls.src = fh.read()

    def test_artifact_is_a_button(self):
        o = _run(PAGE % (_harness(self.src), json.dumps(BACKUP_MSG)))
        self.assertEqual(o["buttons"], 1, "the workspace backup did not become a download button")
        self.assertEqual(o["apiAnchors"], 0,
                         "a bare <a href='/api/files/…'> survived — that is the link the bundled "
                         "app resolves against its own origin and reports as not found")
        self.assertTrue(o["dataUrl"].startswith("/api/files/"),
                        "data-url must stay RELATIVE so the fetch goes through the shim: "
                        + repr(o["dataUrl"]))

    def test_name_loses_the_glyph(self):
        o = _run(PAGE % (_harness(self.src), json.dumps(BACKUP_MSG)))
        self.assertEqual(o["dataName"], "sandbox-workspace.tar.gz",
                         "the ⬇️ from the server's label leaked into the filename: "
                         + repr(o["dataName"]))

    def test_ordinary_links_intact(self):
        o = _run(PAGE % (_harness(self.src), json.dumps(BACKUP_MSG)))
        self.assertEqual(o["plainHref"], "https://example.com/docs")
        self.assertEqual(o["plainLabel"], "the docs")

    def test_check_can_fail(self):
        """Without the rule the same message DOES produce the un-resolvable anchor."""
        o = _run(PAGE % (_harness(self.src, with_rule=False), json.dumps(BACKUP_MSG)))
        self.assertEqual(o["buttons"], 0)
        self.assertEqual(o["apiAnchors"], 1,
                         "removing the rule did not reproduce the bug — the harness is not "
                         "exercising the path this test claims to cover")


if __name__ == "__main__":
    unittest.main()
