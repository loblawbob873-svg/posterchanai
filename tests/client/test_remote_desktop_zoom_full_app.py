"""A 4K desktop is readable, steerable, and still clicks where you point.

Reported twice. First "My desktop 4K screen is too tiny on laptop" — `object-fit:contain` is a
correct fit and, on a 3840-wide desktop in a ~1400-wide window, a 36% scale, i.e. unreadable. Then,
about the control that answered it: "the remote desktop zoom is terrible and inefficient. zooms way
too much, don't fit like vnc". A multiplier over Fit in 25% jumps, with the view auto-panning to
wherever the remote cursor went, is not a zoom control; Fit / 1:1 / free, ~8% notches anchored at the
pointer, and a pan you drive is.

The risk in any of it is not the picture, it is the POINTER: every input this viewer sends is mapped
through `video.getBoundingClientRect()`, so a zoom implemented as a resize (rather than a transform)
leaves the mapping behind and the session clicks the wrong thing while looking perfectly fine. These
run the SHIPPED zoom maths and the SHIPPED `_rdVideoPoint` against a real element carrying a real CSS
transform, in a real browser, and check exactly that — at several scales, several pan offsets, and on
a phone-width window.
"""
import asyncio

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def STAGE(width=800, height=450, remote_w=3840, remote_h=2160):
    return """
(() => {
  const old=document.getElementById('zoomprobe'); if(old)old.remove();
  const host=document.createElement('div'); host.id='zoomprobe';
  host.style.cssText='position:fixed;left:0;top:0;width:%dpx;height:%dpx;overflow:hidden;z-index:-1';
  const video=document.createElement('video');
  video.id='zoomprobe-v';
  video.style.cssText='position:absolute;inset:0;width:100%%;height:100%%;object-fit:contain';
  Object.defineProperty(video,'videoWidth',{value:%d,configurable:true});
  Object.defineProperty(video,'videoHeight',{value:%d,configurable:true});
  host.appendChild(video); document.body.appendChild(host);
  return true;
})()
""" % (width, height, remote_w, remote_h)


# `_rdZoomTransform` takes a MULTIPLIER over Fit and the picture's own (letterboxed) box, because
# that is what a CSS transform is written in. Everything a person touches is in SCALE — a fraction of
# 1:1 — so the test derives the multiplier the same way the viewer does: scale / fit.
MAP = """
(() => {
  const video=document.getElementById('zoomprobe-v'), stage=document.getElementById('zoomprobe');
  const b=__PC.__rdZoomBounds(stage.clientWidth,stage.clientHeight,video.videoWidth,video.videoHeight);
  const scale=%(scale)s, mult=scale/b.fit;
  const t=__PC.__rdZoomTransform(mult,b.content,{x:%(ax)s,y:%(ay)s});
  video.style.transformOrigin='center center';
  video.style.transform='translate('+t.x+'px,'+t.y+'px) scale('+t.scale+')';
  // The STAGE's own rect, never a hardcoded coordinate: this client scales the whole page with
  // `body{zoom}` on some viewports, so 400,225 is not the middle of an 800x450 box.
  const box=stage.getBoundingClientRect();
  const p=__PC.__rdVideoPoint(video,{clientX:box.left+box.width*%(fx)s, clientY:box.top+box.height*%(fy)s});
  return {t:t, fit:b.fit, mult:mult, centre:__PC.__rdZoomCentre(t,b.content),
          x:Math.round(p.x*10000)/10000, y:Math.round(p.y*10000)/10000,
          box:{w:Math.round(box.width),h:Math.round(box.height)}};
})()
"""


def _map(browser, scale, at, where=(0.5, 0.5)):
    """`where` is a fraction of the stage, so the assertions are about the mapping and nothing else."""
    return browser.js(MAP % dict(scale=scale, ax=at[0], ay=at[1], fx=where[0], fy=where[1]))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_zooming_keeps_the_pointer_over_the_same_place_on_the_remote_screen():
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        assert await browser.js(STAGE())

        # FIT: an 800x450 stage showing a 16:9 desktop is a 0.2083 scale, and the stage IS the whole
        # remote screen, so its centre is the middle of that screen and nothing is translated.
        fit = await _map(browser, 800 / 3840, (0.5, 0.5))
        assert abs(fit['fit'] - 800 / 3840) < 1e-6, fit
        assert fit['t']['x'] == 0 and fit['t']['y'] == 0 and abs(fit['t']['scale'] - 1) < 1e-6
        assert abs(fit['x'] - 0.5) < 0.01 and abs(fit['y'] - 0.5) < 0.01

        # 1:1 — the scale that makes 4K text readable, and a 4.8x magnification over Fit here. A
        # point that CAN be centred IS the centre of the stage. This is the whole contract: a
        # mapping that ignored the transform would still answer 0.5, 0.5.
        for scale in (0.5, 1.0, 2.0):
            for at in ((0.25, 0.75), (0.5, 0.5), (0.35, 0.6)):
                got = await _map(browser, scale, at)
                assert abs(got['x'] - at[0]) < 0.005, (scale, at, got)
                assert abs(got['y'] - at[1]) < 0.005, (scale, at, got)

        # A point NEAR THE EDGE cannot be centred — half the picture would be off the stage, which is
        # the band of black the clamp exists to prevent. The promise there is weaker and still the
        # one that matters: it is VISIBLE.
        for scale in (0.5, 1.0):
            for at in ((0.8, 0.2), (1, 0), (0, 1)):
                got = await _map(browser, scale, at)
                half = 0.5 / got['mult']
                for axis, want in (('x', at[0]), ('y', at[1])):
                    low, high = got[axis] - half, got[axis] + half
                    assert low - 0.002 <= want <= high + 0.002, (scale, at, axis, got)

        # And a point offset from the centre moves proportionally less across the remote screen the
        # more it is magnified: at 1:1 a quarter of the stage is 1400/3840 * 0.25 of the screen.
        for scale in (0.5, 1.0, 2.0):
            off = await _map(browser, scale, (0.5, 0.5), (0.75, 0.5))
            want = 0.5 + 0.25 * (800 / (3840 * scale))
            assert abs(off['x'] - want) < 0.005, (scale, off, want)

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_click_is_exact_at_every_zoom_and_pan_including_a_phone_window():
    """The one property that cannot be read off the source, measured as a GRID rather than a point.

    A mapping can be right in the middle and wrong at the corners (a transform origin that is not the
    centre), or right at one scale and wrong at another (a multiplier applied twice). Every cell of a
    5x5 grid of the stage is asked for, at four scales, on a desktop window, a phone-width window and
    a window whose aspect does not match the remote screen at all."""
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        # The third one is deliberately 16:10 over a 16:9 desktop: the picture is then LETTERBOXED,
        # and a zoom that reasons about the stage rather than the picture's own box over-translates
        # by exactly that letterbox. It is the shape almost every real window has.
        for width, height, rw, rh in ((800, 450, 3840, 2160), (390, 220, 3840, 2160),
                                      (900, 562, 3840, 2160), (600, 500, 1280, 1024)):
            assert await browser.js(STAGE(width, height, rw, rh))
            fit = (await _map(browser, 1, (0.5, 0.5)))['fit']
            for scale in (fit, fit * 1.5, 1.0, 2.0):
                for at in ((0.5, 0.5), (0.3, 0.7), (0.85, 0.15)):
                    for gx in (0.02, 0.25, 0.5, 0.75, 0.98):
                        for gy in (0.02, 0.5, 0.98):
                            got = await _map(browser, scale, at, (gx, gy))
                            # Where the picture says that stage position is, computed from the
                            # transform the viewer wrote — NOT from the request, so a clamped pan is
                            # held to what it actually did.
                            mult, centre = got['mult'], got['centre']
                            # The picture's box is `fit*mult` of the remote screen per stage pixel;
                            # the letterbox is the difference between the stage and that box.
                            cw = rw * fit * mult / width      # fraction of the STAGE the picture spans
                            ch = rh * fit * mult / height
                            want_x = centre['x'] + (gx - 0.5) / max(cw, 1e-9)
                            want_y = centre['y'] + (gy - 0.5) / max(ch, 1e-9)
                            want_x = min(1, max(0, want_x))
                            want_y = min(1, max(0, want_y))
                            assert abs(got['x'] - want_x) < 0.006, (width, height, scale, at, gx, gy, got, want_x)
                            assert abs(got['y'] - want_y) < 0.006, (width, height, scale, at, gx, gy, got, want_y)

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_stage_is_measured_in_the_pixels_the_transform_is_written_in():
    """The viewer's OWN measurement, not the test's arithmetic.

    A CSS `translate()` resolves in LAYOUT pixels; `getBoundingClientRect()` answers in VISUAL ones,
    and this client scales whole pages with `body{zoom}`. Measuring the stage with the rect
    over-translates by exactly the page zoom — measured at 2x centred on 0.25, the middle of the
    stage came out at 0.32 of the remote screen. Under a deliberate page zoom the two numbers
    differ, so this fails the moment the rect comes back. The same page zoom is why a PAN has to
    divide by the ratio: a drag delta arrives in visual pixels and is applied in layout ones."""
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


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
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
        # BELOW Fit there is nothing to pan either — which is a real state now, because 1:1 on a
        # remote screen SMALLER than the window is an enlargement undone. An offset there would
        # slide a picture that is already surrounded by black.
        under = await browser.js("__PC.__rdZoomTransform(0.6,{width:800,height:450},{x:0.1,y:0.9})")
        assert under['x'] == 0 and under['y'] == 0 and abs(under['scale'] - 0.6) < 1e-6, under
        # The STATE is clamped too, not only the paint: a drag that keeps pushing past the edge and
        # is clamped only at paint time builds an invisible offset, and the drag back does nothing
        # for as long as it took to accumulate.
        held = await browser.js("__PC.__rdClampAt({x:-4,y:9},4)")
        assert abs(held['x'] - 0.125) < 1e-6 and abs(held['y'] - 0.875) < 1e-6, held
        assert await browser.js("JSON.stringify(__PC.__rdClampAt({x:0.9,y:0.1},1))") == '{"x":0.5,"y":0.5}'

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_range_is_a_fraction_of_one_to_one_and_reaches_both_ends():
    """SCALE IS A FRACTION OF 1:1, and the bounds are what make "1:1" and "Fit" both reachable.

    The old control's number was a multiplier over Fit, so "2x" meant 72% of actual size on one
    window and 200% on another, and the ceiling of 4x could not reach 1:1 on a big enough screen.
    The floor is Fit — below it the session shows black — EXCEPT when the remote screen is smaller
    than the window, where Fit is already an enlargement and 1:1 is below it."""
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        # A 4K desktop in a laptop window: Fit is 36%, 1:1 is inside the range, 200% is the ceiling.
        big = await browser.js("__PC.__rdZoomBounds(1400,790,3840,2160)")
        assert abs(big['fit'] - 1400 / 3840) < 1e-6, big
        assert abs(big['min'] - big['fit']) < 1e-9 and big['max'] == 2, big
        assert await browser.js("__PC.__rdClampScale(1,__PC.__rdZoomBounds(1400,790,3840,2160))") == 1
        assert await browser.js("__PC.__rdClampScale(99,__PC.__rdZoomBounds(1400,790,3840,2160))") == 2
        got = await browser.js("__PC.__rdClampScale(0.01,__PC.__rdZoomBounds(1400,790,3840,2160))")
        assert abs(got - big['fit']) < 1e-9, got
        # A SMALL remote screen on a big window: Fit enlarges it, and Actual size must still be
        # reachable BELOW Fit or the 1:1 button is a lie on exactly the screens it is easiest on.
        small = await browser.js("__PC.__rdZoomBounds(1920,1080,1280,1024)")
        assert small['fit'] > 1 and small['min'] == 1, small
        assert await browser.js("__PC.__rdClampScale(1,__PC.__rdZoomBounds(1920,1080,1280,1024))") == 1
        # Nonsense resolves to the floor rather than to NaN, which would write `scale(NaN)` and
        # blank the picture with nothing in any log.
        assert await browser.js("__PC.__rdClampScale('x',__PC.__rdZoomBounds(1400,790,3840,2160))") == big['min']

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_ctrl_wheel_zooms_at_the_pointer_and_keeps_that_point_still():
    """ANCHORED. Zooming about the centre moves whatever you were pointing at off the screen, which
    is half of "zooms way too much" — the magnification lands somewhere else."""
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        # Zooming IN at a pointer moves the centre toward it by exactly the shrink in the offset.
        got = await browser.js("__PC.__rdZoomAnchor(1,2,{x:0.8,y:0.2},{x:0.5,y:0.5})")
        assert abs(got['x'] - 0.65) < 1e-9 and abs(got['y'] - 0.35) < 1e-9, got
        # Zooming OUT moves it away by the same rule, so a wheel up and a wheel down return.
        back = await browser.js("__PC.__rdZoomAnchor(2,1,{x:0.8,y:0.2},%s)"
                                % ('{x:%.17g,y:%.17g}' % (got['x'], got['y'])))
        assert abs(back['x'] - 0.5) < 1e-9 and abs(back['y'] - 0.5) < 1e-9, back
        # Pointing at the centre is a no-op, which is what makes the +/- buttons and Ctrl+ behave.
        same = await browser.js("__PC.__rdZoomAnchor(1,3,{x:0.5,y:0.5},{x:0.5,y:0.5})")
        assert abs(same['x'] - 0.5) < 1e-9 and abs(same['y'] - 0.5) < 1e-9, same
        # End to end, against the real transform: the stage position of the anchored point does not
        # move when the scale changes.
        assert await browser.js(STAGE())
        held = await browser.js("""
        (() => {
          const video=document.getElementById('zoomprobe-v'), stage=document.getElementById('zoomprobe');
          const b=__PC.__rdZoomBounds(stage.clientWidth,stage.clientHeight,3840,2160);
          const pointer={x:0.62,y:0.41};
          const where=(scale,at)=>{
            const mult=scale/b.fit, t=__PC.__rdZoomTransform(mult,b.content,at);
            const c=__PC.__rdZoomCentre(t,b.content);
            // Where `pointer` lands on the stage, as a fraction of it.
            return {gx:0.5+(pointer.x-c.x)*(3840*b.fit*mult/stage.clientWidth),
                    gy:0.5+(pointer.y-c.y)*(2160*b.fit*mult/stage.clientHeight), t:t, c:c, mult:mult};
          };
          let at={x:0.5,y:0.5}, out=[];
          for(const [from,to] of [[b.fit,b.fit*1.08],[b.fit*1.08,b.fit*1.08*1.08],[0.5,1],[1,2],[2,1.5]]){
            const before=where(from,__PC.__rdClampAt(at,from/b.fit));
            at=__PC.__rdClampAt(__PC.__rdZoomAnchor(from/b.fit,to/b.fit,pointer,before.c),to/b.fit);
            const after=where(to,at);
            out.push({from:from,to:to,before:[before.gx,before.gy],after:[after.gx,after.gy]});
          }
          return out;
        })()""")
        for row in held:
            for i in (0, 1):
                # Once the pan clamps against an edge the anchor cannot be honoured exactly — the
                # picture would have to leave the stage — so the promise is that it barely moves,
                # not that it is pinned. Unanchored, the same point walks a third of the window.
                assert abs(row['after'][i] - row['before'][i]) < 0.06, row

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_host_is_only_asked_for_the_resolution_this_window_can_show():
    """The "inefficient" half, and it is arithmetic before it is a protocol.

    Measured on a real WebRTC loopback with a text-heavy 4K source: full resolution is 2992 kbps,
    22.6ms of encode per frame on the sharing machine and 3.0ms of decode on this one; the same view
    at what a 1400-wide window can actually show is 1304 kbps, 7.0ms and 0.83ms. The rule that gets
    that has to hold in both directions — it must never ask for LESS than the window shows (the
    picture would be soft, which is the bug it exists to fix), and it must go back to full resolution
    the moment somebody zooms in, which is precisely when they are reading something."""
    async def check(browser):
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        d = lambda shown, remote, dpr=1: browser.js(
            "__PC.__rdSourceDownscale(%s,%s,%s)" % (shown, remote, dpr))
        # A 4K desktop fitted into a laptop window: ask for about a third.
        assert await d(1400, 3840) == 2.5
        # The SAME window on a HiDPI screen really is showing 2800 device pixels, so it may not.
        assert await d(1400, 3840, 2) == 1.25
        # Zoomed to 1:1 the window is showing 3840 remote pixels across, so nothing is given up.
        assert await d(3840, 3840) == 1
        assert await d(9000, 3840) == 1
        # A minimised session is a thumbnail, and a thumbnail does not need a 4K encoder running.
        assert await d(132, 3840) == 16
        # NEVER soft: every answer must leave at least as many source pixels as the window shows,
        # or as many as the screen HAS when the window wants more than that (a HiDPI laptop at 1:1
        # is asking for more detail than a 4K desktop contains, and the right answer is all of it).
        for shown in (120, 300, 640, 800, 1024, 1280, 1400, 1600, 1920, 2560, 3000, 3840):
            for dpr in (1, 2, 3):
                step = await d(shown, 3840, dpr)
                assert 3840 / step >= min(3840, shown * dpr) - 1e-6, (shown, dpr, step)
        # And it is a STEP, not a continuum: a slider dragged across the range must not re-tune the
        # encoder on every frame.
        steps = sorted({await d(shown, 3840) for shown in range(200, 3900, 37)})
        assert len(steps) <= 11, steps

    asyncio.run(desktop.with_browser('online', '', check))


def test_the_controls_and_the_keys_are_wired_and_belong_to_a_desktop_session_only():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    app = (root / "static/js/client/app.js").read_text(encoding="utf-8")
    # Ctrl/Cmd+wheel must be taken before the branch that forwards a scroll to the other machine.
    zoom_at = app.index("if(e.ctrlKey||e.metaKey){")
    remote_at = app.index("listen(video,'wheel',e=>{if(!active()||e.ctrlKey||e.metaKey)return;")
    assert zoom_at < remote_at
    # THREE modes, and the readout doubles as "show me the whole screen again".
    for wiring in ("bind('rd-zoom-fit',()=>_rdZoomSetMode('fit'));",
                   "bind('rd-zoom-actual',()=>_rdZoomSetMode('actual'));",
                   "bind('rd-zoom-level',()=>_rdZoomSetMode('fit'));",
                   "bind('rd-zoom-in',()=>_rdZoomBy(RD_ZOOM_KEY));",
                   "bind('rd-zoom-out',()=>_rdZoomBy(1/RD_ZOOM_KEY));"):
        assert wiring in app, wiring
    assert "range.oninput=" in app
    # FOLLOWING IS OPT-IN. The default must be off in the preference seed AND in the state builder,
    # or the first session of a fresh install pans itself while somebody is reading.
    assert "let _rdZoomPref={mode:'fit',scale:1,follow:false};" in app
    assert "follow:!!_rdZoomPref.follow" in app
    assert "if(!state||!state.follow||!p)return;" in app
    # The viewer's own keys are registered BEFORE the remote key forwarder — both are capture-phase
    # on `document`, so registration order is the priority and Ctrl+0 cannot reach the other machine.
    keys_at = app.index("const viewerKeysApply=()")
    forward_at = app.index("for(const name of ['keydown','keyup'])listen(document,name,e=>{")
    assert keys_at < forward_at
    for binding in ("code==='Digit0'", "code==='Digit1'", "code==='NumpadAdd'", "code==='NumpadSubtract'"):
        assert binding in app, binding
    # A camera call never gets a zoom row.
    assert "const wanted=!!(_call&&_call.remoteDesktop&&!_call.caller)" in app
    # The quality request only ever reduces work, and only the host acts on it.
    assert "if(m.t==='quality'&&_call.caller){_rdApplyQuality(m.d);return;}" in app
    assert "const d=Math.max(1,Math.min(16,Number(down)||1));" in app
    css = (root / "static/css/client.css").read_text(encoding="utf-8")
    # The stage clips, or a magnified desktop paints over the header and the actions.
    assert ".rd-stage{display:contents}" in css
    assert "overflow:hidden" in css[css.index(".call-overlay.rd:not(.call-mini) .rd-stage{"):][:200]
    # The position indicators are indicators: a click at the edge of a magnified desktop belongs to
    # the other machine, not to a scrollbar drawn over it.
    assert "pointer-events:none" in css[css.index(".rd-scroll{"):][:200]


# A real session: a canvas at a real resolution, captured into a real MediaStream, handed to the real
# `_callUI` so the overlay, the stage, the stylesheet and `_rdBindViewer` are the shipped ones.
SESSION = """
(async () => {
  const c=document.createElement('canvas'); c.width=%(rw)d; c.height=%(rh)d;
  const g=c.getContext('2d');
  // Something with structure, so a click can be checked against a colour rather than a coordinate.
  g.fillStyle='#222';g.fillRect(0,0,c.width,c.height);
  for(let i=0;i<8;i++){g.fillStyle=['#f00','#0f0','#00f','#ff0'][i%%4];g.fillRect(i*c.width/8,0,c.width/8,c.height);}
  (function draw(){g.fillRect(0,0,0,0);requestAnimationFrame(draw)})();
  const stream=c.captureStream(12);
  __PC.__rdFakeViewerSession(Object.assign({stream:stream,geometry:{width:%(rw)d,height:%(rh)d}},%(opts)s));
  const video=document.getElementById('call-remote');
  for(let i=0;i<120 && !video.videoWidth;i++)await new Promise(r=>setTimeout(r,25));
  __PC.__rdApplyZoom();
  return {w:video.videoWidth,h:video.videoHeight};
})()
"""

READ = """
(() => {
  const video=document.getElementById('call-remote'), stage=video.parentElement;
  const st=__PC.__rdZoomState(), m=__PC.__rdZoomMetrics();
  const scale=st.mode==='fit'?m.fit:(st.mode==='actual'?1:st.scale);
  const thumb=(id)=>{const b=document.getElementById(id);if(!b)return null;
    return {hidden:!!b.hidden, a:b.firstElementChild&&b.firstElementChild.style[id==='rd-scroll-h'?'left':'top'],
            len:b.firstElementChild&&b.firstElementChild.style[id==='rd-scroll-h'?'width':'height']};};
  return {mode:st.mode, follow:!!st.follow, at:st.at, scale:scale, fit:m.fit,
          label:(document.getElementById('rd-zoom-level')||{}).textContent,
          fitOn:document.getElementById('rd-zoom-fit').classList.contains('on'),
          oneOn:document.getElementById('rd-zoom-actual').classList.contains('on'),
          followOn:document.getElementById('rd-zoom-follow').classList.contains('on'),
          range:document.getElementById('rd-zoom-range').value,
          transform:video.style.transform,
          pannable:video.classList.contains('rd-pannable'),
          h:thumb('rd-scroll-h'), v:thumb('rd-scroll-v'),
          stage:{w:stage.clientWidth,h:stage.clientHeight},
          // Every click this session would send, for a 3x3 grid of the VISIBLE picture, mapped by
          // the shipped `_rdVideoPoint` through whatever transform is on screen right now.
          grid:(()=>{const box=stage.getBoundingClientRect(),out=[];
            for(const fy of [0.05,0.5,0.95])for(const fx of [0.05,0.5,0.95]){
              const p=__PC.__rdVideoPoint(video,{clientX:box.left+box.width*fx,clientY:box.top+box.height*fy});
              out.push([Math.round(p.x*10000)/10000,Math.round(p.y*10000)/10000]);}
            return out;})()};
})()
"""


LAYOUT = """
(() => {
  const ov=document.getElementById('call-overlay'), row=document.getElementById('rd-zoom');
  const r=(el)=>{const b=el.getBoundingClientRect();return {l:b.left,r:b.right,t:b.top,b:b.bottom,w:b.width,h:b.height}};
  const o=r(ov);
  return {ov:o, row:r(row), kids:[...row.children].map(el=>[el.id||el.tagName, r(el)]),
          outside:[...ov.querySelectorAll('*')].filter(e=>e.getClientRects().length &&
            (e.getBoundingClientRect().right>o.r+0.5 || e.getBoundingClientRect().left<o.l-0.5))
            .map(e=>e.id||String(e.className)).slice(0,6)};
})()
"""


def _expected(state, fx, fy):
    """Where a point `fx,fy` of the STAGE is on the remote screen, derived from the transform the
    viewer actually wrote — never from the pan it was asked for, so a clamped pan is held to what it
    did. This is a check on the MAPPING and not a restatement of the pan arithmetic."""
    mult = state['scale'] / state['fit']
    stage = state['stage']
    # The picture's own (letterboxed) box, as a fraction of the stage.
    cw = state['fit'] * mult * state['remote'][0] / stage['w']
    ch = state['fit'] * mult * state['remote'][1] / stage['h']
    x = state['centre'][0] + (fx - 0.5) / cw
    y = state['centre'][1] + (fy - 0.5) / ch
    return (min(1, max(0, x)), min(1, max(0, y)))


def _expected_grid(state):
    return [_expected(state, fx, fy) for fy in (0.05, 0.5, 0.95) for fx in (0.05, 0.5, 0.95)]


async def _session(browser, rw=3840, rh=2160, opts='{}'):
    """`opts` is passed to the shipped fake-session hook verbatim: `{control:true}` for a session
    that is driving the other machine, `{channel:"connecting"}` for one whose control channel has
    not opened yet."""
    size = await browser.js(SESSION % dict(rw=rw, rh=rh, opts=opts))
    assert size['w'] == rw and size['h'] == rh, size
    return size


async def _state(browser, rw=3840, rh=2160):
    """Read the view, and recover the centre from the transform the viewer WROTE.

    Parsing `style.transform` rather than asking for the pan it was given is what makes every
    assertion below about the picture on screen instead of about the request."""
    import re
    st = await browser.js(READ)
    st['remote'] = [rw, rh]
    nums = [float(n) for n in re.findall(r'-?[\d.]+', st['transform'])] if st['transform'] else []
    tx, ty = (nums[0], nums[1]) if len(nums) >= 3 else (0.0, 0.0)
    # The picture's box in CSS pixels is the remote screen at the current scale, by definition.
    st['centre'] = (0.5 - tx / (st['scale'] * rw), 0.5 - ty / (st['scale'] * rh))
    return st


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('window', [(1440, 900), (390, 780)])
def test_the_shipped_controls_switch_modes_pan_and_still_click_where_you_point(window):
    """THE WHOLE VIEWER, driven the way a person drives it, on a laptop window and a phone one.

    Everything below is the shipped thing: the overlay `_callUI` builds, the stylesheet that lays the
    stage out, the buttons bound in `_callUI`, the keys and the pan gesture bound in `_rdBindViewer`,
    and `_rdVideoPoint` reading a real transformed element. After every change the 3x3 grid of clicks
    is checked against where the picture actually is — which is the property that cannot be read off
    the source and the one that broke last time."""
    width, height = window

    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',
                           dict(width=width, height=height, deviceScaleFactor=1, mobile=width < 600))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await _session(browser)

        # FIT is the default, the readout is a PERCENTAGE OF 1:1 (36% on a laptop, not "1x"), there
        # is nothing to pan and nothing to indicate.
        st = await _state(browser)
        assert st['mode'] == 'fit' and st['fitOn'] and not st['oneOn'], st
        assert st['transform'] == '', st
        assert st['label'] == '%d%%' % round(st['fit'] * 100), st
        assert st['fit'] < 0.75, st            # the complaint: a 4K desktop is small in any window
        assert not st['pannable'] and st['h']['hidden'] and st['v']['hidden'], st
        assert st['grid'][4] == [0.5, 0.5], st
        for got, want in zip(st['grid'], _expected_grid(st)):
            assert abs(got[0] - want[0]) < 0.006 and abs(got[1] - want[1]) < 0.006, (got, want, st)

        # FOLLOWING IS OFF until somebody turns it on. It is the behaviour that made a magnified
        # session feel out of control, so its default is the fix.
        assert not st['follow'] and not st['followOn'], st

        # EVERY CONTROL IS INSIDE THE SESSION AND BIG ENOUGH TO PRESS, at both widths. Measured at
        # 390 before the head was allowed to wrap: the toolbar was squeezed into a 197px column
        # beside the peer's name and folded into two cramped rows, with the last control (the follow
        # toggle) the first thing to disappear if anything overflows. It gets its own line now.
        box = await browser.js(LAYOUT)
        assert not box['outside'], box
        assert box['row']['l'] >= box['ov']['l'] - 0.5 and box['row']['r'] <= box['ov']['r'] + 0.5, box
        for name, rect in box['kids']:
            assert rect['w'] >= 24 and rect['h'] >= 20, (name, rect, box)
            assert rect['l'] >= box['ov']['l'] - 0.5 and rect['r'] <= box['ov']['r'] + 0.5, (name, rect, box)

        # 1:1 — the button that makes 4K text readable. Remote pixels are screen pixels, the picture
        # is now pannable, both indicators appear, and the clicks still land.
        await browser.js("document.getElementById('rd-zoom-actual').click()")
        st = await _state(browser)
        assert st['mode'] == 'actual' and st['oneOn'] and not st['fitOn'], st
        assert st['label'] == '100%' and abs(st['scale'] - 1) < 1e-6, st
        assert st['pannable'] and not st['h']['hidden'] and not st['v']['hidden'], st
        # The thumb is as long a fraction of the bar as the window is of the screen.
        assert abs(float(st['h']['len'].rstrip('%')) / 100 - st['fit']) < 0.02, st
        for got, want in zip(st['grid'], _expected_grid(st)):
            assert abs(got[0] - want[0]) < 0.006 and abs(got[1] - want[1]) < 0.006, (got, want, st)

        # PANNING. A plain drag pans while this viewer is not sending input — the reading case — and
        # the clicks move with the picture rather than staying where they were.
        before = st
        stage = await browser.js("(()=>{const b=document.getElementById('call-remote')"
                                 ".parentElement.getBoundingClientRect();return {x:b.left+b.width/2,y:b.top+b.height/2}})()")
        for kind, dx, dy in (('pointerDown', 0, 0), ('pointerMove', -160, -90), ('pointerUp', -160, -90)):
            await browser.call('Input.dispatchMouseEvent', dict(
                type={'pointerDown': 'mousePressed', 'pointerMove': 'mouseMoved', 'pointerUp': 'mouseReleased'}[kind],
                x=stage['x'] + dx, y=stage['y'] + dy, button='left', buttons=1 if kind != 'pointerUp' else 0,
                clickCount=1, pointerType='mouse'))
        await asyncio.sleep(.1)
        st = await _state(browser)
        # Dragging LEFT and UP shows content further right and down.
        assert st['at']['x'] > before['at']['x'] + 1e-4, (before['at'], st['at'])
        assert st['at']['y'] > before['at']['y'] + 1e-4, (before['at'], st['at'])
        assert float(st['h']['a'].rstrip('%')) > float(before['h']['a'].rstrip('%')), st
        for got, want in zip(st['grid'], _expected_grid(st)):
            assert abs(got[0] - want[0]) < 0.006 and abs(got[1] - want[1]) < 0.006, (got, want, st)

        # FINE STEPS. The − button moves ~10%, not 25% of Fit, and it says so.
        await browser.js("document.getElementById('rd-zoom-out').click()")
        stepped = await _state(browser)
        assert stepped['mode'] == 'free', stepped
        assert abs(stepped['scale'] / st['scale'] - 1 / 1.1) < 0.01, (st['scale'], stepped['scale'])
        assert stepped['label'] == '%d%%' % round(stepped['scale'] * 100), stepped
        for got, want in zip(stepped['grid'], _expected_grid(stepped)):
            assert abs(got[0] - want[0]) < 0.006 and abs(got[1] - want[1]) < 0.006, (got, want, stepped)

        # THE READOUT IS THE WAY BACK, one press, no aiming.
        await browser.js("document.getElementById('rd-zoom-level').click()")
        st = await _state(browser)
        assert st['mode'] == 'fit' and st['transform'] == '' and st['h']['hidden'], st

        # THE SLIDER IS CONTINUOUS and logarithmic: half way up the track is the geometric middle of
        # Fit..200%, which on a 4K desktop in a laptop window is around 85% — i.e. the half of the
        # range people read text in is half the track, not its first third.
        await browser.js("""(()=>{const r=document.getElementById('rd-zoom-range');
          r.value='500';r.dispatchEvent(new Event('input',{bubbles:true}));})()""")
        st = await _state(browser)
        assert st['mode'] == 'free', st
        assert abs(st['scale'] - (st['fit'] * 2) ** 0.5) < 0.02, st
        for got, want in zip(st['grid'], _expected_grid(st)):
            assert abs(got[0] - want[0]) < 0.006 and abs(got[1] - want[1]) < 0.006, (got, want, st)

        # THE KEYS. Ctrl+1, Ctrl+0, Ctrl+plus, Ctrl+minus.
        async def key(code, key_text):
            for kind in ('keyDown', 'keyUp'):
                await browser.call('Input.dispatchKeyEvent', dict(type=kind, code=code, key=key_text,
                                                                 modifiers=2, windowsVirtualKeyCode=0))
            await asyncio.sleep(.05)
        await key('Digit1', '1')
        assert (await _state(browser))['mode'] == 'actual'
        await key('Minus', '-')
        st = await _state(browser)
        assert st['mode'] == 'free' and abs(st['scale'] - 1 / 1.1) < 0.01, st
        await key('Equal', '=')
        st = await _state(browser)
        assert abs(st['scale'] - 1) < 0.01, st
        await key('Digit0', '0')
        assert (await _state(browser))['mode'] == 'fit'

        # THE MODE IS REMEMBERED, per session and for the next one: somebody who works at 1:1 is not
        # re-choosing it every time they connect, which is the "terrible" half of the report.
        await browser.js("document.getElementById('rd-zoom-actual').click()")
        assert await browser.js("JSON.parse(localStorage.getItem('pc_rd_zoom')).mode") == 'actual'

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_controlling_viewer_pans_with_the_middle_button_and_never_loses_a_click():
    """The gesture conflict, which is the reason a naive drag-to-pan cannot ship.

    While this viewer has control, a press is a press on the other machine — so panning takes only
    the gestures that cannot be one. Middle-drag pans WHILE THERE IS SOMETHING TO PAN and goes
    through as a middle click when there is not, and a left press is always a click."""
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',
                           dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await _session(browser, opts='{control:true}')
        await browser.js("__PC.__rdOutbox().length;window.__n=__PC.__rdOutbox().length")

        async def drag(button, dx, dy):
            stage = await browser.js("(()=>{const b=document.getElementById('call-remote')"
                                     ".parentElement.getBoundingClientRect();return {x:b.left+b.width/2,y:b.top+b.height/2}})()")
            mask = {'left': 1, 'middle': 4}[button]
            await browser.call('Input.dispatchMouseEvent', dict(type='mousePressed', x=stage['x'], y=stage['y'],
                                                                button=button, buttons=mask, clickCount=1, pointerType='mouse'))
            await browser.call('Input.dispatchMouseEvent', dict(type='mouseMoved', x=stage['x'] + dx, y=stage['y'] + dy,
                                                                button=button, buttons=mask, pointerType='mouse'))
            await browser.call('Input.dispatchMouseEvent', dict(type='mouseReleased', x=stage['x'] + dx, y=stage['y'] + dy,
                                                                button=button, buttons=0, clickCount=1, pointerType='mouse'))
            await asyncio.sleep(.1)

        # AT FIT there is nothing to pan, so a middle-drag is a middle click on the other machine.
        await drag('middle', -80, -40)
        sent = await browser.js("__PC.__rdOutbox().filter(m=>m.t==='input').map(m=>m.e.type+':'+(m.e.button??''))")
        assert 'button:1' in sent, sent
        at_fit = await _state(browser)
        assert at_fit['at']['x'] == 0.5 and at_fit['at']['y'] == 0.5, at_fit

        # MAGNIFIED, the same gesture pans and sends NOTHING — a pan that also clicks would open a
        # menu or paste a selection on the other machine every time somebody looked around.
        await browser.js("__PC.__rdZoomSetMode('actual');window.__before=__PC.__rdOutbox().length")
        await drag('middle', -120, -70)
        st = await _state(browser)
        assert st['at']['x'] > 0.5 and st['at']['y'] > 0.5, st
        assert await browser.js("__PC.__rdOutbox().length===__before"), await browser.js("__PC.__rdOutbox().slice(-4)")
        for got, want in zip(st['grid'], _expected_grid(st)):
            assert abs(got[0] - want[0]) < 0.006 and abs(got[1] - want[1]) < 0.006, (got, want, st)

        # A LEFT press still clicks, at exactly the point the picture shows — the property the whole
        # feature is measured against, taken while zoomed AND panned, from a REAL dispatched press
        # rather than from a synthetic call into the mapping.
        for fx, fy in ((0.1, 0.2), (0.5, 0.5), (0.9, 0.85)):
            # POINTER LOCK MAKES THE MAPPING RELATIVE ON PURPOSE (a locked cursor has no page
            # coordinate; motion is accumulated from `movementX`), and a press grabs the lock. So the
            # absolute path is measured unlocked, which is also the state a viewer is in when it
            # clicks into a session for the first time. The locked path has its own coverage in
            # tests/test_remote_desktop_alignment_runtime.py.
            await browser.js("if(document.pointerLockElement)document.exitPointerLock();"
                             "window.__before=__PC.__rdOutbox().length")
            await asyncio.sleep(.05)
            spot = await browser.js("""(()=>{const b=document.getElementById('call-remote').parentElement
              .getBoundingClientRect();return {x:b.left+b.width*%s,y:b.top+b.height*%s}})()""" % (fx, fy))
            for kind, mask in (('mousePressed', 1), ('mouseReleased', 0)):
                await browser.call('Input.dispatchMouseEvent', dict(type=kind, x=spot['x'], y=spot['y'],
                                                                    button='left', buttons=mask,
                                                                    clickCount=1, pointerType='mouse'))
            await asyncio.sleep(.1)
            press = await browser.js("__PC.__rdOutbox().slice(__before)"
                                     ".find(m=>m.t==='input'&&m.e.type==='button'&&m.e.down)")
            assert press, (fx, fy, await browser.js("__PC.__rdOutbox().slice(__before)"))
            st2 = await _state(browser)
            wx, wy = _expected(st2, fx, fy)
            assert abs(press['e']['x'] - wx) < 0.006, (fx, fy, press, wx, st2)
            assert abs(press['e']['y'] - wy) < 0.006, (fx, fy, press, wy, st2)

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_viewer_asks_the_host_for_the_resolution_it_can_show_and_stops_asking():
    """End to end over the control channel the session already has.

    At Fit a 4K desktop in a laptop window is encoded, transmitted and decoded at 3840 so the
    compositor can throw most of every frame away; zoomed to 1:1 every one of those pixels is wanted.
    The request has to follow the view in BOTH directions and must not be re-sent on every frame."""
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',
                           dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await _session(browser)
        await asyncio.sleep(.7)
        asks = await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality').map(m=>m.d)")
        assert asks and asks[-1] > 1, asks           # Fit asks for less than the whole screen

        await browser.js("__PC.__rdZoomSetMode('actual')")
        await asyncio.sleep(.7)
        asks = await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality').map(m=>m.d)")
        assert asks[-1] == 1, asks                   # 1:1 wants every pixel

        # Nothing further is sent while the view does not change — a `setParameters` on the sharing
        # machine is not free, and a slider dragged across the range would otherwise re-tune it per
        # frame. Repainting the same view repeatedly must be silent.
        count = len(asks)
        for _ in range(10):
            await browser.js("__PC.__rdApplyZoom()")
        await asyncio.sleep(.7)
        assert len(await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality')")) == count

        # And back down again when the view goes back.
        await browser.js("__PC.__rdZoomSetMode('fit')")
        await asyncio.sleep(.7)
        asks = await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality').map(m=>m.d)")
        assert asks[-1] > 1, asks

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_minimising_a_magnified_session_does_not_magnify_the_thumbnail():
    """A remembered 1:1 must not follow a session into the corner.

    `.rd-stage` is `display:contents` while minimised, so it has NO BOX — `clientWidth` is 0 and
    `getBoundingClientRect()` is all zeros. Through the 1x1 fallback that produced, Fit came out as
    1/3840 and the derived multiplier pinned at the sanity ceiling: a 132px thumbnail scaled 32x, from
    a preference set in a previous session. The thumbnail is always Fit, and because the VIDEO is
    measurable there, minimising is also when the host should stop encoding 4K."""
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',
                           dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await _session(browser)
        await browser.js("__PC.__rdZoomSetMode('actual')")
        assert (await _state(browser))['transform'], 'the full-size session should be magnified'
        # The shipped minimise button, not a class poked in by the test.
        await browser.js("document.getElementById('call-min').click()")
        await browser.js("__PC.__rdApplyZoom()")
        await asyncio.sleep(.1)
        assert await browser.js("document.getElementById('call-overlay').classList.contains('call-mini')")
        assert await browser.js("document.getElementById('call-remote').style.transform") == ''
        assert not await browser.js("document.getElementById('call-remote').classList.contains('rd-pannable')")
        # The remembered mode survives the trip — minimising is not a way to lose your zoom.
        assert await browser.js("__PC.__rdZoomState().mode") == 'actual'
        # And the thumbnail asks for a thumbnail.
        await asyncio.sleep(.7)
        asks = await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality').map(m=>m.d)")
        assert asks and asks[-1] >= 4, asks
        # Restoring brings the magnification back.
        await browser.js("document.getElementById('call-overlay').click();__PC.__rdApplyZoom()")
        await asyncio.sleep(.1)
        assert await browser.js("document.getElementById('call-remote').style.transform") != ''

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_resolution_request_made_before_the_channel_opened_is_not_recorded_as_sent():
    """The latch-before-the-attempt shape, in the one message that is sent ONCE per change.

    `_rdApplyZoom` runs from `_callUI`, which fires on the `ontrack` event — routinely BEFORE the
    control channel finishes opening. `_rdSend` drops a message on a channel that is not open yet and
    says nothing, so recording it as sent means it is never re-sent and the session runs at full 4K
    for its whole life with nothing in any log. Every input message is self-correcting (the next mouse
    move repeats it); this one is not."""
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',
                           dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        # A session whose control channel has not opened yet — the ordering that loses the message.
        await _session(browser, opts='{channel:"connecting"}')
        await asyncio.sleep(.7)
        assert await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality').length") == 0
        # Nothing went out, so nothing may be remembered as having gone out.
        assert await browser.js("__PC.__rdOutbox().length") == 0
        # The channel opening is what triggers the retry, through `_callUI` like every other event.
        await browser.js("__PC.__rdOpenChannel()")
        await asyncio.sleep(.7)
        asks = await browser.js("__PC.__rdOutbox().filter(m=>m.t==='quality').map(m=>m.d)")
        assert asks and asks[-1] > 1, asks

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not desktop.Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_real_ctrl_wheel_zooms_in_small_steps_at_the_pointer():
    """The gesture the report is actually about, dispatched as a real wheel event.

    "zooms way too much" was two things at once: a step of 25% of Fit, and a zoom about the centre of
    the window so that whatever was being pointed at slid away. A notch is ~8% now and the point under
    the cursor stays under the cursor — measured here by asking the shipped mapping where that screen
    position is on the remote screen, before and after."""
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',
                           dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await _session(browser)
        await browser.js("__PC.__rdZoomSetMode('actual')")
        spot = await browser.js("""(()=>{const b=document.getElementById('call-remote').parentElement
          .getBoundingClientRect();return {x:b.left+b.width*0.22,y:b.top+b.height*0.7,
          fx:0.22,fy:0.7}})()""")

        async def under_cursor():
            st = await _state(browser)
            return st, _expected(st, spot['fx'], spot['fy'])

        before, where_before = await under_cursor()
        for _ in range(4):
            await browser.call('Input.dispatchMouseEvent', dict(type='mouseWheel', x=spot['x'], y=spot['y'],
                                                                deltaX=0, deltaY=-120, modifiers=2,
                                                                pointerType='mouse'))
            await asyncio.sleep(.08)
        after, where_after = await under_cursor()
        # FOUR NOTCHES IS ~36%, not 4x and not 100%.
        ratio = after['scale'] / before['scale']
        assert abs(ratio - 1.08 ** 4) < 0.02, (before['scale'], after['scale'], ratio)
        assert after['mode'] == 'free', after
        # ANCHORED: the remote point under the cursor did not move.
        assert abs(where_after[0] - where_before[0]) < 0.01, (where_before, where_after)
        assert abs(where_after[1] - where_before[1]) < 0.01, (where_before, where_after)
        # And back down again, to the same place.
        for _ in range(4):
            await browser.call('Input.dispatchMouseEvent', dict(type='mouseWheel', x=spot['x'], y=spot['y'],
                                                                deltaX=0, deltaY=120, modifiers=2,
                                                                pointerType='mouse'))
            await asyncio.sleep(.08)
        back, where_back = await under_cursor()
        assert abs(back['scale'] - before['scale']) < 0.01, (before['scale'], back['scale'])
        assert abs(where_back[0] - where_before[0]) < 0.01, (where_before, where_back)
        # A WHEEL WITH NO MODIFIER IS NEVER A ZOOM, and what it IS depends on who owns the gesture.
        # View-only (this session), it is the pan every VNC viewer gives you; shift is its horizontal
        # half. The scale must not move either way.
        await browser.js("__PC.__rdZoomSetMode('actual')")
        plain = await _state(browser)
        await browser.call('Input.dispatchMouseEvent', dict(type='mouseWheel', x=spot['x'], y=spot['y'],
                                                            deltaX=0, deltaY=240, modifiers=0, pointerType='mouse'))
        await asyncio.sleep(.15)
        panned = await _state(browser)
        assert abs(panned['scale'] - plain['scale']) < 1e-9, (plain['scale'], panned['scale'])
        assert panned['at']['y'] > plain['at']['y'] + 1e-4, (plain['at'], panned['at'])
        assert abs(panned['at']['x'] - plain['at']['x']) < 1e-9, (plain['at'], panned['at'])
        await browser.call('Input.dispatchMouseEvent', dict(type='mouseWheel', x=spot['x'], y=spot['y'],
                                                            deltaX=0, deltaY=240, modifiers=8, pointerType='mouse'))
        await asyncio.sleep(.15)
        sideways = await _state(browser)
        assert sideways['at']['x'] > panned['at']['x'] + 1e-4, (panned['at'], sideways['at'])
        assert abs(sideways['at']['y'] - panned['at']['y']) < 1e-9, (panned['at'], sideways['at'])

        # CONTROLLING, the same wheel belongs to the OTHER MACHINE — scrolling the remote window is
        # the point of having control — so it is forwarded and the view does not move.
        await browser.js("__PC.__rdSetControl(true);window.__before=__PC.__rdOutbox().length")
        held = await _state(browser)
        await browser.call('Input.dispatchMouseEvent', dict(type='mouseWheel', x=spot['x'], y=spot['y'],
                                                            deltaX=0, deltaY=240, modifiers=0, pointerType='mouse'))
        await asyncio.sleep(.15)
        sent = await browser.js("__PC.__rdOutbox().slice(__before).filter(m=>m.t==='input').map(m=>m.e.type)")
        assert 'wheel' in sent, sent
        still = await _state(browser)
        assert abs(still['at']['x'] - held['at']['x']) < 1e-9 and abs(still['at']['y'] - held['at']['y']) < 1e-9, (held['at'], still['at'])

    asyncio.run(desktop.with_browser('online', '', check))
