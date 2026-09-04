/* A LINUX APP INSIDE A POSTERCHAN WINDOW.
 *
 * Firefox on this desktop must not look like firefox ON TOP OF this desktop. It gets a PosterChan
 * window — our title bar, our buttons, our taskbar entry, dragged and resized like every other
 * window here — and the real surface is kept exactly over that window's body by the compositor.
 * There is no reparenting and there is no compositing trick: the frame is HTML, the app is a real
 * Wayland surface, and this file is the arithmetic that keeps the two in the same place.
 *
 * THE THREE THINGS THAT MAKE THAT ARITHMETIC NON-OBVIOUS, each of which puts the window somewhere
 * wrong rather than failing:
 *
 *   1. THE PAGE IS NOT MEASURED IN THE COMPOSITOR'S PIXELS. The client scales itself with
 *      `body{zoom}` by viewport, and the display has its own scale factor. So a CSS pixel here is
 *      some other number of pixels out there, and it is not devicePixelRatio — it is both of those
 *      multiplied. It is not guessed: the shell's OWN window is a rectangle we can measure in both
 *      coordinate systems at once, and their ratio is the conversion. Measured, never assumed.
 *
 *   2. THE SHELL IS NOT AT THE ORIGIN. On a laptop with an external display the output the desktop
 *      is on starts at x=1920, and a window placed at "100" lands on the other screen.
 *
 *   3. A NATIVE WINDOW IS ALWAYS ABOVE THE SHELL. Ours is a tiled compositor window and firefox is
 *      a floating one; nothing this page can express reaches across that. So an HTML window that
 *      overlaps a native one is DRAWN UNDERNEATH IT — the compose modal, the start menu, a settings
 *      window, all of them behind a browser that is merely still open. The only cure the compositor
 *      offers is to put the native window away while something of ours needs the space, which is
 *      what `stashPlan` decides.
 *
 * DOM-free on purpose: tests/test_os_native_windows.py runs this file under node.
 */
(function(root){
  'use strict';

  /* The conversion between this page's pixels and the compositor's, derived from the one rectangle
   * both can see: the shell's own window. Null when it cannot be measured — and a null is returned
   * rather than a 1, because placing a window with the wrong scale is worse than not placing it. */
  function scaleFrom(shellRect, cssW, cssH){
    if(!shellRect || !(shellRect.width > 0) || !(shellRect.height > 0)) return null;
    if(!(cssW > 0) || !(cssH > 0)) return null;
    return { x: shellRect.width / cssW, y: shellRect.height / cssH,
             ox: shellRect.x || 0, oy: shellRect.y || 0 };
  }

  /** Where the compositor must put a surface so it fills `body`, a rectangle in page pixels. */
  function mapRect(body, scale){
    if(!body || !scale) return null;
    const x = Math.round(scale.ox + body.left * scale.x);
    const y = Math.round(scale.oy + body.top * scale.y);
    const w = Math.round(body.width * scale.x);
    const h = Math.round(body.height * scale.y);
    /* A window with no area is not a placement — it is a window that has been minimised, parked or
     * measured while its container was display:none, and sending it makes the app redraw itself at
     * 1x1 and forget its layout. The caller stashes instead. */
    if(w < 8 || h < 8) return null;
    return { x, y, w, h };
  }

  /* Clamp a PosterChan frame to one renderer/output's usable rectangle. Kept DOM-free because the
   * same rule must govern floating geometry saved before a snap and live frame geometry. */
  function clampLocalRect(rect, bounds, minimum){
    const b=bounds||{}, r=rect||{}, m=minimum||{}, gap=Math.max(0,Number(m.gap)||0);
    const bw=Math.max(1,Number(b.width)||1), bh=Math.max(1,Number(b.height)||1);
    const roomW=Math.max(1,bw-gap*2), roomH=Math.max(1,bh-gap*2);
    const minW=Math.min(roomW,Math.max(1,Number(m.width)||1));
    const minH=Math.min(roomH,Math.max(1,Number(m.height)||1));
    const w=Math.min(roomW,Math.max(minW,Number(r.w)||minW));
    const h=Math.min(roomH,Math.max(minH,Number(r.h)||minH));
    const x=Math.min(Math.max(Number(r.x)||0,gap),Math.max(gap,bw-gap-w));
    const y=Math.min(Math.max(Number(r.y)||0,gap),Math.max(gap,bh-gap-h));
    return {x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h)};
  }

  /* ── THE WORK AREA: AN OUTPUT MINUS THE TASKBAR, IN COMPOSITOR UNITS ────────────────────────
   *
   * THE TASKBAR IS THE ONE PART OF THIS DESKTOP THAT NOTHING OUTSIDE IT KNOWS ABOUT. It is painted
   * at the bottom of the shell's own surface, and the shell is the TILED window every native app
   * floats above — so a native window over that band does not merely overlap the bar, it HIDES it,
   * and there is no stacking order anywhere that puts it back. Every other desktop states the band
   * to the compositor as a layer-shell exclusive zone and lets the compositor keep windows out of
   * it; an Electron toplevel cannot make one, so Wayfire's `workarea` is reported as the WHOLE
   * output — and its `place` plugin, its grid/maximise, and every application's own remembered
   * geometry are then all free to land on the bar, none of them wrong by their own lights.
   *
   * MEASURED, on the two-monitor 3072x2048 desk at output scale 1.25, with the shell renderer
   * alive from before the launch until after the reading: `firefox-bin` started from the start
   * menu opened on DP-2 at geometry {3205,47,2913,2080} — 2080 tall on a 2048-tall output, i.e.
   * past the bottom edge of the screen — and stayed exactly there for the twenty-five seconds it
   * was watched. The taskbar is the bottom 38 of those units. Nothing moved it because nothing in
   * this desktop places a window it does not host, and hosting is off by default.
   *
   * DERIVED FROM THE MEASURED DESK, NEVER FROM THE 48px CONSTANT. os.js's place() already carries
   * the note explaining why that constant is wrong at some zooms (at 1280x800 the real desk is 16
   * layout px shorter than the constant claims, which opened windows 3px under the bar). This is
   * that same measurement, converted once into the compositor's units. */

  //: A reserve wider than this share of the output is a mis-measurement, not a taskbar — a desk
  //: measured while the shell was hidden reads zero high, and obeying that pins every window on
  //: the machine into a strip. Refuse it and keep the whole output, which is today's behaviour.
  const MAX_RESERVE = 0.5;

  //: Below this a window is not shortened, it is destroyed — so one dragged bodily onto the bar is
  //: moved up instead of being squeezed into a sliver of its former self.
  const MIN_TALL = 240;

  /** The rectangle a native window may occupy on this shell's output, in compositor units.
   *  `deskRect` is `#os-desk`'s getBoundingClientRect() and `cssH` the visual viewport height,
   *  both in page pixels; `scale` is scaleFrom()'s answer. Null when the shell itself could not be
   *  measured — a guessed work area places windows worse than no rule at all. */
  function workAreaFrom(shellRect, deskRect, cssH, scale){
    if(!shellRect || !scale) return null;
    const w = Math.round(Number(shellRect.width) || 0), h = Math.round(Number(shellRect.height) || 0);
    if(!(w > 0) || !(h > 0)) return null;
    const area = { x: Math.round(Number(shellRect.x) || 0), y: Math.round(Number(shellRect.y) || 0),
                   w, h, reserve: 0 };
    const bottom = deskRect && Number(deskRect.bottom);
    if(!(Number(cssH) > 0) || !Number.isFinite(bottom) || !(bottom > 0)) return area;
    const reserve = Math.round(Math.max(0, Number(cssH) - bottom) * (Number(scale.y) || 0));
    if(!(reserve > 0) || reserve > h * MAX_RESERVE) return area;
    return { x: area.x, y: area.y, w, h: h - reserve, reserve };
  }

  /** Is this window on the output the work area was measured from? Horizontally by its centre —
   *  the usual "which screen is it on" — and vertically by mere OVERLAP, because the window this
   *  rule exists for is one hanging off the bottom edge, whose centre can be past it entirely. The
   *  reserved band is part of the output even though it is not part of the area, so it is added
   *  back here. */
  function inWorkOutput(area, rect){
    if(!area || !rect) return false;
    const cx = rect.x + rect.w / 2, bottom = area.y + area.h + (area.reserve || 0);
    return cx >= area.x && cx < area.x + area.w
        && rect.y < bottom && rect.y + rect.h > area.y;
  }

  /* WHICH NATIVE WINDOWS ARE SITTING ON THE TASKBAR, AND WHERE THEY BELONG.
   *
   * VERTICAL ONLY, deliberately. The taskbar is a band across the bottom, so whether a window is
   * on it is a question about y and height and nothing else; sliding somebody's window sideways to
   * keep a bar visible is a correction nobody asked for, and it would additionally have to trust
   * the x coordinate a row arrives with, which is the one axis this codebase has moved twice.
   *
   * SETTLED, NOT SNAPSHOT. A window is opened, dragged and resized through dozens of intermediate
   * rectangles — Firefox alone published three in its first 1.2 seconds — and correcting one of
   * those fights the gesture that is producing them: the window jumps back under the pointer, and
   * every toggle is a compositor round trip. So a rectangle is acted on only once it has been seen
   * TWICE unchanged, which `prev`/`seen` carry between passes. That also means a deliberate drag
   * downwards is answered once, when the hand stops, instead of continuously.
   *
   * `below`/`above` are how far the server-side decoration reaches past the content — 29 and 23
   * units on the measured desk. Judged without `below` the correction is 29 units short and a
   * strip of bar stays covered, which reads as "it did nothing"; ignoring `above` loses the TITLE
   * BAR off the top of the screen, i.e. the close button, on a window that was merely too tall.
   *
   * IT SHRINKS BEFORE IT MOVES, and that ordering is the difference between a window that is still
   * where its owner put it and one that jumped. Taking the overhang off the bottom keeps the top
   * edge exactly where it was; moving is the answer only for a window that cannot be made to fit
   * that way and still be worth having — one dragged bodily onto the bar, not one that is tall.
   *
   * A FULLSCREEN WINDOW IS LEFT ALONE, because a fullscreen application owns the screen by
   * definition and the taskbar is not visible under it in any desktop. So is a minimised one (it
   * is nowhere), one with no area (it is still mapping), and one of OUR OWN surfaces — the shell
   * itself fills the output including the band, and "correcting" it would shrink the desktop out
   * from under its own taskbar, once per pass, for ever. */
  function taskbarPlan(rows, area, prev){
    const seen = new Map(), place = [];
    if(!area || !(area.reserve > 0)) return { place, seen };
    const floor = area.y + area.h;
    for(const row of (rows || [])){
      const id = Number(row && row.id);
      if(!Number.isFinite(id)) continue;
      const r = (row && row.rect) || {};
      const rect = { x: Math.round(Number(r.x) || 0), y: Math.round(Number(r.y) || 0),
                     w: Math.round(Number(r.width) || 0), h: Math.round(Number(r.height) || 0) };
      if(!(rect.w > 0) || !(rect.h > 0)) continue;
      const key = rect.x + ',' + rect.y + ',' + rect.w + ',' + rect.h;
      seen.set(id, key);
      if(row.own || row.fullscreen || row.stashed) continue;
      if(!inWorkOutput(area, rect)) continue;
      if(!prev || prev.get(id) !== key) continue;          // still moving: this is not its answer
      const decor = n => Math.min(256, Math.max(0, Math.round(Number(n) || 0)));
      const below = decor(row.below), above = decor(row.above);
      if(rect.y + rect.h + below <= floor) continue;       // already clear of the band
      const top = area.y + above;                          // where the title bar has to start
      const shrunk = floor - below - rect.y;
      let y = rect.y, h;
      if(shrunk >= MIN_TALL && rect.y >= top){ h = shrunk; }
      else {
        h = Math.min(rect.h, Math.max(1, floor - below - top));
        y = Math.min(Math.max(rect.y, top), floor - below - h);
      }
      if(y === rect.y && h === rect.h) continue;
      place.push({ id, rect: { x: rect.x, y, w: rect.w, h } });
    }
    return { place, seen };
  }

  const overlaps = (a, b) => !!(a && b)
    && a.left < b.left + b.width && b.left < a.left + a.width
    && a.top < b.top + b.height && b.top < a.top + a.height;

  /* WHICH NATIVE WINDOWS MUST GO AWAY RIGHT NOW — i.e. CLICKING A WINDOW PUTS IT IN FRONT.
   *
   * THE CONSTRAINT, because this rule has been written three ways and every rewrite starts by not
   * knowing it: a native app is a FLOATING sway window and this whole desktop is the one TILED
   * window. sway paints floating above tiled, always. So a PosterChan window can never be drawn in
   * front of Firefox or Telegram — there is no shared stacking order to fix, and the only lever
   * anything here has is whether the native surface is on the screen at all.
   *
   * Which means: "the window you clicked goes in front" and "the app behind it keeps its pixels on
   * screen" cannot both be true. One of them has to give, and the desktop everybody already knows
   * gives up the second one — a covered window is covered. So a native app is put away exactly
   * when a PosterChan window that is ABOVE IT overlaps it, and comes straight back when that stops
   * being true (focusing it raises it, so it un-covers itself on the very next pass).
   *
   * THE OTHER HALF OF THIS RULE IS IN THE STYLESHEET, and without it this is the bug rather than
   * the fix. `.osw.native-stashed` used to be `visibility:hidden`, so putting the surface away
   * ALSO took the title bar, the border and the whole frame off the screen: from the person's side
   * the app had not gone behind, it had vanished — reported exactly that way, and answered by
   * deleting this rule, which then left Telegram on top of everything for ever. The frame now
   * stays, occluded by whatever covers it like any background window, and clicking it brings the
   * app back. Do not restore one of these two halves without the other.
   *
   * Both directions of the comparison are load-bearing: a PosterChan window BELOW a native app
   * must not touch it, or every window would stash everything it happens to share pixels with.
   *
   * AND IT IS NOT ANY OVERLAP — A SLIVER IS NOT A COVER. Reported as "Settings is now glitching my
   * screen and telegram, sticking to that on desktop": a Settings window whose edge lapped about
   * 38px over Telegram's took the WHOLE of Telegram off the screen and replaced it with a frozen
   * screenshot. The rule was `overlaps()`, i.e. one shared pixel, and windows abut constantly.
   *
   * The trade is ASYMMETRIC, which is why the threshold is not zero. Parking costs the entire
   * native app no matter how small the overlap was — it becomes a still image of itself. NOT
   * parking costs only the overlapped band, and that cost scales with the band. One is fixed and
   * large, the other is proportional, so they cross somewhere above zero.
   *
   * IT IS MEASURED AGAINST THE COVERING WINDOW, NEVER THE NATIVE ONE, and that is the whole
   * subtlety. "How much of Telegram is covered" sounds like the question and is the wrong one: a
   * small dialog opened in the middle of a MAXIMISED Firefox covers about 2% of it, so a rule
   * written that way would leave Firefox on top and the dialog invisible and unclickable — which
   * is the maximised-Firefox bug this desktop has already paid for twice. The rule exists to make
   * the window you clicked usable, so it asks how much of THAT window is blocked.
   *
   * A sliver is an overlap thinner than `SLIVER` in either direction. That keeps the two cases
   * that matter on the right sides of the line: 38px of Telegram over Settings' edge is incidental
   * and Telegram stays, while a dialog sitting inside Firefox is overlapped in both axes by its
   * own full size and Firefox goes away. */

  //: An intersection thinner than this in either axis is windows touching, not one covering the
  //: other. Roughly a fingertip; well above the border/rounding overlaps that are pure accident.
  const SLIVER = 64;

  /** Does `w` cover enough of the rectangle it sits over to be worth parking the app underneath? */
  function coversMoreThanASliver(nativeRect, w){
    if(!overlaps(nativeRect, w)) return false;
    const wide = Math.min(nativeRect.left + nativeRect.width, w.left + w.width)
               - Math.max(nativeRect.left, w.left);
    const tall = Math.min(nativeRect.top + nativeRect.height, w.top + w.height)
               - Math.max(nativeRect.top, w.top);
    // A window smaller than the slop in one axis can never overlap by more than it is, so judge it
    // against its own size instead — otherwise a narrow palette could never park anything.
    return wide >= Math.min(SLIVER, w.width) && tall >= Math.min(SLIVER, w.height);
  }

  function stashPlan(items, htmlWins){
    const stash = [], show = [];
    for(const it of (items || [])){
      if(!it || it.native == null) continue;
      /* A LIVE GESTURE PARKS ON ANY OVERLAP — the threshold is for windows at REST.
       *
       * Reported as "terminal is glitching out on moving it". While a window is being dragged its
       * frame is pushed here as an overlay, and judged by the sliver rule the overlap crosses 64px
       * again and again as it moves: the surface parks, unparks, parks — and every toggle is a
       * scratchpad round trip plus a full screen capture for the preview. Flicker during a drag is
       * far worse than a native app briefly leaving for a 9px lap, and the drag ENDS, so nothing
       * is left parked by it. `live` says which rectangles are a gesture rather than a window. */
      const covered = (htmlWins || []).some(w => w && !w.minimised && w.z > (it.z || 0)
                                               && (w.live ? overlaps(it.rect, w.rect)
                                                          : coversMoreThanASliver(it.rect, w.rect)));
      const hide = !!it.minimised || !it.rect || !(it.rect.width > 0) || !(it.rect.height > 0)
                   || covered;
      (hide ? stash : show).push(it.native);
    }
    return { stash, show };
  }

  /* A real PosterChan toplevel participates in Sway's ordinary floating stack and only needs
   * focus. A legacy/fallback DOM frame lives inside the tiled shell and can never rise above a
   * floating Telegram/Firefox surface, so overlapping external surfaces must be parked while that
   * frame owns focus. Keep this decision pure: os.js supplies rectangles in compositor pixels and
   * performs the bounded hide/show operations. */
  function domStackPlan(rows, focusedRect){
    const hide=[], show=[];
    for(const row of (rows||[])){
      if(!row || row.id==null || row.own || row.fullscreen) continue;
      const r=row.rect;
      const covered=!!(focusedRect && r && coversMoreThanASliver(
        {left:Number(r.x)||0,top:Number(r.y)||0,width:Number(r.width)||0,height:Number(r.height)||0},
        focusedRect));
      (covered?hide:show).push(Number(row.id));
    }
    return {hide,show};
  }

  /* Only what CHANGED. Every one of these is a round trip to the compositor and a reconfigure the
   * app must handle; re-sending an identical rectangle sixty times a second is how a browser is
   * made to relayout continuously while somebody drags a window that is not even theirs. */
  function changed(prev, next){
    if(!next) return false;
    if(!prev) return true;
    return prev.x !== next.x || prev.y !== next.y || prev.w !== next.w || prev.h !== next.h;
  }

  /* WHAT THE COMPOSITOR ACTUALLY DID, VERSUS WHAT WE ASKED IT TO DO.
   *
   * The desktop judged a native surface's geometry against its own record of the last placement it
   * sent. That record is INTENT, and read as truth it can disagree with sway for ever with nothing
   * to resolve it — the same mistake already documented for the parked/hidden flag, made a second
   * time one field over. Anything that moves or resizes a window without us is therefore permanent:
   * Firefox restoring its own session geometry as it finishes starting, a sway keybinding, a client
   * that resizes itself. Measured on a real desktop: a 1280x1624 HTML frame containing a 1394x867
   * Firefox whose POSITION tracked the frame exactly and whose size was never once corrected —
   * "Firefox is launching and it does not even fit in the PosterChan window".
   *
   * `tol` and the give-up count are both load-bearing. A terminal resizes in whole character cells
   * and can NEVER be exactly the rectangle it is asked for, so an exact comparison would re-place
   * it every few seconds for the life of the desktop — worse than being eight pixels out. After two
   * corrections at the same target the current geometry is accepted until the target changes.
   *
   * `memo` is this window's give-up state and is returned rather than mutated, so the whole rule is
   * one pure function the tests can drive.
   */
  function driftPlan(want, real, memo, tol){
    const T = Math.max(0, Number(tol) || 0);
    if(!want || want === 'hidden' || !real) return { replace:false, memo:memo || null };
    const off = Math.abs(want.x - real.x) > T || Math.abs(want.y - real.y) > T
             || Math.abs(want.w - real.w) > T || Math.abs(want.h - real.h) > T;
    if(!off) return { replace:false, memo:null };          // agreed — forget any give-up memory
    const again = !!(memo && memo.want && !changed(memo.want, want));
    const tries = again ? (Number(memo.tries) || 0) : 0;
    if(tries >= 2) return { replace:false, memo:{ want, tries } };
    return { replace:true, memo:{ want, tries: tries + 1 } };
  }

  /* IS THIS CAPTURE A PICTURE OF NOTHING?
   *
   * `grim` photographs a SCREEN REGION, not a window, and it always hands back a well-formed PNG —
   * so "it decoded" says nothing about whether there are any pixels in it. A surface that was
   * parked a moment earlier, an output that has not damaged a frame since grim subscribed, or a
   * rectangle that has drifted off the edge of every output all photograph as one flat dark colour.
   * Adopted as a preview that paints an OPAQUE black body under our label, and that is exactly the
   * reported "Firefox turns black with click to bring this window forward".
   *
   * The bright system card the no-preview state already paints is better than a black rectangle in
   * every one of those cases, so a blank capture is refused and falls back to it.
   *
   * Blank means flat AND dark: every sample within a hair of every other one, and dark overall. A
   * uniformly BRIGHT capture is deliberately kept — a blank white page is a real thing to be
   * looking at, and refusing it would replace a truthful preview with a card. Fully transparent
   * pixels count as black, because that is how they will be composited over the body.
   *
   * `px` is RGBA bytes as `getImageData().data` returns them; the sampling is in os.js, this is the
   * decision, and it lives here so it can be run under node. */
  function previewIsBlank(px){
    if(!px || !px.length || px.length % 4) return true;
    let min = 255, max = 0, sum = 0, n = 0;
    for(let i = 0; i < px.length; i += 4){
      const a = px[i+3];
      const lum = a === 0 ? 0
                : (0.2126*px[i] + 0.7152*px[i+1] + 0.0722*px[i+2]) * (a/255);
      if(lum < min) min = lum;
      if(lum > max) max = lum;
      sum += lum; n++;
    }
    if(!n) return true;
    return (max - min) <= 6 && (sum / n) <= 24;
  }

  const API = { scaleFrom, mapRect, clampLocalRect, overlaps, coversMoreThanASliver,
                workAreaFrom, inWorkOutput, taskbarPlan,
                stashPlan, domStackPlan, changed, driftPlan,
                previewIsBlank };
  root.PCOSNative = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
