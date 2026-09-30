/* PDF EDIT — fill in, mark up, sign and rearrange a PDF, inside Preview.
 *
 * Asked for: "make sure posterchan preview has PDF editing capabilities". Everything happens in this
 * page: the drive's files are ENCRYPTED and the server holds no key, so a server-side editor would
 * mean uploading the decrypted document — the one thing the drive exists to prevent (the same reason
 * preview.js renders with pdf.js rather than PyMuPDF). pdf.js draws the pages; pdf-lib (vendored,
 * MIT, the UMD build so it loads from a plain <script> like pdf.js does) writes the result.
 *
 * WHAT IT DOES: type text anywhere, highlight a region, draw freehand ink, draw a signature once and
 * place it, fill the document's own form fields, and rotate / delete / move pages. Nothing is changed
 * until Save: the edits are a list, applied to a fresh copy of the ORIGINAL bytes, so Cancel is free
 * and a save can never compound an earlier one.
 *
 * WHERE IT SAVES: `saveBack(blob)` when the caller gave one — the drive, a synced folder, a file on
 * This Computer, each through the writer it already uses for Office — otherwise "Save a copy" through
 * saveBlobAs (the only way a file reaches the device in the APK). */
(function (root) {
  'use strict';
  var PC = function () { return root.__PC || {}; };
  var toast = function (m) { try { (PC().toast || function () {})(m); } catch (_) {} };
  var H = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); };

  var _lib = null;
  function loadPdfLib() {
    if (root.PDFLib) return Promise.resolve(root.PDFLib);
    if (_lib) return _lib;
    _lib = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = '/static/vendor/pdf-lib/pdf-lib.min.js';
      s.onload = function () { root.PDFLib ? resolve(root.PDFLib) : reject(new Error('the PDF writer did not start')); };
      s.onerror = function () { _lib = null; reject(new Error('the PDF writer could not be loaded')); };
      document.head.appendChild(s);
    });
    return _lib;
  }

  /* THE FIELD'S KIND BY instanceof, NEVER BY constructor.name — the vendored build is minified,
   * so the class names are single letters and a name check matched nothing: every form showed an
   * empty "Form" strip. */
  function kindOf(L, f) {
    if (L.PDFCheckBox && f instanceof L.PDFCheckBox) return 'PDFCheckBox';
    if (L.PDFDropdown && f instanceof L.PDFDropdown) return 'PDFDropdown';
    if (L.PDFOptionList && f instanceof L.PDFOptionList) return 'PDFOptionList';
    if (L.PDFTextField && f instanceof L.PDFTextField) return 'PDFTextField';
    return '';
  }
  var TOOLS = [['text', 'Text'], ['highlight', 'Highlight'], ['draw', 'Draw'], ['sign', 'Sign']];

  /* The typed-signature styles: bundled script fonts (licences beside them in static/fonts). */
  var SIG_STYLES = [
    { family: 'PC Sig Dancing', file: 'sig-dancing.woff2', label: 'Dancing' },
    { family: 'PC Sig Great Vibes', file: 'sig-greatvibes.woff2', label: 'Elegant' },
    { family: 'PC Sig Allura', file: 'sig-allura.woff2', label: 'Classic' },
    { family: 'PC Sig Homemade', file: 'sig-homemade.woff2', label: 'Handwritten' },
  ];
  var SIG_INKS = [['Black', '#111111'], ['Blue', '#1a3a8f']];
  var _sigFonts = null;
  /* Every style, loaded once and ADDED to document.fonts: a canvas draws with a font only once it is
   * loaded, and drawing before that silently uses the fallback -- the placed signature would then be
   * in a different hand from the one previewed. */
  function loadSigFonts() {
    if (_sigFonts) return _sigFonts;
    if (!root.FontFace || !document.fonts) return (_sigFonts = Promise.resolve());
    _sigFonts = Promise.all(SIG_STYLES.map(function (st) {
      var f = new FontFace(st.family, 'url(/static/fonts/' + st.file + ') format("woff2")');
      return f.load().then(function (loaded) { document.fonts.add(loaded); }, function () {});
    }));
    return _sigFonts;
  }
  /* The typed name as a transparent PNG cropped to the ink, at 3x the placed size so it prints sharp.
   * {png, aspect}. The canvas is measured with the real font, so long names are never clipped. */
  function typedSignature(text, family, ink) {
    var size = 120, pad = Math.round(size * 0.35);
    var m = document.createElement('canvas').getContext('2d');
    m.font = size + 'px "' + family + '", cursive';
    var tm = m.measureText(text);
    var asc = tm.actualBoundingBoxAscent || size * 0.8, desc = tm.actualBoundingBoxDescent || size * 0.3;
    var left = tm.actualBoundingBoxLeft || 0, right = tm.actualBoundingBoxRight || tm.width;
    var w = Math.ceil(left + right + pad * 2), h = Math.ceil(asc + desc + pad * 2);
    var cv = document.createElement('canvas'); cv.width = Math.max(1, w); cv.height = Math.max(1, h);
    var c = cv.getContext('2d');
    c.font = m.font; c.fillStyle = ink; c.textBaseline = 'alphabetic';
    c.fillText(text, pad + left, pad + asc);
    return { png: cv.toDataURL('image/png'), aspect: cv.width / cv.height };
  }

  /* The editor. `pdfjs` is preview.js's loaded pdf.js; `bytes` the original file. Resolves when the
   * person closes the editor, with the saved Blob or null. */
  function edit(host, opts) {
    opts = opts || {};
    var name = opts.name || 'document.pdf', saveBack = opts.saveBack || null;
    var original = opts.bytes;                 // Uint8Array — never modified
    var state = {
      tool: 'text', color: '#ff2b6d',
      marks: [],                               // {page, type, ...pdf-space geometry}
      order: [],                               // original page indexes in their new order
      rotate: {},                              // original index -> extra degrees
      fields: {},                              // form field name -> value
      signature: null,                         // PNG data URL
      pages: [],                               // {index, el, overlay, viewport, width, height}
      dirty: false,
    };
    var resolveDone; var done = new Promise(function (r) { resolveDone = r; });

    host.innerHTML = '<div class="pe-root">'
      + '<div class="pe-bar" role="toolbar" aria-label="PDF tools">'
      + TOOLS.map(function (t) { return '<button class="btn btn-ghost small pe-tool" data-tool="' + t[0] + '" aria-pressed="false">' + t[1] + '</button>'; }).join('')
      + '<input type="color" class="pe-color" value="' + state.color + '" aria-label="Colour" title="Colour">'
      + '<button class="btn btn-ghost small pe-undo" title="Undo the last mark">Undo</button>'
      + '<button class="btn btn-ghost small pe-preview-btn" aria-pressed="false" title="See the document exactly as it will be saved">Preview</button>'
      + '<span class="pe-gap"></span>'
      + '<button class="btn btn-ghost small pe-cancel">Cancel</button>'
      + (saveBack ? '<button class="btn btn-neon small pe-save">Save</button>' : '')
      + '<button class="btn ' + (saveBack ? 'btn-ghost' : 'btn-neon') + ' small pe-copy">Save a copy</button>'
      + '</div>'
      + '<div class="pe-hint" role="status" hidden></div>'
      + '<div class="pe-fields hidden" aria-label="Form fields"></div>'
      + '<div class="pe-pages" role="document" aria-label="' + H(name) + '"><div class="spinner"></div></div>'
      + '<div class="pe-preview" hidden><div class="pe-preview-bar"><b>Preview</b><span class="muted small">This is exactly what Save writes.</span>'
      + '<button class="btn btn-neon small pe-preview-back">Back to editing</button></div><div class="pe-preview-pages"></div></div>'
      + '</div>';
    var q = function (s) { return host.querySelector(s); };
    var pagesBox = q('.pe-pages');

    function setTool(t) {
      var again = state.tool === t;
      state.tool = t; state.picked = null;
      host.querySelectorAll('.pe-tool').forEach(function (b) {
        var on = b.dataset.tool === t; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      state.pages.forEach(function (r) { r.overlay.style.touchAction = touchActionFor(); redraw(r.index); });
      // Sign pressed while Sign is already on: change the signature (a typo in a typed name was stuck).
      if (t === 'sign' && (!state.signature || again)) signaturePad();
      hint();
    }
    /* ONE FINGER MUST STILL SCROLL. The pages fill a phone's screen, and every overlay was
     * touch-action:none -- so a swipe drew, or dropped a text box, and the document could not be
     * scrolled past the first page ("the pdf editor has no scroll so i can't edit everything").
     * Tap tools (Text, Sign) let the browser pan and act on a TAP; the drawing tools keep one finger
     * for the pen and leave two fingers to pan and zoom. */
    function touchActionFor() { return (state.tool === 'draw' || state.tool === 'highlight') ? 'pinch-zoom' : 'pan-x pan-y pinch-zoom'; }
    /* WHERE THE SIGNATURE GOES, said on screen: after "Use signature" the only cue was a toast. The
     * strip shows the signature itself, what a tap will do, and the way to change it. */
    function hint() {
      var el = q('.pe-hint'); if (!el) return;
      if (state.tool !== 'sign' || !state.signature) { el.hidden = true; el.innerHTML = ''; return; }
      el.hidden = false;
      el.innerHTML = '<img class="pe-hint-sig" alt="Your signature" src="' + H(state.signature) + '">'
        + '<span class="pe-hint-say">' + (state.picked ? 'Tap where this signature should go.' : 'Tap the page where your signature goes. Tap a placed signature to move it.') + '</span>'
        + (state.picked ? '<button class="btn btn-ghost small pe-hint-remove">Remove it</button>' : '')
        + '<button class="btn btn-ghost small pe-hint-change">Change signature</button>';
      el.querySelector('.pe-hint-change').onclick = function () { signaturePad(); };
      var rm = el.querySelector('.pe-hint-remove');
      if (rm) rm.onclick = function () {
        var m = state.picked; state.picked = null;
        var i = state.marks.indexOf(m); if (i >= 0) { state.marks.splice(i, 1); state.dirty = true; redraw(m.page); }
        hint();
      };
    }
    host.querySelectorAll('.pe-tool').forEach(function (b) { b.onclick = function () { setTool(b.dataset.tool); }; });
    q('.pe-color').oninput = function (e) { state.color = e.target.value; };
    q('.pe-undo').onclick = function () { var m = state.marks.pop(); if (m) { redraw(m.page); state.dirty = true; } };
    q('.pe-cancel').onclick = function () {
      if (state.dirty && PC().uiConfirm) {
        Promise.resolve(PC().uiConfirm('Discard your changes to this PDF?')).then(function (ok) { if (ok) finish(null); });
      } else finish(null);
    };
    var sv = q('.pe-save'); if (sv) sv.onclick = function () { save(true); };
    q('.pe-copy').onclick = function () { save(false); };
    /* PREVIEW ("let you see a preview or let you go back to see how the form looks as you edit"). The
     * form's values only ever showed in the strip above the pages, never IN them, so there was no way
     * to see the filled-in document before saving it. This renders build() -- the very bytes Save
     * would write, fields filled, signatures and marks in, pages rotated and removed -- and Back
     * returns to the untouched edit state: nothing about the edits changes by looking. */
    var previewBtn = q('.pe-preview-btn'), previewing = false, previewDoc = null;
    function closePreview() {
      previewing = false;
      host.querySelector('.pe-root').classList.remove('pe-previewing');
      q('.pe-preview').hidden = true; q('.pe-preview-pages').innerHTML = '';
      previewBtn.setAttribute('aria-pressed', 'false'); previewBtn.textContent = 'Preview';
      if (previewDoc) { try { previewDoc.destroy(); } catch (_) {} previewDoc = null; }
    }
    async function openPreview() {
      previewing = true;
      previewBtn.setAttribute('aria-pressed', 'true'); previewBtn.textContent = 'Back to editing';
      host.querySelector('.pe-root').classList.add('pe-previewing');
      var box = q('.pe-preview-pages'); q('.pe-preview').hidden = false;
      box.innerHTML = '<div class="spinner"></div>';
      try {
        var blob = await build();
        if (!previewing) return;
        var bytes = new Uint8Array(await blob.arrayBuffer());
        previewDoc = await opts.pdfjs.getDocument({ data: bytes }).promise;
        if (!previewing) return;
        box.innerHTML = '';
        var width = Math.max(260, Math.min((box.clientWidth || 800) - 24, 1100)), ratio = root.devicePixelRatio || 1;
        for (var n = 1; n <= previewDoc.numPages && previewing; n++) {
          var page = await previewDoc.getPage(n), base = page.getViewport({ scale: 1 });
          var vp = page.getViewport({ scale: width / base.width });
          var cv = document.createElement('canvas'); cv.className = 'pe-preview-page';
          cv.width = Math.ceil(vp.width * ratio); cv.height = Math.ceil(vp.height * ratio);
          cv.style.width = Math.floor(vp.width) + 'px'; cv.style.height = Math.floor(vp.height) + 'px';
          cv.setAttribute('aria-label', 'Page ' + n + ' as it will be saved');
          box.appendChild(cv);
          await page.render({ canvasContext: cv.getContext('2d'), viewport: vp, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null }).promise;
        }
      } catch (e) {
        if (previewing) box.innerHTML = '<div class="empty">The preview could not be drawn: ' + H((e && e.message) || e) + '</div>';
      }
    }
    previewBtn.onclick = function () { previewing ? closePreview() : openPreview(); };
    /* Escape cancels the innermost thing and nothing more: the signature dialog, then Preview. A text
     * note handles its own. It never discards the edits -- that is Cancel's job, and Cancel asks. */
    host.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var sheet = host.querySelector('.pe-sign-sheet');
      if (sheet) { e.preventDefault(); sheet.remove(); if (!state.signature) setTool('text'); return; }
      if (previewing) { e.preventDefault(); closePreview(); }
    });
    q('.pe-preview-back').onclick = closePreview;

    function finish(blob) { try { host.innerHTML = ''; } catch (_) {} resolveDone(blob); }

    /* ---- drawing the pages ---- */
    async function render() {
      var lib = opts.pdfjs;
      var doc = await lib.getDocument({ data: original.slice() }).promise;
      pagesBox.innerHTML = '';
      var width = Math.max(260, Math.min((pagesBox.clientWidth || 800) - 24, 1100));
      for (var n = 1; n <= doc.numPages; n++) {
        var page = await doc.getPage(n);
        var base = page.getViewport({ scale: 1 });
        var vp = page.getViewport({ scale: width / base.width });
        var ratio = root.devicePixelRatio || 1;
        var wrap = document.createElement('div'); wrap.className = 'pe-page'; wrap.dataset.index = String(n - 1);
        wrap.style.width = Math.floor(vp.width) + 'px'; wrap.style.height = Math.floor(vp.height) + 'px';
        var canvas = document.createElement('canvas'); canvas.className = 'pe-canvas';
        canvas.width = Math.ceil(vp.width * ratio); canvas.height = Math.ceil(vp.height * ratio);
        canvas.style.width = Math.floor(vp.width) + 'px'; canvas.style.height = Math.floor(vp.height) + 'px';
        var overlay = document.createElement('canvas'); overlay.className = 'pe-overlay';
        overlay.width = canvas.width; overlay.height = canvas.height;
        overlay.style.width = canvas.style.width; overlay.style.height = canvas.style.height;
        var tools = document.createElement('div'); tools.className = 'pe-page-tools';
        tools.innerHTML = '<span class="pe-page-no">Page ' + n + '</span>'
          + '<button class="btn btn-ghost small" data-pa="up" title="Move up" aria-label="Move page ' + n + ' up"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-up"></use></svg></button>'
          + '<button class="btn btn-ghost small" data-pa="down" title="Move down" aria-label="Move page ' + n + ' down"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-down"></use></svg></button>'
          + '<button class="btn btn-ghost small" data-pa="rot" title="Rotate" aria-label="Rotate page ' + n + '"><svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg></button>'
          + '<button class="btn btn-ghost small" data-pa="del" title="Delete page" aria-label="Delete page ' + n + '"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>';
        wrap.appendChild(canvas); wrap.appendChild(overlay);
        var cell = document.createElement('div'); cell.className = 'pe-cell'; cell.appendChild(tools); cell.appendChild(wrap);
        pagesBox.appendChild(cell);
        await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null }).promise;
        var rec = { index: n - 1, cell: cell, el: wrap, overlay: overlay, viewport: vp, ratio: ratio };
        state.pages.push(rec);
        state.order.push(n - 1);
        wirePage(rec);
        wirePageTools(rec, tools);
      }
      await formFields(doc);
      try { doc.destroy(); } catch (_) {}
    }

    function toPdf(rec, x, y) { var p = rec.viewport.convertToPdfPoint(x, y); return { x: p[0], y: p[1] }; }
    function toView(rec, x, y) { var p = rec.viewport.convertToViewportPoint(x, y); return { x: p[0], y: p[1] }; }
    function redraw(index) {
      var rec = state.pages.find(function (r) { return r.index === index; }); if (!rec) return;
      var c = rec.overlay.getContext('2d');
      c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, rec.overlay.width, rec.overlay.height);
      c.setTransform(rec.ratio, 0, 0, rec.ratio, 0, 0);
      state.marks.filter(function (m) { return m.page === index; }).forEach(function (m) { paint(c, rec, m); });
    }
    function paint(c, rec, m) {
      var k = rec.viewport.scale;
      if (m.type === 'text') {
        var a = toView(rec, m.x, m.y); c.fillStyle = m.color; c.font = (m.size * k) + 'px Helvetica, Arial, sans-serif';
        c.textBaseline = 'alphabetic'; c.fillText(m.text, a.x, a.y);
      } else if (m.type === 'highlight') {
        var p1 = toView(rec, m.x, m.y), p2 = toView(rec, m.x + m.w, m.y + m.h);
        c.fillStyle = m.color; c.globalAlpha = 0.35;
        c.fillRect(Math.min(p1.x, p2.x), Math.min(p1.y, p2.y), Math.abs(p2.x - p1.x), Math.abs(p2.y - p1.y));
        c.globalAlpha = 1;
      } else if (m.type === 'ink') {
        c.strokeStyle = m.color; c.lineWidth = m.width * k; c.lineCap = 'round'; c.lineJoin = 'round';
        c.beginPath(); m.points.forEach(function (pt, i) { var v = toView(rec, pt.x, pt.y); i ? c.lineTo(v.x, v.y) : c.moveTo(v.x, v.y); }); c.stroke();
      } else if (m.type === 'image') {
        if (m === state.picked) {
          var a0 = toView(rec, m.x, m.y + m.h), b0 = toView(rec, m.x + m.w, m.y);
          c.save(); c.setLineDash([6, 4]); c.lineWidth = 2; c.strokeStyle = '#00b3ff';
          c.strokeRect(Math.min(a0.x, b0.x) - 3, Math.min(a0.y, b0.y) - 3, Math.abs(b0.x - a0.x) + 6, Math.abs(b0.y - a0.y) + 6); c.restore();
        }
        var img = m._img; if (!img) { img = new Image(); img.onload = function () { redraw(rec.index); }; img.src = m.png; m._img = img; return; }
        var tl = toView(rec, m.x, m.y + m.h), br = toView(rec, m.x + m.w, m.y);
        c.drawImage(img, Math.min(tl.x, br.x), Math.min(tl.y, br.y), Math.abs(br.x - tl.x), Math.abs(br.y - tl.y));
      }
    }

    function wirePage(rec) {
      var el = rec.overlay, drag = null;
      var at = function (e) { var r = el.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
      el.style.touchAction = touchActionFor();
      var tap = null;
      // A TAP, not a press: the finger that starts a scroll must not also drop a text box.
      var tapped = function (e) {
        var v = at(e), p = toPdf(rec, v.x, v.y);
        if (state.tool === 'text') { textBox(rec, v, p); return; }
        if (!state.signature) { signaturePad(); return; }
        // A placed signature under the tap is PICKED (the next tap moves it); a picked one goes here.
        var hit = state.marks.slice().reverse().find(function (m) {
          return m.type === 'image' && m.page === rec.index && p.x >= m.x && p.x <= m.x + m.w && p.y >= m.y && p.y <= m.y + m.h; });
        if (hit && hit !== state.picked) { state.picked = hit; redraw(rec.index); hint(); return; }
        if (state.picked) {
          var m = state.picked, from = m.page; state.picked = null;
          m.page = rec.index; m.x = p.x - m.w / 2; m.y = p.y - m.h / 2; state.dirty = true;
          redraw(from); if (from !== rec.index) redraw(rec.index); hint(); return;
        }
        // The signature's OWN shape: a fixed 160x60 box squashed a short typed name and crushed a long one.
        var aspect = state.signatureAspect || (600 / 220);
        var h = 60 / rec.viewport.scale, w = Math.min(h * aspect, 240 / rec.viewport.scale);
        if (w < h * aspect) h = w / aspect;
        state.marks.push({ page: rec.index, type: 'image', png: state.signature, x: p.x - w / 2, y: p.y - h / 2, w: w, h: h });
        state.dirty = true; redraw(rec.index);
      };
      el.addEventListener('pointerdown', function (e) {
        if (state.tool === 'text' || state.tool === 'sign') {
          tap = { id: e.pointerId, x: e.clientX, y: e.clientY };
          /* A MOUSE press's default action moves focus to the page, which blurred the new text box
           * the instant it appeared (an empty box commits as nothing and removes itself). A finger
           * is left alone, or the browser could not start a scroll from it. */
          if (e.pointerType === 'mouse') e.preventDefault();
          return;
        }
        var v = at(e), p = toPdf(rec, v.x, v.y);
        try { el.setPointerCapture(e.pointerId); } catch (_) {}
        drag = { start: p, points: [p] };
        if (state.tool === 'draw') { drag.mark = { page: rec.index, type: 'ink', color: state.color, width: 2, points: drag.points }; state.marks.push(drag.mark); }
        else if (state.tool === 'highlight') { drag.mark = { page: rec.index, type: 'highlight', color: state.color === '#ff2b6d' ? '#ffe600' : state.color, x: p.x, y: p.y, w: 0, h: 0 }; state.marks.push(drag.mark); }
        e.preventDefault();
      });
      el.addEventListener('pointermove', function (e) {
        if (tap && e.pointerId === tap.id && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 10) tap = null;
        if (!drag) return;
        var v = at(e), p = toPdf(rec, v.x, v.y);
        if (drag.mark.type === 'ink') drag.points.push(p);
        else { drag.mark.x = Math.min(drag.start.x, p.x); drag.mark.y = Math.min(drag.start.y, p.y); drag.mark.w = Math.abs(p.x - drag.start.x); drag.mark.h = Math.abs(p.y - drag.start.y); }
        redraw(rec.index);
      });
      var end = function () {
        if (!drag) return;
        var m = drag.mark; drag = null;
        if ((m.type === 'ink' && m.points.length < 2) || (m.type === 'highlight' && (m.w < 2 || m.h < 2))) state.marks.splice(state.marks.indexOf(m), 1);
        else state.dirty = true;
        redraw(rec.index);
      };
      el.addEventListener('pointerup', function (e) {
        if (tap && e.pointerId === tap.id && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) <= 10) { tap = null; tapped(e); return; }
        tap = null; end();
      });
      // The browser took the finger for a scroll: nothing was tapped.
      el.addEventListener('pointercancel', function () { tap = null; end(); });
    }

    /* A typed note: an input at the point you tapped, committed on Enter or when it loses focus. */
    function textBox(rec, v, p) {
      var input = document.createElement('input');
      input.className = 'pe-textbox'; input.type = 'text'; input.placeholder = 'Type…';
      input.style.left = v.x + 'px'; input.style.top = (v.y - 12) + 'px'; input.style.color = state.color;
      rec.el.appendChild(input); setTimeout(function () { try { input.focus(); } catch (_) {} }, 0);
      var committed = false;
      var commit = function () {
        if (committed) return; committed = true;
        var t = input.value.trim(); input.remove();
        if (!t) return;
        state.marks.push({ page: rec.index, type: 'text', text: t, color: state.color, size: 12, x: p.x, y: p.y - 4 });
        state.dirty = true; redraw(rec.index);
      };
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); commit(); } if (e.key === 'Escape') { committed = true; input.remove(); } });
      input.addEventListener('blur', commit);
    }

    /* Your signature, once -- DRAWN with a finger or the mouse, or TYPED in a handwriting style
     * ("add ability to add Cursive Signature to documents"). Either way it becomes the same thing: a
     * transparent PNG that every tap with Sign places, so saving, undo and the placed size cannot
     * differ between the two. The styles are bundled OFL/Apache script fonts (static/fonts/sig-*),
     * loaded only when the Type tab opens -- the generic `cursive` family is Comic Sans on some systems
     * and absent in the APK's WebView, and a signature must look the same wherever it is made. */
    function signaturePad() {
      var sheet = document.createElement('div'); sheet.className = 'pe-sign-sheet';
      sheet.innerHTML = '<div class="pe-sign-card" role="dialog" aria-label="Your signature"><b>Your signature</b>'
        + '<div class="pe-sign-tabs" role="tablist">'
        + '<button class="btn btn-ghost small pe-sign-tab on" data-mode="draw" role="tab" aria-selected="true">Draw</button>'
        + '<button class="btn btn-ghost small pe-sign-tab" data-mode="type" role="tab" aria-selected="false">Type</button></div>'
        + '<canvas class="pe-sign-pad" width="600" height="220"></canvas>'
        + '<div class="pe-sign-type" hidden>'
        + '<input class="input pe-sign-name" type="text" maxlength="60" autocomplete="name" placeholder="Type your name" aria-label="Your name">'
        + '<div class="pe-sign-styles" role="radiogroup" aria-label="Handwriting style">'
        + SIG_STYLES.map(function (st, i) { return '<button class="pe-sign-style' + (i ? '' : ' on') + '" data-style="' + i + '" role="radio" aria-checked="' + (i ? 'false' : 'true') + '" style="font-family:\'' + st.family + '\',cursive">' + H(st.label) + '</button>'; }).join('')
        + '</div>'
        + '<div class="pe-sign-inks" role="radiogroup" aria-label="Ink">'
        + SIG_INKS.map(function (k, i) { return '<button class="pe-sign-ink' + (i ? '' : ' on') + '" data-ink="' + k[1] + '" role="radio" aria-checked="' + (i ? 'false' : 'true') + '" aria-label="' + k[0] + ' ink" style="background:' + k[1] + '"></button>'; }).join('')
        + '</div></div>'
        + '<div class="pe-sign-acts"><button class="btn btn-ghost small pe-sign-clear">Clear</button>'
        + '<button class="btn btn-ghost small pe-sign-cancel">Cancel</button><button class="btn btn-neon small pe-sign-ok">Use signature</button></div></div>';
      host.appendChild(sheet);
      var pad = sheet.querySelector('.pe-sign-pad'), c = pad.getContext('2d'), down = false, drew = false;
      var mode = 'draw', style = 0, ink = SIG_INKS[0][1];
      var nameIn = sheet.querySelector('.pe-sign-name');
      var pen = function () { c.lineWidth = 3; c.lineCap = 'round'; c.strokeStyle = '#111'; };
      pen();
      var pt = function (e) { var r = pad.getBoundingClientRect(); return [(e.clientX - r.left) * pad.width / r.width, (e.clientY - r.top) * pad.height / r.height]; };
      pad.style.touchAction = 'none';
      pad.onpointerdown = function (e) { if (mode !== 'draw') return; down = true; var p = pt(e); c.beginPath(); c.moveTo(p[0], p[1]); try { pad.setPointerCapture(e.pointerId); } catch (_) {} };
      pad.onpointermove = function (e) { if (!down) return; var p = pt(e); c.lineTo(p[0], p[1]); c.stroke(); drew = true; };
      pad.onpointerup = pad.onpointercancel = function () { down = false; };
      // The typed name, drawn on the same pad as a preview of exactly what will be placed.
      var preview = function () {
        c.clearRect(0, 0, pad.width, pad.height);
        var t = (nameIn.value || '').trim();
        if (!t) { c.fillStyle = '#9aa0a6'; c.font = '20px sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText('Type your name below', pad.width / 2, pad.height / 2); return; }
        var fam = SIG_STYLES[style].family, size = 96;
        c.font = size + 'px "' + fam + '", cursive';
        while (size > 24 && c.measureText(t).width > pad.width - 40) { size -= 4; c.font = size + 'px "' + fam + '", cursive'; }
        c.fillStyle = ink; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(t, pad.width / 2, pad.height / 2 + size * 0.05);
      };
      var setMode = function (m) {
        mode = m;
        sheet.querySelectorAll('.pe-sign-tab').forEach(function (b) { var on = b.dataset.mode === m; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false'); });
        sheet.querySelector('.pe-sign-type').hidden = m !== 'type';
        pad.classList.toggle('typed', m === 'type');
        c.clearRect(0, 0, pad.width, pad.height); drew = false; pen();
        if (m === 'type') {
          preview();
          loadSigFonts().then(preview, preview);           // a style that fails to load falls back, visibly
          setTimeout(function () { try { nameIn.focus(); } catch (_) {} }, 0);
        }
      };
      sheet.querySelectorAll('.pe-sign-tab').forEach(function (b) { b.onclick = function () { setMode(b.dataset.mode); }; });
      nameIn.oninput = preview;
      nameIn.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); sheet.querySelector('.pe-sign-ok').click(); } };
      sheet.querySelectorAll('.pe-sign-style').forEach(function (b) {
        b.onclick = function () {
          style = +b.dataset.style;
          sheet.querySelectorAll('.pe-sign-style').forEach(function (x) { var on = x === b; x.classList.toggle('on', on); x.setAttribute('aria-checked', on ? 'true' : 'false'); });
          preview();
        };
      });
      sheet.querySelectorAll('.pe-sign-ink').forEach(function (b) {
        b.onclick = function () {
          ink = b.dataset.ink;
          sheet.querySelectorAll('.pe-sign-ink').forEach(function (x) { var on = x === b; x.classList.toggle('on', on); x.setAttribute('aria-checked', on ? 'true' : 'false'); });
          preview();
        };
      });
      sheet.querySelector('.pe-sign-clear').onclick = function () {
        if (mode === 'type') { nameIn.value = ''; preview(); return; }
        c.clearRect(0, 0, pad.width, pad.height); drew = false;
      };
      sheet.querySelector('.pe-sign-cancel').onclick = function () { sheet.remove(); if (!state.signature) setTool('text'); };
      sheet.querySelector('.pe-sign-ok').onclick = function () {
        if (mode === 'type') {
          var t = (nameIn.value || '').trim();
          if (!t) { toast('type your name first'); return; }
          var out = typedSignature(t, SIG_STYLES[style].family, ink);
          state.signature = out.png; state.signatureAspect = out.aspect;
        } else {
          if (!drew) { toast('draw your signature first'); return; }
          state.signature = pad.toDataURL('image/png'); state.signatureAspect = pad.width / pad.height;
        }
        sheet.remove(); state.picked = null; hint();
      };
    }

    /* The document's own form: every text field, checkbox and choice, filled in beside the pages. */
    async function formFields() {
      var box = q('.pe-fields');
      try {
        var L = await loadPdfLib();
        var d = await L.PDFDocument.load(original.slice(), { ignoreEncryption: true });
        var fields = d.getForm().getFields();
        if (!fields.length) return;
        box.innerHTML = '<b>Form</b>' + fields.map(function (f, i) {
          var n = f.getName(), t = kindOf(L, f);
          if (t === 'PDFCheckBox') return '<label class="pe-field"><input type="checkbox" data-f="' + H(n) + '"' + (f.isChecked() ? ' checked' : '') + '> ' + H(n) + '</label>';
          if (t === 'PDFDropdown' || t === 'PDFOptionList') {
            var cur = (f.getSelected() || [])[0] || '';
            return '<label class="pe-field">' + H(n) + '<select data-f="' + H(n) + '">' + f.getOptions().map(function (o) { return '<option' + (o === cur ? ' selected' : '') + '>' + H(o) + '</option>'; }).join('') + '</select></label>';
          }
          if (t === 'PDFTextField') return '<label class="pe-field">' + H(n) + '<input class="input" data-f="' + H(n) + '" value="' + H(f.getText() || '') + '"></label>';
          return '';
        }).join('');
        box.classList.remove('hidden');
        box.querySelectorAll('[data-f]').forEach(function (inp) {
          var set = function () { state.fields[inp.dataset.f] = inp.type === 'checkbox' ? inp.checked : inp.value; state.dirty = true; };
          inp.addEventListener('input', set); inp.addEventListener('change', set);
        });
      } catch (_) { /* no form, or one pdf-lib cannot read: marking up still works */ }
    }

    function wirePageTools(rec, tools) {
      tools.querySelectorAll('[data-pa]').forEach(function (b) {
        b.onclick = function () {
          var a = b.dataset.pa, pos = state.order.indexOf(rec.index);
          if (a === 'rot') { state.rotate[rec.index] = ((state.rotate[rec.index] || 0) + 90) % 360; rec.el.style.transform = 'rotate(' + state.rotate[rec.index] + 'deg)'; }
          else if (a === 'del') {
            if (state.order.length <= 1) { toast('a PDF needs at least one page'); return; }
            state.order.splice(pos, 1); rec.cell.classList.add('pe-deleted'); rec.cell.remove();
          } else if (a === 'up' && pos > 0) { state.order.splice(pos, 1); state.order.splice(pos - 1, 0, rec.index); }
          else if (a === 'down' && pos < state.order.length - 1) { state.order.splice(pos, 1); state.order.splice(pos + 1, 0, rec.index); }
          else return;
          state.dirty = true; relayout();
        };
      });
    }
    function relayout() {
      state.order.forEach(function (idx, i) {
        var rec = state.pages.find(function (r) { return r.index === idx; });
        if (rec) { pagesBox.appendChild(rec.cell); var no = rec.cell.querySelector('.pe-page-no'); if (no) no.textContent = 'Page ' + (i + 1); }
      });
    }

    /* ---- writing the result ---- */
    function hex(c) { var n = parseInt(String(c).slice(1), 16); return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255]; }
    async function build() {
      var L = await loadPdfLib();
      var src = await L.PDFDocument.load(original.slice(), { ignoreEncryption: true });
      // Form fields first, in the source document, where they live.
      var names = Object.keys(state.fields);
      if (names.length) {
        var form = src.getForm();
        names.forEach(function (n) {
          try {
            var f = form.getField(n), v = state.fields[n], t = kindOf(L, f);
            if (t === 'PDFCheckBox') v ? f.check() : f.uncheck();
            else if (t === 'PDFDropdown' || t === 'PDFOptionList') f.select(String(v));
            else if (f.setText) f.setText(String(v));
          } catch (_) {}
        });
        try { form.updateFieldAppearances(); } catch (_) {}
      }
      var font = await src.embedFont(L.StandardFonts.Helvetica);
      var images = {};
      for (var i = 0; i < state.marks.length; i++) {
        var m = state.marks[i], page = src.getPage(m.page), col = L.rgb.apply(null, hex(m.color || '#000000'));
        if (m.type === 'text') page.drawText(m.text, { x: m.x, y: m.y, size: m.size, font: font, color: col });
        else if (m.type === 'highlight') page.drawRectangle({ x: m.x, y: m.y, width: m.w, height: m.h, color: col, opacity: 0.35 });
        else if (m.type === 'ink') {
          for (var j = 1; j < m.points.length; j++)
            page.drawLine({ start: m.points[j - 1], end: m.points[j], thickness: m.width, color: col, lineCap: L.LineCapStyle.Round });
        } else if (m.type === 'image') {
          var img = images[m.png] || (images[m.png] = await src.embedPng(m.png));
          page.drawImage(img, { x: m.x, y: m.y, width: m.w, height: m.h });
        }
      }
      Object.keys(state.rotate).forEach(function (k) {
        var p = src.getPage(Number(k)); p.setRotation(L.degrees(((p.getRotation().angle || 0) + state.rotate[k]) % 360));
      });
      var identity = state.order.length === src.getPageCount() && state.order.every(function (v, i) { return v === i; });
      /* REARRANGED IN PLACE, not copied into a new document: a fresh document built with copyPages
       * leaves the original's form (AcroForm), outline and metadata behind, so deleting one page
       * quietly threw away every field anybody had filled in. */
      if (!identity) {
        var keep = state.order.map(function (i) { return src.getPage(i); });
        for (var r = src.getPageCount() - 1; r >= 0; r--) src.removePage(r);
        keep.forEach(function (p) { src.addPage(p); });
      }
      return new Blob([await src.save()], { type: 'application/pdf' });
    }
    async function save(inPlace) {
      var btns = host.querySelectorAll('.pe-save,.pe-copy'); btns.forEach(function (b) { b.disabled = true; });
      try {
        var blob = await build();
        if (inPlace && saveBack) { await saveBack(blob); toast('PDF saved'); finish(blob); return; }
        var copyName = name.replace(/\.pdf$/i, '') + ' (edited).pdf';
        if (!PC().saveBlobAs) throw new Error('this build cannot save a file');
        await PC().saveBlobAs(blob, copyName); finish(blob);
      } catch (e) {
        toast('could not save the PDF: ' + ((e && e.message) || e));
        btns.forEach(function (b) { b.disabled = false; });
      }
    }

    setTool('text');
    render().catch(function (e) { pagesBox.innerHTML = '<div class="empty">This PDF could not be opened for editing: ' + H((e && e.message) || e) + '</div>'; });
    return { done: done, state: state, build: build };
  }

  root.PCPdfEdit = { edit: edit, loadPdfLib: loadPdfLib };
})(typeof window !== 'undefined' ? window : globalThis);
