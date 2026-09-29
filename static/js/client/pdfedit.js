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
      + '<span class="pe-gap"></span>'
      + '<button class="btn btn-ghost small pe-cancel">Cancel</button>'
      + (saveBack ? '<button class="btn btn-neon small pe-save">Save</button>' : '')
      + '<button class="btn ' + (saveBack ? 'btn-ghost' : 'btn-neon') + ' small pe-copy">Save a copy</button>'
      + '</div>'
      + '<div class="pe-fields hidden" aria-label="Form fields"></div>'
      + '<div class="pe-pages" role="document" aria-label="' + H(name) + '"><div class="spinner"></div></div>'
      + '</div>';
    var q = function (s) { return host.querySelector(s); };
    var pagesBox = q('.pe-pages');

    function setTool(t) {
      state.tool = t;
      host.querySelectorAll('.pe-tool').forEach(function (b) {
        var on = b.dataset.tool === t; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      if (t === 'sign' && !state.signature) signaturePad();
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
        var img = m._img; if (!img) { img = new Image(); img.onload = function () { redraw(rec.index); }; img.src = m.png; m._img = img; return; }
        var tl = toView(rec, m.x, m.y + m.h), br = toView(rec, m.x + m.w, m.y);
        c.drawImage(img, Math.min(tl.x, br.x), Math.min(tl.y, br.y), Math.abs(br.x - tl.x), Math.abs(br.y - tl.y));
      }
    }

    function wirePage(rec) {
      var el = rec.overlay, drag = null;
      var at = function (e) { var r = el.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
      el.style.touchAction = 'none';
      el.addEventListener('pointerdown', function (e) {
        var v = at(e), p = toPdf(rec, v.x, v.y);
        if (state.tool === 'text') {
          /* The press's own default action moves focus to the page, which blurred the new box the
           * instant it appeared — and an empty box commits as nothing and removes itself. */
          e.preventDefault();
          textBox(rec, v, p); return;
        }
        if (state.tool === 'sign') {
          if (!state.signature) { signaturePad(); return; }
          var w = 160 / rec.viewport.scale, h = 60 / rec.viewport.scale;
          state.marks.push({ page: rec.index, type: 'image', png: state.signature, x: p.x - w / 2, y: p.y - h / 2, w: w, h: h });
          state.dirty = true; redraw(rec.index); return;
        }
        try { el.setPointerCapture(e.pointerId); } catch (_) {}
        drag = { start: p, points: [p] };
        if (state.tool === 'draw') { drag.mark = { page: rec.index, type: 'ink', color: state.color, width: 2, points: drag.points }; state.marks.push(drag.mark); }
        else if (state.tool === 'highlight') { drag.mark = { page: rec.index, type: 'highlight', color: state.color === '#ff2b6d' ? '#ffe600' : state.color, x: p.x, y: p.y, w: 0, h: 0 }; state.marks.push(drag.mark); }
        e.preventDefault();
      });
      el.addEventListener('pointermove', function (e) {
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
      el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
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

    /* Draw your signature once; every tap with Sign then places it. */
    function signaturePad() {
      var sheet = document.createElement('div'); sheet.className = 'pe-sign-sheet';
      sheet.innerHTML = '<div class="pe-sign-card" role="dialog" aria-label="Draw your signature"><b>Draw your signature</b>'
        + '<canvas class="pe-sign-pad" width="600" height="220"></canvas>'
        + '<div class="pe-sign-acts"><button class="btn btn-ghost small pe-sign-clear">Clear</button>'
        + '<button class="btn btn-ghost small pe-sign-cancel">Cancel</button><button class="btn btn-neon small pe-sign-ok">Use signature</button></div></div>';
      host.appendChild(sheet);
      var pad = sheet.querySelector('.pe-sign-pad'), c = pad.getContext('2d'), down = false, drew = false;
      c.lineWidth = 3; c.lineCap = 'round'; c.strokeStyle = '#111';
      var pt = function (e) { var r = pad.getBoundingClientRect(); return [(e.clientX - r.left) * pad.width / r.width, (e.clientY - r.top) * pad.height / r.height]; };
      pad.style.touchAction = 'none';
      pad.onpointerdown = function (e) { down = true; var p = pt(e); c.beginPath(); c.moveTo(p[0], p[1]); try { pad.setPointerCapture(e.pointerId); } catch (_) {} };
      pad.onpointermove = function (e) { if (!down) return; var p = pt(e); c.lineTo(p[0], p[1]); c.stroke(); drew = true; };
      pad.onpointerup = pad.onpointercancel = function () { down = false; };
      sheet.querySelector('.pe-sign-clear').onclick = function () { c.clearRect(0, 0, pad.width, pad.height); drew = false; };
      sheet.querySelector('.pe-sign-cancel').onclick = function () { sheet.remove(); if (!state.signature) setTool('text'); };
      sheet.querySelector('.pe-sign-ok').onclick = function () {
        if (!drew) { toast('draw your signature first'); return; }
        state.signature = pad.toDataURL('image/png'); sheet.remove(); toast('tap the page where the signature goes');
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
