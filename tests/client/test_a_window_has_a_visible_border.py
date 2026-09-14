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
    start = OSWIN.index('  function installFrame(){')
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


# --------------------------------------------------- a reply composer opened as its own window
#
# THE FIFTH SURFACE, and the one that shipped without a border because nothing here looked at it.
# Reported as "ok one reply modal did not have the rounder corners / window border, not sure how to
# explain it" — hard to explain because it is the same composer either way. In the page it is a
# `.modal` with its own 18px radius; opened from the desktop it is a compositor toplevel whose card
# deliberately fills the window (`.os-popup-compose #modal-root .modal` zeroes the radius, so the
# 72%-opaque backdrop stops painting a hard dark box around the card). That left one window on the
# desk with no border of any kind, while every other window on it had one.
#
# It runs the SHIPPED `installFrame` — the same function oswin.js calls — against the shipped
# `.os-popup-compose` markup, so a frame that stops being installed here fails here.
COMPOSE = """
const root = window;
%s
window.__hasFocus = true;
Object.defineProperty(document, 'hasFocus', { value: () => window.__hasFocus });
document.body.className = 'os-popup-body os-popup-compose';
document.body.innerHTML = '<div id="modal-root"><div class="modal-bg">'
  + '<div class="modal"><h3>Reply</h3></div></div></div>';
installFrame();
""" % _installChrome()


def test_a_reply_opened_as_its_own_window_has_a_border_like_every_other_window(page):
    """The composer is the same composer; the WINDOW has to look like a window."""
    with _open(page, _page('', COMPOSE)):
        assert page.evaluate("!!document.getElementById('pc-oswin-frame')"), (
            "a reply composer opened from the desktop installs no border element. It is a "
            "compositor toplevel like every other window on that desk, and it was the only one "
            "without a frame — the card fills the whole window, so nothing else can draw one.")
        _measure_popped_out(page)


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

    # THE CORNERS, which the edge scan above cannot see and which were the actual report
    # ("the fucking corners are not drawn"). Two separate things are checked, because a browser
    # CANNOT reproduce the cause:
    #   1. the stroke closes around each corner (measurable here);
    #   2. the frame is ROUNDED (a rule, not a pixel). The corners went missing because a SQUARE
    #      border at inset:0 puts its corner pixels exactly where the compositor rounds the toplevel
    #      away. Nothing rounds anything in headless Chrome, so a square frame's corners look
    #      perfect here — which is precisely why this has to be pinned as a rule instead.
    for cx, cy in ((0, 0), (899, 0), (0, 599), (899, 599)):
        box = [focused.getpixel((min(899, max(0, cx + dx)), min(599, max(0, cy + dy))))
               for dx in range(-22, 23) for dy in range(-22, 23)]
        interior = focused.getpixel((450, 300))
        best = max(_distance(p, interior) for p in box)
        assert best >= VISIBLE, (
            f'the border does not close at corner ({cx},{cy}): strongest pixel within 22px is '
            f'{best:.0f} from the page behind it (need {VISIBLE}). Four edges that do not meet '
            'read as four lines, not a window.')

    css = (Path(__file__).resolve().parents[2] / 'static/css/client.css').read_text(encoding='utf-8')
    rule = css.split('#pc-oswin-frame{', 1)[1].split('}', 1)[0]
    assert 'border-radius' in rule, (
        'the window frame lost its border-radius. A square frame at inset:0 paints its corners '
        'exactly where the compositor rounds the toplevel away, so on the real desktop the corners '
        'disappear while looking perfect in every browser test.')
    width = int(rule.split('border:', 1)[1].split('px', 1)[0].strip())
    assert width >= 3, (
        f'the window border is {width}px. These are 3840x2560 panels; a 2px edge that reads fine in '
        'a browser is hair-thin there, which is what "better on webui than the actual OS" meant.')

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

