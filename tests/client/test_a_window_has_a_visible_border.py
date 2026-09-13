"""A WINDOW'S BORDER IS MEASURED OFF A SCREENSHOT, BECAUSE THE LAST ONE WAS ONLY EVER READ.

Reported as "desktop and laptop still missing the window border thing like hyprland", after a
gradient ring had already shipped for exactly that request and `test_the_focused_window_wears_a_
gradient.py` was green the whole time. That test reads `client.css` as TEXT. A rule can be perfect
in the file and paint nothing on a screen, and this is the difference between the two questions.

What was actually happening, measured with `grim` on both machines and with Wayfire's IPC:

    5,7  place.poster.desktop  "PosterChan Desktop"             3840x2560   (one per output)
    13   place.poster.desktop  "PosterChan Window - terminal"   1653x998
    199  place.poster.desktop  "PosterChan Window - calendar"   3053x2205

Every window on those desks is a COMPOSITOR TOPLEVEL -- a popped-out window -- not an in-page
`.osw`. Two mechanisms exist to draw a border and neither covers that surface: `.osw`'s CSS is a
different document, and `wayfire.ini`'s `[decoration]` deliberately ignores this app_id (the
full-output desktop shell shares it). Scanned column by column, the screenshots carried not one
accent pixel down either window's edge, focused or not.

So this suite screenshots and looks, over the FOUR surfaces that can carry a window:

  * a popped-out window, focused and blurred (`#pc-oswin-frame`, installed by the shipped
    `oswin.js`);
  * an in-page `.osw`, focused and not, with desktop effects ON;
  * the same with effects OFF -- `.os-fx-off` is a real state (touch, low power) in which the
    gradient ring is switched off entirely, so it is the state a border must survive.

The thresholds are composites, not taste. Against a window interior at (16,16,24):

    unfocused, before   1px rgba(125,210,255,.16)  ->  (33,47,61)   distance 51
    unfocused, after    2px rgba( 60,232,255,.24)  ->  (27,68,79)   distance 77
    focused,   after    2px rgba( 60,232,255,.85)  ->  (52,199,219) distance 250
    popped-out, before  no element at all          ->  distance 0
"""
import base64
import io
import math
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from tests.client.test_emoji_pack_tabs_layout import chrome  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'static/css/client.css').read_text(encoding='utf-8')
OSWIN = (ROOT / 'static/js/client/oswin.js').read_text(encoding='utf-8')

# An unfocused border still has to be FINDABLE -- that is the whole of "a dimmer one on the windows
# that are not focused". 70 sits above the hairline this replaced (51) and below what it draws (77).
VISIBLE = 70
# And the focused one has to be unmistakable from across a 4K desk, not merely different.
ACCENT = 150


def _installChrome():
    """The shipped popped-out-window chrome, sliced out of oswin.js rather than retyped."""
    start = OSWIN.index('  function installChrome(state){')
    return OSWIN[start:OSWIN.index('  const API = {', start)]


def _shot(browser):
    data = browser.command('Page.captureScreenshot', {'format': 'png'})['data']
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')


def _distance(a, b):
    return math.dist(a, b)


def _edge(image, inside, outside, fixed, vertical=False):
    """How far the strongest pixel between inside and outside sits from the window's interior.

    Returns (distance, colour). Sampling a BAND rather than one pixel because a 2px border lands on
    whole device pixels only when nothing scales, and a test that assumes it does is measuring the
    zoom rather than the border.
    """
    at = (lambda n: (fixed, n)) if vertical else (lambda n: (n, fixed))
    interior = image.getpixel(at(inside))
    lo, hi = sorted((inside, outside))
    best = max((image.getpixel(at(n)) for n in range(lo, hi + 1)),
               key=lambda p: _distance(p, interior))
    return _distance(best, interior), best


def _page(body, script=''):
    return ('<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<style>{CSS}html,body{{margin:0;padding:0;zoom:1!important;'
            'background:var(--bg);height:100%}</style>'
            f'<body>{body}<script>{script}</script></body></html>')


@contextmanager
def _open(browser, html):
    """Served over HTTP, not as a data: URL -- the stylesheet alone is past the CDP frame cap."""
    payload = html.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        browser.command('Page.enable')
        browser.command('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/'})
        browser.evaluate('new Promise(resolve=>setTimeout(resolve,300))')
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def page(chrome):  # noqa: F811
    target = chrome.command('Target.createTarget', {'url': 'about:blank'})['targetId']
    chrome.session = chrome.command(
        'Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
    chrome.command('Emulation.setDeviceMetricsOverride',
                   {'width': 900, 'height': 600, 'deviceScaleFactor': 1, 'mobile': False})
    try:
        yield chrome
    finally:
        chrome.session = None
        chrome.command('Target.closeTarget', {'targetId': target})


# ----------------------------------------------------------------- the popped-out window

POPPED = """
const root = window;
%s
window.__hasFocus = true;
Object.defineProperty(document, 'hasFocus', { value: () => window.__hasFocus });
installChrome({ view:'terminal', label:'terminal', shared:false });
""" % _installChrome()


def test_a_popped_out_window_draws_its_own_border_in_both_focus_states(page):
    """The surface every window on those two desks actually is.

    Before this shipped there was no element here at all: `#pc-oswin-frame` did not exist, Wayfire
    was told to ignore the app_id, and the measured answer was a distance of zero on all four sides.
    """
    with _open(page, _page('<div style="height:100%"></div>', POPPED)):
        _measure_popped_out(page)


def _measure_popped_out(page):
    assert page.evaluate("!!document.getElementById('pc-oswin-frame')"), \
        "a popped-out window installs no border element, so nothing on it can draw one"

    focused = _shot(page)
    page.evaluate("window.__hasFocus=false;window.dispatchEvent(new Event('blur'))")
    page.evaluate('new Promise(r=>setTimeout(r,120))')
    blurred = _shot(page)

    # The frame is `inset:0`, so its edge is the viewport's own edge on all four sides.
    for name, image, floor in (('focused', focused, ACCENT), ('blurred', blurred, VISIBLE)):
        for side, args in {
                'left': ((40, 0, 300), {}), 'right': ((859, 899, 300), {}),
                'top': ((300, 0, 450), {'vertical': True}),
                'bottom': ((300, 599, 450), {'vertical': True}),
        }.items():
            distance, colour = _edge(image, *args[0], **args[1])
            assert distance >= floor, (
                f'{name} popped-out window: the {side} edge is {distance:.0f} from the page behind '
                f'it (need {floor}); strongest pixel {colour}')

    lit, _ = _edge(focused, 40, 0, 300)
    dim, _ = _edge(blurred, 40, 0, 300)
    assert lit > dim * 1.5, (
        f'focus is not legible from the border alone: focused {lit:.0f} vs blurred {dim:.0f}. '
        'Two states that look the same are one state.')


# ----------------------------------------------------------------- the in-page window

DESK = """
<div class="os-root os-fx" id="desk" style="position:relative;height:600px">
  <div class="osw focused" id="a" style="left:60px;top:60px;width:300px;height:200px"></div>
  <div class="osw"         id="b" style="left:500px;top:60px;width:300px;height:200px"></div>
</div>
"""


@pytest.mark.parametrize('effects', ['os-fx', 'os-fx-off'])
def test_an_in_page_window_keeps_its_border_whatever_the_effects_switch_says(page, effects):
    """`.os-fx-off` is where the gradient ring does not exist, and it is a state real machines land in.

    Desktop effects are `auto` by default and `auto` resolves to OFF on touch, so a border that only
    exists on the `.os-fx` path is a border a tablet never has -- and, before this, one an unfocused
    window never had anywhere.
    """
    with _open(page, _page(DESK)):
        page.evaluate(f"document.getElementById('desk').className='os-root {effects}'")
        page.evaluate('new Promise(r=>setTimeout(r,120))')
        image = _shot(page)

    focused, _ = _edge(image, 64, 58, 160)           # the left edge of #a, mid-height
    unfocused, colour = _edge(image, 504, 498, 160)  # ...and of #b
    assert focused >= ACCENT, f'the focused window edge is only {focused:.0f} from its interior'
    assert unfocused >= VISIBLE, (
        f'an unfocused window has no findable edge under {effects}: {unfocused:.0f} from its own '
        f'interior (strongest pixel {colour})')
    assert focused > unfocused * 1.5, (
        f'under {effects} focused {focused:.0f} and unfocused {unfocused:.0f} read the same')


def test_the_border_is_the_theme_accent_and_not_a_typed_colour():
    """Nine themes, one palette. A hardcoded cyan is a blue stripe on the light ones.

    Kept as a source check on purpose: the screenshots above prove a border is PAINTED, and this
    proves it will follow somebody who changes the theme -- which no single screenshot can say.
    """
    for rule in ('.osw{position:absolute', '#pc-oswin-frame{position:fixed'):
        at = CSS.index(rule)
        block = CSS[at:CSS.index('}', at)]
        assert 'var(--accent-rgb)' in block, f'{rule} paints a border from outside the palette: {block}'
    assert '#pc-oswin-frame.focused{border-color:rgba(var(--accent-rgb)' in CSS
    assert '.osw.focused{border-color:rgba(var(--accent-rgb)' in CSS
