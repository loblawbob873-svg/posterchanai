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
      const covered = (htmlWins || []).some(w => w && !w.minimised && w.z > (it.z || 0)
                                               && coversMoreThanASliver(it.rect, w.rect));
      const hide = !!it.minimised || !it.rect || !(it.rect.width > 0) || !(it.rect.height > 0)
                   || covered;
      (hide ? stash : show).push(it.native);
    }
    return { stash, show };
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
                stashPlan, changed, driftPlan,
                previewIsBlank };
  root.PCOSNative = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
