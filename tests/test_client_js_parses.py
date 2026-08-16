"""Every shipped JavaScript file PARSES. The cheapest test in the repo, and the one whose absence
took the Windows app out on launch.

WHAT HAPPENED. A comment was extended by inserting lines after the block's closing `*/`, orphaning
them, so `fs-android.js` became `… */` followed by bare `* text` lines. Node reports
`SyntaxError: Unexpected template string`; the desktop app reports `unexpected string` on launch and
does not start. Not a subtle failure — the whole app, on every launch, for everyone who updated.

WHY NOTHING CAUGHT IT. The unit tests exercise the pure modules (`foldersync.js`, `syncrun.js`) and
the simulations load `sync.js`, so a break in either of those fails loudly. `fs-android.js` is loaded
by no test at all — it is a thin platform shim that needs a Capacitor bridge to do anything — and it
is in the SHELL precache list and the desktop bundle, so a syntax error in it is fatal at load and
invisible to everything here. The same is true of most files in this directory: they are shipped as
plain `<script>` tags, and ONE unparseable file is not a broken feature, it is a blank app.

WHY IT IS A SEPARATE FILE FROM test_client_module_deps.py. That one answers a harder question (does
every identifier resolve) with a real parser, needs acorn, and SKIPS when acorn is absent — which is
the common case here. This asks the stupidest possible question, needs nothing but node, and must
never skip quietly on a machine that has node. Dumb and always-on beats clever and conditional for a
failure this total.

It covers the desktop shell too: `main.js` and `preload.js` are the Electron process itself, where a
parse error is not a blank page but an app that will not launch at all.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node") or shutil.which("nodejs")

# Everything the browser, the APK bundle or Electron loads as a script. Discovered, never typed: a
# file added tomorrow is covered the moment it lands, which is the whole reason the check suite
# discovers its scripts rather than listing them.
def _shipped():
    out = []
    for rel in ("static/js/client", "static/js", "desktop"):
        d = os.path.join(ROOT, rel)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".js"):
                out.append(os.path.join(rel, name))
    return out


SHIPPED = _shipped()


@pytest.mark.skipif(not NODE, reason="no node on this node")
@pytest.mark.parametrize("rel", SHIPPED)
def test_the_file_parses(rel):
    r = subprocess.run([NODE, "--check", os.path.join(ROOT, rel)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (
        "%s does not parse, so every page that loads it is blank and the desktop app does not "
        "start:\n%s" % (rel, (r.stderr or r.stdout)[:1200])
    )


def test_there_is_something_to_check():
    """A discovery bug would make every test above vanish and the file report success — the same
    silent-pass shape the check suite treats as a failure rather than a skip."""
    assert len(SHIPPED) > 30, "the shipped-script discovery found almost nothing: %d" % len(SHIPPED)
    assert any(r.endswith("client/fs-android.js") for r in SHIPPED), (
        "the file that actually broke is not in the discovered set"
    )
    assert any(r.endswith("desktop/main.js") for r in SHIPPED)


@pytest.mark.skipif(not NODE, reason="no node on this node")
def test_the_check_can_fail(tmp_path):
    """PROOF IT BITES, written as the exact break that shipped: comment lines orphaned after the
    block's closing `*/`."""
    bad = tmp_path / "broken.js"
    bad.write_text("const a = 1;\n/* fine */\n * `orphaned` continuation\n * more */\nconst b = 2;\n")
    r = subprocess.run([NODE, "--check", str(bad)], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "node --check accepted an orphaned comment block, so this gate is inert"
