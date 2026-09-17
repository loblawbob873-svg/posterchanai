"""System Settings: a tick box sits beside its words, and every select on a page is styled alike.

Measured on the test desktop, Displays page: the "Use this display" checkbox and the "Primary display"
radio were at the LEFT of a 500px grid column with their words at the RIGHT, because the grid's
`label{justify-content:space-between}` (right for "Scale ....... [100%]") applied to them too. And on
Power & brightness the display idle-timeout <select> was a bare native control beside styled ones,
because its row lacked `os-set-control`.

Measured in real Chrome against the SHIPPED stylesheet and the SHIPPED markup where it is static.
"""
from pathlib import Path

from tests.client.test_a_window_has_a_visible_border import _open, page  # noqa: F401
from tests.client.test_emoji_pack_tabs_layout import chrome  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'static/css/client.css').read_text(encoding='utf-8')
OS = (ROOT / 'static/js/client/os.js').read_text(encoding='utf-8')

CONTROLS = ('<div class="os-display-controls" style="width:1000px">'
            '<label id="en"><input type="checkbox" checked> <span id="en-t">Use this display</span></label>'
            '<label>Scale<select><option>100%</option></select></label>'
            '<label id="pr"><input type="radio" checked> <span id="pr-t">Primary display</span></label>'
            '</div>')


def _doc(body):
    return ('<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>%s body{zoom:1!important}</style><body>%s</body></html>' % (CSS, body))


def test_a_tick_box_is_next_to_its_label(page):  # noqa: F811
    with _open(page, _doc(CONTROLS)):
        gaps = page.evaluate("""(()=>{
          const gap=(l,t)=>{const i=document.querySelector(l+' input').getBoundingClientRect();
            const s=document.querySelector(t).getBoundingClientRect(); return Math.round(s.left-i.right);};
          return {en:gap('#en','#en-t'), pr:gap('#pr','#pr-t')};})()""")
    assert gaps['en'] < 40 and gaps['pr'] < 40, gaps


def test_the_idle_timeout_select_is_styled_like_the_others(page):  # noqa: F811
    start = OS.index('<b>Turn display off when idle</b>')
    row = OS.rindex('<section class="', 0, start)
    cls = OS[row + len('<section class="'):OS.index('"', row + len('<section class="'))]
    body = ('<section class="%s"><div><b>Turn display off when idle</b></div>'
            '<select id="idle"><option>2 minutes</option></select></section>'
            '<section class="os-setting-row os-set-control"><div><b>Power mode</b></div>'
            '<select id="mode"><option>powersave</option></select></section>') % cls
    with _open(page, _doc(body)):
        got = page.evaluate("""(()=>{const s=id=>{const c=getComputedStyle(document.getElementById(id));
            return [c.paddingTop,c.backgroundColor,c.minWidth,c.borderRadius];};
            return {idle:s('idle'),mode:s('mode')};})()""")
    assert got['idle'] == got['mode'], got
