"""THE WALLET IS SIZED BY THE SPACE IT IS IN, NOT BY THE SIZE OF THE SCREEN.

`#feed` is shared by every view, and on the windowed desktop it lives inside an `.osw` window whose
width has nothing to do with the viewport. Every responsive rule the wallet had was an `@media`
query, so in a window the phone layout simply never fired. Measured before the fix, in a 480px
window on a 2560px screen:

    balance   44px, wrapped to THREE LINES (99px tall) for one number
    actions   still a row — Send and Receive jammed side by side
    history   still the three-column grid, amount column crushed
    padding   still the full desktop 18px

Nothing overflowed, so an overflow test saw a clean screen. It just did not fit.

The invariant this file holds is the one that was actually violated, and it is stronger than any
list of breakpoints: **at a given container width the layout is the same whatever the viewport is
doing.** A rule that reads the screen instead of the container breaks it immediately, and no future
breakpoint can be added in the wrong unit without this failing.

The markup is produced by the SHIPPED module rather than written here, so the thing measured is the
thing that paints. The modal is deliberately excluded — a dialog is sized by the screen, not by any
window, and its rules are correctly still `@media`.
"""
import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WALLET_JS = ROOT / "static/js/client/monero-wallet.js"
WALLET_CSS = ROOT / "static/css/monero-wallet.css"
CSS = (ROOT / "static/css/client.css").read_text() + "\n" + WALLET_CSS.read_text()
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

pytestmark = pytest.mark.skipif(
    not CHROME or shutil.which("node") is None, reason="Chrome and node required")

#: Paint the real view with a stubbed wallet and hand back the HTML the module produced.
RENDER = r"""
globalThis.window = globalThis;
const feed = { innerHTML: '' };
globalThis.document = {
  getElementById: id => (id === 'feed' ? feed : null),
  createElement: () => ({ style:{}, classList:{add(){},remove(){}}, appendChild(){}, setAttribute(){},
                          remove(){}, select(){}, setSelectionRange(){} }),
  body: { appendChild(){} }, querySelector: () => null, querySelectorAll: () => [], addEventListener(){},
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.__PC = { VIEW:'wallet', $: s => (s === '#feed' ? feed : null),
                    toast(){}, modal(){}, closeModal(){} };
const A = '5' + 'A'.repeat(94);
globalThis.fetch = async (u) => {
  const k = String(u).split('?')[0];
  const b = k.endsWith('/status')
      ? { network:'stagenet', mainnet:false, transfer_cap:'0.1', daily_cap:'0.5',
          warning:'Stagenet testing wallet — funds have no value' }
    : k.endsWith('/balance') ? { balance:'1234.567890123456', unlocked_balance:'1234.567890123456' }
    : k.endsWith('/address') ? { address: A }
    : { in:[{amount:'9007199.254740993123',timestamp:1700000000},
            {amount:'0.000000000001',timestamp:1700000900}],
        out:[{amount:'12.5',timestamp:1700001000}], pending:[], failed:[] };
  return new Response(JSON.stringify(b), {status:200, headers:{'Content-Type':'application/json'}});
};
require(process.argv[2]);
setTimeout(async () => {
  await window.PCMoneroWallet.render(true);
  process.stdout.write(feed.innerHTML);
}, 20);
"""


def _markup():
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "render.js"
        script.write_text(RENDER)
        done = subprocess.run(["node", str(script), str(WALLET_JS)],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-2000:]
        html = done.stdout
        assert "mw-wrap" in html and "mw-balance" in html, f"the view painted nothing usable: {html[:300]}"
        return html


MARKUP = None


def markup():
    global MARKUP
    if MARKUP is None:
        MARKUP = _markup()
    return MARKUP


PROBE = r"""
<pre id="out"></pre><script>
requestAnimationFrame(() => {
  const host = document.getElementById('host'), hb = host.getBoundingClientRect();
  const cs = s => getComputedStyle(host.querySelector(s));
  const box = s => { const e = host.querySelector(s); const r = e.getBoundingClientRect();
                     return { w: Math.round(r.width), h: Math.round(r.height) }; };
  const overflowing = [...host.querySelectorAll('*')].filter(x => {
    const r = x.getBoundingClientRect();
    return r.right > hb.right + 1 || r.left < hb.left - 1;
  }).map(x => String(x.className || x.tagName).slice(0, 40));
  out.textContent = JSON.stringify({
    hostW: Math.round(hb.width), scrollW: host.scrollWidth, overflowing,
    balanceFont: Math.round(parseFloat(cs('.mw-balance>strong').fontSize)),
    balanceBox: box('.mw-balance>strong'),
    balanceLineHeight: parseFloat(cs('.mw-balance>strong').lineHeight),
    actionsDir: cs('.mw-actions').flexDirection,
    txCols: cs('.mw-tx').gridTemplateColumns.split(' ').length,
    cardPad: cs('.mw-card').paddingLeft,
    logo: box('.mw-logo'),
  });
});
</script>"""


def measure(viewport_w, container_w=None, viewport_h=900):
    """Render the wallet in a container of `container_w` on a `viewport_w` screen."""
    width = f"width:{container_w}px;" if container_w else "width:100%;"
    html = f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>html,body{{margin:0;width:100%;height:100%}}{CSS}
    #host{{{width}height:{viewport_h - 40}px;overflow:auto;box-sizing:border-box}}</style>
    <div id="host">{markup()}</div>{PROBE}"""
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "wallet.html"
        page.write_text(html)
        done = subprocess.run([
            CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--window-size={viewport_w},{viewport_h}", "--force-device-scale-factor=1",
            "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-1200:]
        match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert match, done.stdout[-1200:]
        return json.loads(unescape(match.group(1)))


# --------------------------------------------------------------------------- the invariant


@pytest.mark.parametrize("container", [330, 420, 480, 620, 760])
def test_the_layout_depends_on_the_container_and_not_on_the_screen(container):
    """THE BUG, stated as a property. The same width of space must produce the same wallet whether
    that space is a phone screen or a small window on a 2560px desktop. Before the fix these two
    measurements disagreed on the font size, the button direction and the column count at every
    width below 600."""
    on_a_phone = measure(viewport_w=max(container, 360), container_w=container)
    in_a_window = measure(viewport_w=2560, container_w=container)
    for key in ("balanceFont", "actionsDir", "txCols", "cardPad", "logo"):
        assert on_a_phone[key] == in_a_window[key], (
            f"at a {container}px container the wallet renders differently on a 2560px screen: "
            f"{key} was {on_a_phone[key]!r} vs {in_a_window[key]!r} — a rule is reading the "
            f"viewport where it should be reading the container")


def test_a_narrow_window_on_a_big_screen_gets_the_compact_layout():
    """The measured failure, named. A 480px window used to keep the full desktop layout."""
    got = measure(viewport_w=2560, container_w=480)
    assert got["actionsDir"] == "column", "Send and Receive are still jammed side by side"
    assert got["txCols"] == 2, "the transaction list still uses the wide three-column grid"
    assert got["balanceFont"] < 44, "the balance is still at its full desktop size"


def test_the_balance_stays_on_one_line_in_a_narrow_window():
    """The most visible half of the report: one number wrapping to three lines. Checked as a
    measurement — box height against line height — rather than as a font size, because it is the
    wrapping that looks broken and a future font change must not be allowed to reintroduce it."""
    for container in (360, 420, 480, 620):
        got = measure(viewport_w=2560, container_w=container)
        lines = got["balanceBox"]["h"] / max(1.0, got["balanceLineHeight"])
        assert lines < 1.6, (
            f"the balance wraps to {lines:.1f} lines in a {container}px window "
            f"({got['balanceBox']['h']}px tall at {got['balanceFont']}px)")


def test_a_wide_window_still_gets_the_full_desktop_layout():
    """The fix must not make every window look like a phone."""
    got = measure(viewport_w=2560, container_w=900)
    assert got["actionsDir"] == "row"
    assert got["txCols"] == 3
    assert got["balanceFont"] == 44


@pytest.mark.parametrize("container", [300, 330, 360, 412, 480, 620, 760, 1100])
def test_nothing_overflows_its_container_at_any_width(container):
    """The property the old test had, kept: fitting is not only about breakpoints."""
    got = measure(viewport_w=2560, container_w=container)
    assert got["overflowing"] == [], f"these escaped a {container}px container: {got['overflowing']}"
    assert got["scrollW"] <= got["hostW"] + 1


def test_the_phone_screen_itself_is_unchanged():
    """A real phone must keep the layout it already had — including the safe-area padding and the
    room for the bottom nav, which are facts about the SCREEN and correctly stay in a media query."""
    got = measure(viewport_w=360, container_w=None, viewport_h=740)
    assert got["actionsDir"] == "column"
    assert got["txCols"] == 2
    assert got["overflowing"] == []


def test_the_wrap_is_a_query_container_and_the_screen_rules_are_only_screen_facts():
    """Names the split so it cannot quietly drift back. Everything inside the wallet keys off the
    container; what stays on the viewport is the phone's safe areas, its bottom nav and the modal —
    a dialog is sized by the screen, not by whatever window raised it."""
    css = WALLET_CSS.read_text()
    assert "container:mw/inline-size" in css, "the wrap is no longer a query container"
    assert "@container mw (max-width:600px)" in css and "@container mw (max-width:340px)" in css
    balance = css.split(".mw-balance>strong{", 1)[1].split("}", 1)[0]
    assert "vw" not in balance, "the balance is sizing itself off the viewport again"
    assert "cqi" in balance, "the balance no longer scales with the container"
    # The modal legitimately keeps `vw`: a dialog is sized by the screen, not by a window.
    confirm = css.split(".mw-confirm strong{", 1)[1].split("}", 1)[0]
    assert "vw" in confirm
    # Compare whole SELECTORS, never substrings: `.mw-modal .mw-actions` legitimately lives in a
    # media block, and a substring check reads it as the bare `.mw-actions` rule.
    layout = {".mw-actions", ".mw-tx", ".mw-logo", ".mw-balance", ".mw-card", ".mw-head"}
    for block in re.findall(r"@media\([^)]*\)\{(.*?)\}\s*(?=@|$)", css, re.S):
        for rule in block.split("}"):
            if "{" not in rule:
                continue
            for selector in rule.split("{", 1)[0].split(","):
                assert selector.strip() not in layout, (
                    f"the layout rule for {selector.strip()!r} is back in an @media block — inside "
                    f"a desktop window that rule can never fire, which is the whole bug")
