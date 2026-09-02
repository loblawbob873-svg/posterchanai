"""HOW MUCH OF A PHONE IS ACTUALLY MAIL — measured in a browser, at real device widths.

Reported as "you also failed to use the screenspace right on the current email implementation", and
before that as "email UI is terrible on mobile, you get less than half the screen now".

The READING pane had already been measured and fixed (`check_mail_mobile.py` asserts `bodyFrac`).
Nothing had ever measured the LIST — the screen you are on for most of the time you are in Mail —
and that is where the pixels were going. Measured against the shipped stylesheet at 360x640 with
six folders, before this change:

    topbar 62 + folder block 130 + search row 73 + select bar 40 + nav 61  =  366px of chrome (57%)
    message list                                                          =  247px        (39%)
    message rows visible                                                  =  3

Four things were paying for that, and three of them were paying for nothing:

* `.mail-side` was `flex-wrap:wrap` with a `width:100%` folder strip, so it was TWO rows — 114px
  with three folders and 130px from four upwards, permanently. It is one row now and the folder
  chips scroll horizontally, which also stops nine folders squeezing into unreadable slivers.
  Fixed at 72px however many folders the server has.
* The bulk bar existed only to hold a "Select" checkbox, so it stood open at 40px for the whole
  time nothing was selected, which is nearly all of it. The checkbox moved into the search row and
  the bar collapses until `updateBulk` puts buttons in it.
* The reader reserved `62px + safe-area` at its bottom for the fixed `.mobilenav`. It is
  `position:fixed;inset:0;z-index:46` with an opaque background and the nav is `z-index:40`, so the
  nav is COVERED — `elementFromPoint` at the bottom strip answers `.mail-read`. Sixty-two pixels of
  a 553px phone spent hiding behind an overlay.

A NOTE ON THE MEASUREMENT ITSELF. Headless Chrome clamps its top-level window to a MINIMUM WIDTH OF
500px — `--window-size=360,640` yields `innerWidth === 500` — so every probe here that claimed to be
measuring a 360px phone was measuring a 500px one. Everything is measured inside an IFRAME sized to
the device instead, which establishes its own viewport for media queries. The numbers above and in
the assertions are the iframe numbers.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html import escape, unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
APPJS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome"))

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")

#: phones people actually hold. 360x640 is the floor the Android WebView still ships.
PHONES = [(360, 640), (390, 844), (412, 915)]

ITEM = ('<div class="mail-item unread"><input type="checkbox" class="mi-chk">'
        '<div class="mi-content"><div class="mi-row"><span class="mi-from">Alice Example</span>'
        '<span class="mi-date">12:04</span></div>'
        '<div class="mi-subj">A perfectly ordinary subject line</div>'
        '<div class="mi-prev muted small">preview</div></div></div>')
FOLDERS = ['\U0001f4e5 Inbox', '\U0001f4e4 Sent', '\U0001f4dd Drafts', '\U0001f5c4 Archive',
           '\U0001f5d1 Trash', '\U0001f6ab Spam', 'Lists', 'Work', 'postponed']
NAV = ''.join('<button class="nav-item"><svg class="ic"></svg><b>Item</b></button>' for _ in range(5))
TOPBAR = ('<header class="topbar glass"><h2>Email</h2>'
          '<div class="searchbox"><input class="input" type="search" placeholder="Search"></div></header>')


def list_markup(nfolders: int, fixed: bool = True) -> str:
    chips = ''.join(f'<button class="mail-folder{" on" if i == 0 else ""}">{FOLDERS[i]}</button>'
                    for i in range(nfolders))
    selall = ('<label class="mail-selall"><input type="checkbox" id="mail-selall"> Select</label>')
    if fixed:
        head = (f'<div class="mail-list-top">{selall}'
                '<input class="input mail-search" placeholder="Search mail">'
                '<button class="mini mail-refresh">R</button></div>'
                '<div class="mail-bulk"><span class="mail-bulk-act"></span></div>')
    else:                                   # the pre-fix arrangement, for the mutation check
        head = ('<div class="mail-list-top">'
                '<input class="input mail-search" placeholder="Search mail">'
                '<button class="mini mail-refresh">R</button></div>'
                f'<div class="mail-bulk">{selall}<span class="mail-bulk-act"></span></div>')
    return (f'{TOPBAR}<div class="feed" style="display:flex;flex-direction:column;min-height:0;flex:1">'
            '<div class="mail-root"><div class="mail-wrap">'
            '<div class="mail-side"><select class="input mail-acct"><option>me@example.com</option></select>'
            f'<button class="btn btn-neon mail-compose">Compose</button><div class="mail-folders">{chips}</div></div>'
            f'<div class="mail-list">{head}<div class="mail-items">{ITEM * 25}</div></div>'
            '<div class="mail-read"><div class="empty">Select a message</div></div>'
            f'</div></div></div><nav class="mobilenav">{NAV}</nav>')


LONG = "<br>".join(f"Paragraph {i} of a long plain-text message." for i in range(80))


def reader_markup(nmsg: int = 1) -> str:
    def block(open_):
        return (f'<div class="mail-msg{" open" if open_ else ""}"><div class="mail-msg-hd">'
                '<span class="mm-avatar">A</span><div class="mm-who"><b class="mm-sender">Alice</b>'
                '<div class="muted small">To: me@example.com</div></div>'
                '<span class="mm-preview muted">p</span><span class="muted small mm-date">12:04</span>'
                '<span class="mm-chevron">v</span></div>'
                f'<div class="mail-msg-body"><div class="mail-body"><div class="mail-text">{LONG}</div>'
                '</div></div></div>')
    acts = ''.join('<button class="btn small icon-only"><svg class="ic b-ic"></svg></button>'
                   for _ in range(7))
    return (f'{TOPBAR}<div class="feed" style="display:flex;flex-direction:column;min-height:0;flex:1">'
            '<div class="mail-root"><div class="mail-wrap"><div class="mail-side">s</div>'
            '<div class="mail-list">l</div><div class="mail-read has-open">'
            '<div class="mail-read-hd"><button class="mini mail-back">B</button>'
            '<div class="mr-meta"><div class="mr-subj">Re: an ordinary email subject line</div>'
            + (f'<div class="muted small">{nmsg} messages</div>' if nmsg > 1 else '')
            + f'</div></div><div class="mail-actions">{acts}</div><div class="mail-thread">'
            + ''.join(block(i == nmsg - 1) for i in range(nmsg))
            + '<div class="mail-thread-reply"><button class="btn btn-cyan">Reply</button>'
              '<button class="btn">Forward</button></div>'
            f'</div></div></div></div></div><nav class="mobilenav">{NAV}</nav>')


PROBE_LIST = r"""
 const R=e=>e.getBoundingClientRect(), Q=s=>D.querySelector(s);
 const vis=e=>e&&(!e.checkVisibility||e.checkVisibility());
 const h=s=>{const e=Q(s);return e&&vis(e)?Math.round(R(e).height):0};
 const items=Q('.mail-items'), r=R(items), navH=h('.mobilenav'), vh=W.innerHeight;
 let rows=0; D.querySelectorAll('.mail-item').forEach(e=>{const b=R(e);
   if(b.top>=r.top-1 && b.bottom<=Math.min(vh-navH,r.bottom)+1) rows++;});
 res={vh, vw:W.innerWidth, topbar:h('.topbar'), side:h('.mail-side'), search:h('.mail-list-top'),
      bulk:h('.mail-bulk'), nav:navH, rows,
      listBand:Math.round(Math.min(vh-navH,r.bottom)-r.top),
      overflow: D.documentElement.scrollWidth > W.innerWidth+1,
      selallVisible: !!(Q('#mail-selall') && vis(Q('#mail-selall'))),
      chipMinH: Math.min(...[...D.querySelectorAll('.mail-folder')].map(e=>Math.round(R(e).height))),
      chipsScroll: (()=>{const f=Q('.mail-folders');return f.scrollWidth>f.clientWidth+1;})()};
"""

PROBE_READ = r"""
 const R=e=>e.getBoundingClientRect(), Q=s=>D.querySelector(s);
 const read=Q('.mail-read'), cs=W.getComputedStyle(read), vh=W.innerHeight;
 const pb=Math.round(parseFloat(cs.paddingBottom)||0);
 const hd=R(Q('.mail-read-hd')), rr=R(Q('.mail-thread-reply'));
 const at=D.elementFromPoint(Math.round(W.innerWidth/2), vh-6);
 res={vh, vw:W.innerWidth, padBottom:pb,
      bottomStrip: at ? (at.closest('.mobilenav')?'mobilenav':(at.closest('.mail-read')?'mail-read':'other')) : 'none',
      band:Math.round(Math.min(rr.top, vh-pb)-hd.bottom)};
"""


def measure(markup: str, probe: str, width: int, height: int, css: str = CSS) -> dict:
    """Render `markup` in an iframe of exactly (width, height) and run `probe` inside it.

    The iframe is the whole point: headless Chrome will not give a top-level window narrower than
    500px, so measuring a 360px phone any other way measures a 500px one."""
    inner = ('<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">'
             '<style>html,body{margin:0;height:100%;display:flex;flex-direction:column}'
             + css + '</style>' + markup)
    page = ('<!doctype html><style>html,body{margin:0}iframe{border:0;display:block}</style>'
            f'<iframe id="f" width="{width}" height="{height}" '
            f'srcdoc="{escape(inner, quote=True)}"></iframe><pre id="out"></pre>'
            '<script>const f=document.getElementById("f");setTimeout(()=>{'
            'const W=f.contentWindow,D=W.document;let res;' + probe
            + 'out.textContent=JSON.stringify(res);},600);</script>')
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "mail.html"
        html.write_text(page, encoding="utf-8")
        done = subprocess.run(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--window-size=1400,1100", "--force-device-scale-factor=1",
             "--virtual-time-budget=4000", "--dump-dom", html.as_uri()],
            capture_output=True, text=True, timeout=180)
        found = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert found and found.group(1).strip(), (done.stdout[-1500:], done.stderr[-800:])
        got = json.loads(unescape(found.group(1)))
        assert got["vw"] == width, (
            f"the probe measured {got['vw']}px, not {width}px — the iframe sizing is wrong and "
            f"every number here is about a different screen")
        return got


def prefix_css(css: str = CSS) -> str:
    """The stylesheet as it was before this change, for the mutation checks."""
    swaps = [
        (".mail-read.has-open{display:flex;position:fixed;inset:0;z-index:46;background:var(--bg);"
         "padding-bottom:env(safe-area-inset-bottom)}",
         ".mail-read.has-open{display:flex;position:fixed;inset:0;z-index:46;background:var(--bg);"
         "padding-bottom:calc(62px + env(safe-area-inset-bottom))}"),
        ("  .mail-side{flex:0 0 auto;flex-direction:row;flex-wrap:nowrap;align-items:center;gap:8px;"
         "border-inline-end:none;border-bottom:1px solid var(--line);padding:8px;overflow:visible}",
         "  .mail-side{flex:0 0 auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:8px;"
         "border-inline-end:none;border-bottom:1px solid var(--line);padding:8px}"),
        ("  .mail-side .mail-acct{flex:0 1 auto;min-width:0;width:auto;max-width:36%}",
         "  .mail-side .mail-acct{flex:1;min-width:130px;width:auto}"),
        ("""  .mail-folders{flex:1 1 0;min-width:0;flex-direction:row;flex-wrap:nowrap;width:auto;margin-top:0;gap:6px;
    overflow-x:auto;overflow-y:hidden;scrollbar-width:none;-webkit-overflow-scrolling:touch}
  .mail-folders::-webkit-scrollbar{display:none}
  .mail-folder{flex:0 0 auto;text-align:center;white-space:nowrap}""",
         """  .mail-folders{flex-direction:row;width:100%;margin-top:0;gap:6px}
  .mail-folder{flex:1;text-align:center}"""),
        (".mail-bulk:not(:has(.btn)){display:none}", ""),
    ]
    for new, old in swaps:
        assert css.count(new) == 1, f"the stylesheet no longer contains: {new[:70]}"
        css = css.replace(new, old, 1)
    return css


# ── the list view ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("width,height", PHONES)
def test_the_message_list_gets_at_least_half_the_phone(width, height):
    """THE REPORT, AS A NUMBER. Six folders is an ordinary IMAP account."""
    got = measure(list_markup(6), PROBE_LIST, width, height)
    frac = got["listBand"] / got["vh"]
    assert frac >= 0.42, (
        f"{width}x{height}: the message list gets {got['listBand']}px of {got['vh']}px "
        f"({frac:.0%}) — chrome is topbar {got['topbar']} + folders {got['side']} + search "
        f"{got['search']} + bulk bar {got['bulk']} + nav {got['nav']}")


@pytest.mark.parametrize("width,height", PHONES)
def test_at_least_three_messages_are_visible_without_scrolling(width, height):
    """A list showing one row is a list you cannot skim. 360x640 showed three before and four now;
    the floor is set at three so this catches a regression rather than pinning a pixel."""
    got = measure(list_markup(6), PROBE_LIST, width, height)
    assert got["rows"] >= 3, f"{width}x{height}: only {got['rows']} message row(s) fit on screen"


def test_the_folder_count_does_not_cost_screen():
    """THE SECOND ROW. Four folders used to wrap the block from 114px to 130px and it never came
    back down. A server with nine folders must cost exactly what one with three costs."""
    heights = {n: measure(list_markup(n), PROBE_LIST, 360, 640)["side"] for n in (1, 3, 6, 9)}
    assert len(set(heights.values())) == 1, (
        f"the folder rail grows with the folder list: {heights}")


def test_the_folder_chips_stay_tappable_and_reachable():
    """Scrolling is the price of one row; unreadable slivers are not. Nine chips used to share the
    width at `flex:1`. They keep their own width now, so the strip scrolls instead."""
    got = measure(list_markup(9), PROBE_LIST, 360, 640)
    assert got["chipMinH"] >= 32, f"a folder chip is {got['chipMinH']}px tall"
    assert got["chipsScroll"] is True, "nine folders fit a 360px row, so they are being squeezed"
    assert got["overflow"] is False, "the page scrolls sideways"


def test_select_all_is_still_reachable():
    """It moved out of the collapsed bar and must not have moved out of the app."""
    for w, h in PHONES:
        assert measure(list_markup(6), PROBE_LIST, w, h)["selallVisible"] is True


def test_the_bulk_bar_is_gone_until_there_is_a_selection():
    got = measure(list_markup(6), PROBE_LIST, 360, 640)
    assert got["bulk"] == 0, f"the bulk bar still occupies {got['bulk']}px with nothing selected"


def test_the_bulk_bar_comes_back_when_something_is_selected():
    """The collapse is `:not(:has(.btn))`, so the bar must reappear the moment `updateBulk` fills
    it — otherwise the actions are unreachable and the selection is a dead end."""
    markup = list_markup(6).replace(
        '<span class="mail-bulk-act"></span>',
        '<span class="mail-bulk-act"><span class="mail-bulk-n">2 selected</span>'
        '<button class="btn small">Read</button><button class="btn btn-red small">Delete</button></span>')
    got = measure(markup, PROBE_LIST, 360, 640)
    assert got["bulk"] > 0, "the bulk actions are hidden even with a selection"


def test_the_list_budget_check_can_fail():
    """MUTATION. Rebuild the pre-fix stylesheet and markup and prove the measurement goes red on
    them — measured 247px of 553 (39%) at 360x640, which is the report, verbatim."""
    before = measure(list_markup(6, fixed=False), PROBE_LIST, 360, 640, css=prefix_css())
    assert before["listBand"] / before["vh"] < 0.42, (
        f"the pre-fix layout passes this check ({before['listBand']}/{before['vh']}), so the check "
        f"proves nothing")
    assert before["bulk"] > 0 and before["side"] > 100     # …for the reasons named above


def test_the_folder_row_check_can_fail():
    css = prefix_css()
    heights = {n: measure(list_markup(n, fixed=False), PROBE_LIST, 360, 640, css=css)["side"]
               for n in (3, 9)}
    assert len(set(heights.values())) > 1, (
        f"the pre-fix folder rail did not grow with the folder count either: {heights}")


# ── the reading pane ───────────────────────────────────────────────────────────────────────────
def test_the_reader_does_not_reserve_space_for_a_bar_it_covers():
    """`elementFromPoint` at the bottom strip answers `.mail-read`, so the nav is behind the
    overlay. The reserve was 62px of a phone spent on nothing."""
    for w, h in PHONES:
        got = measure(reader_markup(1), PROBE_READ, w, h)
        assert got["bottomStrip"] == "mail-read", (
            f"{w}x{h}: the nav is on top of the reader after all — the reserve is real and this "
            f"change is wrong")
        assert got["padBottom"] < 40, (
            f"{w}x{h}: the reader still reserves {got['padBottom']}px under a bar nothing can see")


@pytest.mark.parametrize("width,height", PHONES)
def test_the_reading_band_is_most_of_the_phone(width, height):
    got = measure(reader_markup(1), PROBE_READ, width, height)
    frac = got["band"] / got["vh"]
    assert frac >= 0.75, (
        f"{width}x{height}: {got['band']}px of {got['vh']}px between the sticky header and the "
        f"reply row ({frac:.0%})")


def test_the_reader_check_can_fail():
    """MUTATION. 442px of 553 (69%) at 360x640 before the reserve went."""
    before = measure(reader_markup(1), PROBE_READ, 360, 640, css=prefix_css())
    assert before["padBottom"] >= 40 and before["band"] / before["vh"] < 0.75, (
        f"the pre-fix reader passes this check ({before}), so the check proves nothing")


# ── the markup the stylesheet is measured against has to be the markup that ships ───────────────
def test_the_shipped_client_puts_select_all_in_the_search_row():
    """The CSS collapse only helps if `Mail.draw` actually moved the checkbox — a stylesheet tested
    against a fixture nobody renders is a stylesheet tested against nothing."""
    row = APPJS.split('<div class="mail-list-top">', 1)[1].split('</div>', 1)[0]
    assert 'id="mail-selall"' in row, "select-all is not in the shipped search row"
    bulk = APPJS.split('<div class="mail-bulk">', 1)[1].split('</div>', 1)[0]
    assert 'id="mail-selall"' not in bulk, "select-all is still in the bulk bar as well"


def test_the_active_folder_is_scrolled_into_view():
    """One row is the fix; the price of one row is that nine folders do not fit in it. The folder
    you are IN has to be brought back — and by writing `scrollLeft` on the strip, never
    `scrollIntoView`, which is free to scroll every ancestor including the page."""
    markup = list_markup(9).replace('<button class="mail-folder on">', '<button class="mail-folder">', 1)
    markup = markup.replace('<button class="mail-folder">postponed</button>',
                            '<button class="mail-folder on" id="active-chip">postponed</button>')
    probe = r"""
     const R=e=>e.getBoundingClientRect(), Q=s=>D.querySelector(s);
     const strip=Q('.mail-folders'), on=Q('#active-chip');
     const l=on.offsetLeft, r=l+on.offsetWidth;
     if(strip.scrollWidth > strip.clientWidth+1){
       if(l < strip.scrollLeft) strip.scrollLeft = Math.max(0, l-8);
       else if(r > strip.scrollLeft+strip.clientWidth) strip.scrollLeft = r-strip.clientWidth+8;
     }
     const sr=R(strip), or_=R(on);
     res={vw:W.innerWidth, visible: or_.left>=sr.left-1 && or_.right<=sr.right+1,
          pageScrolled: W.scrollX !== 0 || W.scrollY !== 0};
    """
    got = measure(markup, probe, 360, 640)
    assert got["visible"], "the folder you are in is scrolled off the strip"
    assert not got["pageScrolled"], "bringing a chip into view scrolled the page"


def test_the_shipped_client_scrolls_the_active_chip_into_view():
    """The rule above is only worth measuring if `Mail.draw` runs it."""
    assert "strip.scrollLeft = " in APPJS and ".mail-folder.on" in APPJS, (
        "nothing in the shipped client brings the active folder chip back into view")
    assert "scrollIntoView" not in APPJS.split(".mail-folders'", 1)[1][:900], (
        "the chip is brought into view with scrollIntoView, which can scroll the whole page")
