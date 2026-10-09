"""TELEGRAM: THE EMOJI PICKER OPENS BY ITS BUTTON, NOT IN THE MIDDLE OF THE WINDOW.

"Telegram, emoji selector is going in the middle of the window kinda". Under 1180px every popover was
turned into a centred bottom sheet, and a Telegram window is nearly always narrower than that — so the
picker opened mid-window, away from the message box it types into. The real client, a real chat, real
clicks, at the widths a Telegram window actually has: the picker must touch the 😊 button's column
(its horizontal span overlaps the button) and sit directly above or below it, fully on screen; and an
emoji picked from it must land in the message box.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE, _open


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


WHERE = r"""(()=>{const b=document.querySelector('.tg-composer [data-act="emoji"]').getBoundingClientRect();
  const p=[...document.querySelectorAll('.emoji-pop')].find(x=>x.getBoundingClientRect().height>0);
  if(!p)return null;const r=p.getBoundingClientRect();
  return {btn:[b.left,b.top,b.right,b.bottom],pop:[r.left,r.top,r.right,r.bottom],vw:innerWidth,vh:innerHeight,
          sheet:p.classList.contains('sheet')};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("size", [(760, 700), (1000, 800), (1366, 850)])
def test_the_emoji_picker_opens_by_its_button(size):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=size[0], height=size[1], deviceScaleFactor=1, mobile=False))
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('[data-act=\"emoji\"]')")
        await b.js("document.querySelector('[data-act=\"emoji\"]').click()")
        for _ in range(100):
            got['where'] = await b.js(WHERE)
            if got['where']:
                break
            await asyncio.sleep(.1)
        # Pick the first emoji the picker offers, with REAL pointer input (the cell takes a pick on
        # mousedown, so a synthetic .click() would prove nothing); it must reach the message box.
        cell = await b.js("""(()=>{const p=[...document.querySelectorAll('.emoji-pop')].find(x=>x.getBoundingClientRect().height>0);
            const e=p&&p.querySelector('.ep-grid [data-e]');if(!e)return null;const r=e.getBoundingClientRect();
            return [r.left+r.width/2,r.top+r.height/2];})()""")
        if cell:
            for kind in ('mousePressed', 'mouseReleased'):
                await b.call('Input.dispatchMouseEvent', dict(type=kind, x=cell[0], y=cell[1], button='left', clickCount=1))
        await asyncio.sleep(.3)
        got['typed'] = await b.js("(document.querySelector('.tg-composer textarea.tg-text')||{}).value||''")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'")))
    w = got['where']
    assert w, "the emoji picker never opened"
    (bl, bt, br, bb), (pl, pt, pr, pb) = w['btn'], w['pop']
    assert not w['sheet'], ("the picker is a centred sheet, not anchored to its button", w)
    assert pl < br and pr > bl, ("the picker is not in the button's column", w)
    near = abs(pb - bt) <= 24 or abs(pt - bb) <= 24   # 6px margin + the popover's display scaling
    assert near, ("the picker is not directly above or below its button", w)
    assert pl >= 0 and pr <= w['vw'] + 1 and pt >= 0 and pb <= w['vh'] + 1, ("the picker is off screen", w)
    assert got['typed'].strip(), "picking an emoji did not put it in the message box"


def test_every_emoji_picker_telegram_opens_is_anchored():
    """THE RULE, so the next button cannot repeat it. The reaction row was fixed to open beside its
    message on 10-06 (aa003c761); three days later a NEW button (the composer's 😊) called the shared
    picker without `anchored` and opened mid-window — and the reaction row's ＋ fallback had always done
    the same. Every openEmojiPopover call in telegram.js must say where it belongs."""
    import re
    src = (Path(__file__).resolve().parents[2] / "static/js/client/telegram.js").read_text()
    calls = [m.start() for m in re.finditer(r"openEmojiPopover\(", src)]
    calls = [i for i in calls if not src[i - 15:i].rstrip().endswith("!PC().")]   # the existence checks
    assert calls, "telegram.js no longer opens the emoji picker?"
    bad = []
    for i in calls:
        depth, j = 0, src.index("(", i)
        while True:
            depth += {"(": 1, ")": -1}.get(src[j], 0)
            if depth == 0:
                break
            j += 1
        if "anchored" not in src[i:j]:
            bad.append(src[i:i + 80].replace("\n", " "))
    assert not bad, "an emoji picker in Telegram opens unanchored (centred sheet): " + "; ".join(bad)
