"""THE MONERO WALLET HAS TO BE A REAL APP ON EVERY SHELL, NOT JUST A SIDEBAR ROW.

The wallet ships as a view. A view is only an *app* once each of the three shells can put it on a
home screen, name it and draw it:

  * **the web client** — a sidebar row, which is where everything else is derived from;
  * **PosterChanOS / the windowed desktop** — a desktop icon, a start-menu entry and a taskbar
    label, all built by `os.js:apps()` from that same sidebar;
  * **the Android launcher** — a tile in `HomeTiles`, drawn by Android from a transcribed sprite
    glyph, because the launcher is native and cannot reach `sprite.js`.

Two gaps this file exists for, both of which leave a working feature looking unfinished:

1. **No launcher tile at all.** The Android catalogue carried Passwords, Signer, Budget and Markets
   but no wallet, so there was no way to put Monero on the phone's home screen. A tile is not
   cosmetic on this build — the launcher IS the home screen.

2. **A desktop icon that was a grey square.** `apps()` reads each row's icon from
   `btn.querySelector('svg use')`. The wallet's row deliberately draws the Monero ɱ as text rather
   than a sprite symbol, so the lookup found nothing, and `appIcon` fell back to `i-grid` — the
   same anonymous square every unrecognised app gets. The row now declares `data-icon`, keeping its
   ɱ in the sidebar while telling the desktop which symbol to draw.

The launcher half runs the real `HomeTiles` under java; the desktop half runs the shipped `apps()`
against the shipped `templates/client.html`.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OS_JS = ROOT / "static/js/client/os.js"
CLIENT_HTML = ROOT / "templates/client.html"
SPRITE = ROOT / "static/js/client/sprite.js"
TILES = ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java"
TILE_ICONS = ROOT / "mobile/android/app/src/main/java/place/poster/app/home/TileIcons.java"
DRAWABLE = ROOT / "mobile/android/app/src/main/res/drawable"

WALLET_VIEW = "wallet"
COIN = "#i-coin"


# --------------------------------------------------------------------------- the web client row


def test_the_sidebar_has_the_wallet_and_it_names_its_icon():
    """Everything else is derived from this row, so it is the one thing that must be right."""
    html = CLIENT_HTML.read_text(encoding="utf-8")
    row = re.search(r'<button class="nav-item" data-view="wallet"[^>]*>', html)
    assert row, "the Monero Wallet is not in the sidebar at all"
    assert 'data-icon="#i-coin"' in row.group(0), (
        "the wallet row draws its own ɱ mark and does not declare a sprite icon, so every shell "
        "built from the sidebar falls back to the anonymous i-grid square")


def test_the_coin_symbol_actually_exists_in_the_sprite():
    """`<use href="#i-nope">` resolves to nothing and draws nothing, with no error anywhere — the
    exact failure mode os.js already carries a comment about."""
    assert 'id="i-coin"' in SPRITE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the desktop


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_desktop_gives_the_wallet_its_own_icon_not_the_grey_fallback():
    """Runs the SHIPPED `apps()` reader over the SHIPPED sidebar markup. A regression here is
    invisible in review — the app still opens, it is just unrecognisable among thirty icons."""
    src = OS_JS.read_text(encoding="utf-8")
    reader = re.search(r"\n  function apps\(\)\{.*?\n  \}", src, re.S)
    assert reader, "apps() is gone from os.js"
    reader = reader.group(0)
    html = CLIENT_HTML.read_text(encoding="utf-8")
    rows = re.findall(r'<button class="nav-item"[^>]*data-view="[^"]+"[^>]*>.*?</button>', html, re.S)
    assert rows, "no sidebar rows found in the shipped template"

    script = r"""
    const rows = %s;
    function el(tag){
      const m = /data-view="([^"]+)"/.exec(tag), i = /data-icon="([^"]+)"/.exec(tag);
      const useM = /<use href="([^"]+)"/.exec(tag);
      return {
        dataset: { view: m ? m[1] : '', icon: i ? i[1] : '' },
        querySelector: s => (s === 'svg use' && useM
                             ? { getAttribute: a => (a === 'href' ? useM[1] : null) } : null),
        textContent: (/<span>([^<]*)<\/span>/.exec(tag) || [,''])[1],
      };
    }
    const nodes = rows.map(el);
    globalThis.$$ = () => nodes;
    globalThis.EXTRAS = [];
    globalThis._navLabel = b => b.textContent;
    globalThis._navGone = () => false;
%s
    process.stdout.write(JSON.stringify(apps().filter(a => a.view === 'wallet')));
    """ % (json.dumps(rows), reader)

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "apps.js"
        f.write_text(script)
        done = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr[-2000:]
        got = json.loads(done.stdout)

    assert got, "the wallet is not in the desktop's app list"
    app = got[0]
    assert app["icon"] == COIN, (
        f"the desktop resolved the wallet's icon to {app['icon']!r} — anything but the coin means "
        f"appIcon() draws the generic i-grid square")
    assert "Monero" in app["label"]


def test_the_desktop_reader_prefers_a_real_use_href_over_the_declared_fallback():
    """`data-icon` is a FALLBACK, not an override: every other row keeps deriving its icon from the
    sprite `<use>` it already draws, so this cannot quietly become a second source of truth."""
    src = OS_JS.read_text(encoding="utf-8")
    reader = src[src.index("const use = btn.querySelector('svg use');"):][:700]
    assert "use ? (use.getAttribute('href')" in reader
    assert reader.index("use.getAttribute('href')") < reader.index("btn.dataset.icon"), (
        "the declared icon is being consulted before the row's own <use>")


def test_the_desktop_widget_and_the_app_agree_on_the_coin():
    """The wallet also ships a desktop WIDGET. Two surfaces for one app must not use two glyphs."""
    src = OS_JS.read_text(encoding="utf-8")
    widget = src[src.index("label:'Monero wallet'"):][:200]
    assert COIN in widget


# --------------------------------------------------------------------------- the Android launcher


def test_the_launcher_offers_a_monero_tile():
    """On this build the launcher IS the home screen, so 'no tile' means the wallet cannot be put
    on it at all — reachable only by opening the app and finding the sidebar."""
    java = TILES.read_text(encoding="utf-8")
    row = re.search(r'new Tile\("wallet",\s*"([^"]+)",\s*"([^"]+)",\s*(true|false)\)', java)
    assert row, "there is no Monero tile in the launcher catalogue"
    label, icon, default_on = row.groups()
    assert "Monero" in label
    assert icon == "coin"
    assert default_on == "false", (
        "the wallet ships ON by default — forty tiles on first run is a worse home screen than "
        "none, and this one is opt-in like every other non-essential view")


def test_the_tile_view_slug_is_the_one_the_client_actually_switches_to():
    """A tile's `view` is handed straight to the client's switchView. A slug that no sidebar row
    answers to is a home-screen icon that opens the app and lands nowhere."""
    java = TILES.read_text(encoding="utf-8")
    html = CLIENT_HTML.read_text(encoding="utf-8")
    app_js = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    # Reachable means either a sidebar row carrying the slug, or a branch in switchView that
    # answers to it — `music` is the second kind (its row is `id="nav-music"`, not a data-view).
    slugs = set(re.findall(r'data-view="([^"]+)"', html))
    for view in re.findall(r'new Tile\("([a-z0-9]+)",', java):
        if view == "app":
            continue
        assert view in slugs or f"VIEW==='{view}'" in app_js, (
            f"launcher tile {view!r} matches no sidebar row and no switchView branch — that tile "
            f"opens the app and lands nowhere")


def test_the_tile_icon_is_a_real_drawable_the_compiler_can_see():
    """`TileIcons` is a switch precisely so a typo is a compile error rather than a silent 0 — but
    the drawable itself still has to be on disk, and it is generated, not written."""
    assert '"coin".equals(icon)' in TILE_ICONS.read_text(encoding="utf-8")
    assert (DRAWABLE / "ic_pc_coin.xml").exists(), "ic_pc_coin.xml was never generated"
    xml = (DRAWABLE / "ic_pc_coin.xml").read_text(encoding="utf-8")
    assert "<vector" in xml and "pathData" in xml
    assert "#FFFFFFFF" in xml.upper() or "strokeColor" in xml, (
        "the glyph must stroke white so the launcher can tint it per theme")


def test_the_launcher_glyph_is_transcribed_from_the_same_sprite_the_web_uses():
    """Two icon sets drift. The generator is the only thing that keeps the phone's Monero tile and
    the desktop's Monero icon the same shape, so the coin has to be in its WANTED list."""
    gen = (ROOT / "scripts/gen_android_icons.py").read_text(encoding="utf-8")
    wanted = gen[gen.index("WANTED = ["):gen.index("]", gen.index("WANTED = ["))]
    assert '"coin"' in wanted, "the coin is not transcribed for Android, so the tile has no glyph"


def test_regenerating_the_icons_changes_nothing():
    """The drawables are checked in AND generated. This is the only thing that can tell you the
    committed file still matches the sprite it claims to be a transcription of."""
    done = subprocess.run(["python3", str(ROOT / "scripts/gen_android_icons.py"), "--check"],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
