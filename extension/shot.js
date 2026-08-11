/* Save the whole page as a picture, into your Notes.
 *
 * The note is a real `pcai:note:<id>` (kind 30078, NIP-44 to your own key) exactly as the "save
 * selection" path writes — this only adds the picture. The picture cannot live IN the note: NIP-44
 * refuses plaintext over 65535 bytes and a full-page PNG is far past that, so it goes to the
 * encrypted drive and the note references it as `pcres:<sha>`, which is the same shape the app's own
 * Notes attachments use. The app renders it without needing anything in the drive INDEX, because a
 * note carries its attachment's name and mime itself — which is why this never touches that index.
 * (Writing it would mean a read-modify-write of the whole thing from a browser extension, and an
 * empty read written back over a full index is the one failure this codebase has a recovery script
 * for. Not from here.)
 *
 * THE CAPTURE. `captureVisibleTab` gives exactly one screenful, so a full page is: measure, scroll,
 * capture, repeat, stitch. Every part of that is a compromise with how real pages behave:
 *
 *   - FIXED elements repeat. A sticky header photographed at every scroll position appears in every
 *     tile. They are hidden for the capture and restored afterwards, which is the difference between
 *     a screenshot and a picture of a header seven times.
 *   - LAZY images need a beat to load after each scroll, so there is a settle delay per tile. It is
 *     the single biggest cost here and the reason this is not instant.
 *   - The LAST tile usually overlaps the one before it (the page rarely divides evenly), so it is
 *     drawn at its true offset rather than appended, or the bottom of the page appears twice.
 *   - Browsers RATE-LIMIT captureVisibleTab. Exceeding it throws mid-page; a small delay between
 *     tiles keeps us under it, and a failed tile stops the sweep with what it has rather than
 *     abandoning the lot.
 *   - A very long page would be a canvas no browser will allocate, so the height is capped and the
 *     note says it was truncated. Silently returning the top 30% of a page is worse than saying so.
 */
(function () {
  const B = (typeof browser !== 'undefined' ? browser : chrome);

  // A canvas past this fails to allocate on some GPUs and silently returns blank pixels rather than
  // throwing — so the cap is ours, and stated, rather than the browser's and invisible.
  const MAX_PIXELS = 40e6;          // ~40MP: a 1440-wide page ~27,000px tall
  const SETTLE_MS = 260;            // after each scroll: lazy images + sticky re-layout
  const THROTTLE_MS = 220;          // under Chrome's captureVisibleTab quota (~2/s)
  const MAX_TILES = 60;

  /* Runs IN the page. Returns its metrics, hides position:fixed/sticky elements, and scrolls. Kept as
   * one injected function with a `step` argument because every executeScript round trip costs a
   * message hop, and a long page does this dozens of times. */
  function _pageAgent(step, y) {
    const de = document.documentElement, bd = document.body;
    if (step === 'measure') {
      const hidden = [];
      for (const el of document.querySelectorAll('*')) {
        const cs = getComputedStyle(el);
        if (cs.position === 'fixed' || cs.position === 'sticky') {
          // Remember what it HAD, so restoring cannot invent a value it never carried.
          hidden.push([el, el.style.visibility]);
          el.style.visibility = 'hidden';
        }
        if (hidden.length > 400) break;              // pathological page; stop paying for it
      }
      window.__pcShotHidden = hidden;
      window.__pcShotScroll = window.scrollY;
      window.__pcShotOverflow = de.style.overflow;
      window.__pcShotBehav = de.style.scrollBehavior;
      // Some pages hide the scrollbar and drive scrolling themselves; force it for the sweep.
      de.style.overflow = 'auto';
      /* SMOOTH SCROLLING IS TURNED OFF FOR THE SWEEP. `html{scroll-behavior:smooth}` is common, and
       * it makes `scrollTo` animate — so the position read afterwards is wherever the animation had
       * got to, a tile behind. That silently drew tile 3 over tile 2 and ended the sweep two
       * screenfuls in, presenting two overlapping screens as the whole page. */
      de.style.scrollBehavior = 'auto';
      return {
        w: Math.min(de.clientWidth || window.innerWidth, window.innerWidth),
        h: Math.max(bd ? bd.scrollHeight : 0, de.scrollHeight, de.clientHeight),
        vh: window.innerHeight,
        dpr: window.devicePixelRatio || 1,
        title: (document.title || '').slice(0, 300),
        url: location.href.slice(0, 2000),
      };
    }
    if (step === 'to') { window.scrollTo(0, y); return true; }
    // Read POSITION separately, after the caller's settle delay — never in the same call that asked
    // for the scroll, whose answer is the position the browser had reached at that instant.
    if (step === 'at') return window.scrollY;
    if (step === 'restore') {
      for (const [el, v] of (window.__pcShotHidden || [])) { try { el.style.visibility = v; } catch (_) {} }
      de.style.overflow = window.__pcShotOverflow || '';
      de.style.scrollBehavior = window.__pcShotBehav || '';
      window.scrollTo(0, window.__pcShotScroll || 0);
      delete window.__pcShotHidden; delete window.__pcShotScroll;
      delete window.__pcShotOverflow; delete window.__pcShotBehav;
      return true;
    }
    return null;
  }

  const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function _run(tabId, step, y) {
    const [res] = await B.scripting.executeScript({
      target: { tabId }, func: _pageAgent, args: [step, y || 0],
    });
    return res && res.result;
  }

  /* Capture the whole page and return {blob, meta}. `onStep` reports progress, because a long page is
   * many seconds of work and a button that does nothing visible for ten of them reads as broken. */
  async function capture(tabId, onStep) {
    const say = (m) => { try { onStep && onStep(m); } catch (_) {} };
    say('measuring…');
    /* The `try` OPENS BEFORE the measure, because measuring is not read-only: it hides every fixed
     * and sticky element and forces the scroll style. Throwing on a bad measurement outside the
     * finally left the page with an invisible header and no scrollbar until it was reloaded. */
    let m = null;
    try {
      m = await _run(tabId, 'measure');
      if (!m || !m.w || !m.h) throw new Error('this page would not report its size');
      /* THE REAL devicePixelRatio, not a clamped one. `captureVisibleTab` returns the tile at the
       * page's ACTUAL scale, so a canvas sized to a clamped ratio and a tile drawn at its natural
       * size disagree by that factor: on a 3x display every tile came back 50% oversized, the right
       * third fell off the canvas and consecutive tiles overlapped by a third of a screen. The
       * destination size is now given to drawImage explicitly, so the tile is scaled to the geometry
       * whatever ratio it arrives at — which also covers a window moved between monitors mid-sweep. */
      const dpr = m.dpr || 1;
      let pageH = m.h;
      const capped = pageH * m.w * dpr * dpr > MAX_PIXELS;
      if (capped) pageH = Math.floor(MAX_PIXELS / (m.w * dpr * dpr));

      const cv = new OffscreenCanvas(Math.round(m.w * dpr), Math.round(pageH * dpr));
      const cx = cv.getContext('2d');
      let y = 0, tiles = 0, cut = false, last = -1;
      while (y < pageH && tiles < MAX_TILES) {
        await _run(tabId, 'to', y);
        await _sleep(SETTLE_MS);
        /* WHERE IT ACTUALLY IS, read after the settle. `??`, not `||`: a scroll position of 0 is a
         * real answer, and treating it as "missing" defeated the stall guard below — on a page held
         * at the top by a consent modal the loop happily stitched sixty copies of the same screen. */
        const at = (await _run(tabId, 'at')) ?? y;

        /* THE TAB MUST STILL BE THE ONE WE ARE PHOTOGRAPHING.
         *
         * `captureVisibleTab` photographs whatever is ACTIVE in the window — there is no API to
         * capture a named tab. The popup closes the moment you click the tab strip, while this loop
         * keeps running for another twenty seconds: switch to your bank and the remaining tiles are
         * pictures of your bank, stitched into a note titled with the original page and kept in the
         * encrypted drive for ever. So every tile checks first, and a switch ENDS the sweep. */
        let tab = null;
        try { tab = await B.tabs.get(tabId); } catch (_) {}
        if (!tab || !tab.active) { cut = true; break; }

        let dataUrl;
        try {
          dataUrl = await B.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
        } catch (e) {
          // Rate limit or a tab that navigated. Keep what we have — a partial page beats nothing,
          // and the note says how far it got.
          cut = true;
          break;
        }
        const bmp = await createImageBitmap(await (await fetch(dataUrl)).blob());
        cx.drawImage(bmp, 0, Math.round(at * dpr), Math.round(m.w * dpr), Math.round(m.vh * dpr));
        bmp.close && bmp.close();
        tiles++;
        say(`captured ${Math.min(100, Math.round((at + m.vh) / pageH * 100))}%`);
        if (at + m.vh >= pageH - 1) break;        // reached the bottom
        if (at <= last) { cut = true; break; }    // it stopped moving: stop rather than loop
        last = at;
        y = at + m.vh;
        await _sleep(THROTTLE_MS);
      }
      const blob = await cv.convertToBlob({ type: 'image/png' });
      return { blob, meta: { ...m, capped: capped || cut || tiles >= MAX_TILES, tiles } };
    } finally {
      // ALWAYS put the page back, including when a capture threw. Leaving someone's sticky header
      // invisible and their scroll position moved is a worse bug than the one that caused it.
      try { await _run(tabId, 'restore'); } catch (_) {}
    }
  }

  self.PCShot = { capture };
})();
