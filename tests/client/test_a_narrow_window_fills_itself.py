"""A popped-out PosterChan window under 821 CSS px wide fills its whole width.

Measured on the test desktop: a Terminal window 763x590 at output scale 1.25 is a 604 CSS-px
viewport, so the phone media query applies, and its guard `html,body,.app,.main,.feed{max-width:100vw}`
capped the body at 604 LAYOUT px while the body carries the desktop's zoom (.67). Drawn, that is
405 px: the terminal and its controls stopped two thirds of the way across and the wallpaper showed
through the rest. After the fix, 604 of 604.

Measured in real Chrome against the SHIPPED stylesheet, because the rule that broke it is correct
text in the file and only wrong once zoom and a viewport width meet.
"""
import pytest

from tests.client.test_a_window_has_a_visible_border import _open, page  # noqa: F401
from tests.client.test_emoji_pack_tabs_layout import chrome  # noqa: F401
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'static/css/client.css').read_text(encoding='utf-8')

BODY = ('<div id="app" class="app"><main class="main"><div id="feed" class="feed feed-term">'
        '<div id="probe" style="width:100%;height:40px;background:#0ff"></div></div></main></div>')


def _doc(css, scale):
    return ('<!doctype html><html class="pc-oswin" style="--ui-scale:%s">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>%s</style><body class="native desktop">%s</body></html>' % (scale, css, BODY))


def _widths(page, css, scale, width):
    page.command('Emulation.setDeviceMetricsOverride',
                 {'width': width, 'height': 600, 'deviceScaleFactor': 1, 'mobile': False})
    with _open(page, _doc(css, scale)):
        return page.evaluate(
            "({vw:innerWidth, body:Math.round(document.body.getBoundingClientRect().width),"
            " probe:Math.round(document.getElementById('probe').getBoundingClientRect().width)})")


@pytest.mark.parametrize('scale,width', [('0.67', 604), ('0.77', 780), ('0.72', 700), ('1', 604)])
def test_a_narrow_zoomed_window_is_filled_edge_to_edge(page, scale, width):  # noqa: F811
    got = _widths(page, CSS, scale, width)
    assert got['body'] >= got['vw'] - 1, got
    assert got['probe'] >= got['vw'] - 1, (
        'the view inside a %spx window at zoom %s is only %spx wide' % (width, scale, got['probe']))


def test_a_wide_window_is_unchanged(page):  # noqa: F811
    got = _widths(page, CSS, '0.67', 1200)
    assert got['body'] >= got['vw'] - 1, got


def test_the_measurement_can_fail(page):  # noqa: F811
    """Without the rule, the shipped stylesheet reproduces the 405-of-604 window from the desk."""
    start = CSS.index('/* A NARROW WINDOW FILLED TWO THIRDS OF ITSELF.')
    end = CSS.index('}\n}', start) + 3
    got = _widths(page, CSS[:start] + CSS[end:], '0.67', 604)
    assert got['probe'] <= 410, got
