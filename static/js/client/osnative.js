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
   * must not touch it, or every window would stash everything it happens to share pixels with. */
  function stashPlan(items, htmlWins){
    const stash = [], show = [];
    for(const it of (items || [])){
      if(!it || it.native == null) continue;
      const covered = (htmlWins || []).some(w => w && !w.minimised && w.z > (it.z || 0)
                                               && overlaps(it.rect, w.rect));
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

  const API = { scaleFrom, mapRect, overlaps, stashPlan, changed };
  root.PCOSNative = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
