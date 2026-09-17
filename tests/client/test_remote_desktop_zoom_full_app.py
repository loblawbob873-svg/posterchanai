"""A 4K desktop can be magnified, and a magnified desktop still clicks where you point.

Reported: "why is remote desktop still hard to see the screen! I thought you fixed it so it scaled
like VNC! My desktop 4K screen is too tiny on laptop". What shipped was `object-fit: contain` — a
correct fit, and on a 3840-wide desktop in a ~1400-wide window that is a 36% scale, i.e. unreadable.
The viewer had no zoom at all.

The risk in adding one is not the picture, it is the POINTER: every input this viewer sends is
mapped through `video.getBoundingClientRect()`, so a zoom implemented as a resize (rather than a
transform) leaves the mapping behind and the session clicks the wrong thing while looking perfectly
fine. These run the SHIPPED `_rdZoomTransform` and `_rdVideoPoint` against a real element carrying a
real CSS transform, in a real browser, and check exactly that.
"""
import asyncio

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STAGE = """
(() => {
  const old=document.getElementById('zoomprobe'); if(old)old.remove();
  const host=document.createElement('div'); host.id='zoomprobe';
  host.style.cssText='position:fixed;left:0;top:0;width:800px;height:450px;overflow:hidden;z-index:-1';
  const video=document.createElement('video');
  video.id='zoomprobe-v';
  video.style.cssText='position:absolute;inset:0;width:100%;height:100%;object-fit:contain';
  Object.defineProperty(video,'videoWidth',{value:3840,configurable:true});
  Object.defineProperty(video,'videoHeight',{value:2160,configurable:true});
  host.appendChild(video); document.body.appendChild(host);
  return true;
})()
"""

# The stage is 800x450 and the remote screen is 16:9, so `contain` fills it exactly: no letterbox to
# reason about, and every offset below is the zoom's alone.
MAP = """
(() => {
  const video=document.getElementById('zoomprobe-v'), stage=document.getElementById('zoomprobe');
  // The STAGE's own rect, never a hardcoded coordinate: this client scales the whole page with
  // `body{zoom}` on some viewports, so 400,225 is not the middle of an 800x450 box. Asking the
  // element where it is keeps the test measuring the mapping instead of the page zoom.
  const box=stage.getBoundingClientRect();
  // The transform is written in LAYOUT pixels (clientWidth), the click is in VISUAL pixels (the
  // rect). Conflating the two over-translates by the page zoom, which is the bug this caught.
  const t=__PC.__rdZoomTransform(%(scale)s,{width:stage.clientWidth,height:stage.clientHeight},{x:%(ax)s,y:%(ay)s});
  video.style.transformOrigin='center center';
  video.style.transform='translate('+t.x+'px,'+t.y+'px) scale('+t.scale+')';
  const p=__PC.__rdVideoPoint(video,{clientX:box.left+box.width*%(fx)s, clientY:box.top+box.height*%(fy)s});
  return {t:t, x:Math.round(p.x*1000)/1000, y:Math.round(p.y*1000)/1000,
          box:{w:Math.round(box.width),h:Math.round(box.height)}};
})()
"""


def _map(browser, scale, at, where=(0.5, 0.5)):
    """`where` is a fraction of the stage, so the assertions are about the mapping and nothing else."""
    return browser.js(MAP % dict(scale=scale, ax=at[0], ay=at[1], fx=where[0], fy=where[1]))


def test_zooming_keeps_the_pointer_over_the_same_place_on_the_remote_screen():
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        assert await browser.js(STAGE)

        # FIT: the stage IS the remote screen, so its centre is the middle of that screen.
        fit = await _map(browser, 1, (0.5, 0.5))
        assert fit['t']['scale'] == 1 and fit['t']['x'] == 0 and fit['t']['y'] == 0
        assert abs(fit['x'] - 0.5) < 0.01 and abs(fit['y'] - 0.5) < 0.01

        # ZOOMED to 2x on a point that CAN be centred: the centre of the stage IS that point. This
        # is the whole contract — a mapping that ignored the transform would still answer 0.5, 0.5.
        for at in ((0.25, 0.75), (0.5, 0.5), (0.35, 0.6)):
            got = await _map(browser, 2, at)
            assert abs(got['x'] - at[0]) < 0.02, (at, got)
            assert abs(got['y'] - at[1]) < 0.02, (at, got)

        # A point NEAR THE EDGE cannot be centred at 2x — half the picture would be off the stage,
        # which is the band of black the clamp exists to prevent. The promise there is weaker and
        # still the one that matters: it is VISIBLE. (Measured: asking for 0.8 centres 0.75, and
        # 0.8 sits inside the half-width the zoom shows.)
        for at in ((0.8, 0.2), (1, 0), (0, 1)):
            got = await _map(browser, 2, at)
            half = 0.5 / got['t']['scale']
            for axis, want in (('x', at[0]), ('y', at[1])):
                low, high = got[axis] - half, got[axis] + half
                assert low - 0.001 <= want <= high + 0.001, (at, axis, got, (low, high))

        # And a point offset from the centre moves HALF as far across the remote screen at 2x.
        off = await _map(browser, 2, (0.5, 0.5), (0.75, 0.5))
        assert abs(off['x'] - 0.625) < 0.02, off

    asyncio.run(desktop.with_browser('online', '', check))


def test_the_stage_is_measured_in_the_pixels_the_transform_is_written_in():
    """The viewer's OWN measurement, not the test's arithmetic.

    A CSS `translate()` resolves in LAYOUT pixels; `getBoundingClientRect()` answers in VISUAL ones,
    and this client scales whole pages with `body{zoom}`. Measuring the stage with the rect
    over-translates by exactly the page zoom — measured at 2x centred on 0.25, the middle of the
    stage came out at 0.32 of the remote screen. Under a deliberate page zoom the two numbers
    differ, so this fails the moment the rect comes back."""
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        got = await browser.js("""
        (() => {
          const old=document.getElementById('boxprobe'); if(old)old.remove();
          const el=document.createElement('div'); el.id='boxprobe';
          el.style.cssText='position:fixed;left:0;top:0;width:640px;height:360px;z-index:-1';
          document.body.appendChild(el);
          const before=document.body.style.zoom;
          document.body.style.zoom='2';
          const box=__PC.__rdStageBox(el), rect=el.getBoundingClientRect();
          document.body.style.zoom=before;
          return {box:{w:Math.round(box.width),h:Math.round(box.height)},
                  rect:{w:Math.round(rect.width),h:Math.round(rect.height)},
                  client:{w:el.clientWidth,h:el.clientHeight}};
        })()""")
        assert got['box']['w'] == got['client']['w'] == 640, got
        assert got['box']['h'] == got['client']['h'] == 360, got
        # The page zoom really was in effect, i.e. this test can tell the two spaces apart.
        assert got['rect']['w'] != got['box']['w'], got

    asyncio.run(desktop.with_browser('online', '', check))


def test_a_magnified_picture_can_never_be_panned_off_its_own_stage():
    """Clamping, against the shipped arithmetic: asking to centre a corner must still fill the view,
    or the session shows a band of black beside the desktop and calls it zoom."""
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        for at in ((0, 0), (1, 1), (0, 1), (1, 0), (-5, 9)):
            t = await browser.js(
                "__PC.__rdZoomTransform(2,{width:800,height:450},{x:%s,y:%s})" % at)
            assert abs(t['x']) <= 800 * (2 - 1) / 2 + 1, (at, t)
            assert abs(t['y']) <= 450 * (2 - 1) / 2 + 1, (at, t)
        # Fit never offsets, whatever it is asked to centre.
        flat = await browser.js("__PC.__rdZoomTransform(1,{width:800,height:450},{x:0.1,y:0.9})")
        assert flat['x'] == 0 and flat['y'] == 0
        # The range is bounded at both ends: no 1000% zoom, no inside-out picture.
        small = await browser.js("__PC.__rdZoomTransform(0.2,{width:800,height:450},{x:.5,y:.5})")
        big = await browser.js("__PC.__rdZoomTransform(99,{width:800,height:450},{x:.5,y:.5})")
        assert small['scale'] == 1 and 1 < big['scale'] <= 8

    asyncio.run(desktop.with_browser('online', '', check))


def test_the_control_is_wired_and_belongs_to_a_desktop_session_only():
    app = (desktop.ROOT / "static/js/client/app.js").read_text(encoding="utf-8") \
        if hasattr(desktop, "ROOT") else None
    if app is None:
        import pathlib
        app = (pathlib.Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")
    # Ctrl/Cmd+wheel must be taken before the branch that forwards a scroll to the other machine.
    zoom_at = app.index("if(!(e.ctrlKey||e.metaKey))return;")
    remote_at = app.index("listen(video,'wheel',e=>{if(!active()||e.ctrlKey||e.metaKey)return;")
    assert zoom_at < remote_at
    # The readout doubles as "show me the whole screen again".
    assert "bind('rd-zoom-level',()=>step(0));" in app
    # A camera call never gets a zoom row.
    assert "const wanted=!!(_call&&_call.remoteDesktop&&!_call.caller)" in app
    import pathlib
    css = (pathlib.Path(__file__).resolve().parents[2] / "static/css/client.css").read_text(encoding="utf-8")
    # The stage clips, or a magnified desktop paints over the header and the actions.
    assert ".rd-stage{display:contents}" in css
    assert "overflow:hidden" in css[css.index(".call-overlay.rd:not(.call-mini) .rd-stage{"):][:200]
