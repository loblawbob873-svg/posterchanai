/* PREVIEW — look at a picture, a video or a PDF without leaving the app.
 *
 * Files could open a document in Office and a text file in Code, and for everything else offered
 * "open in a new tab", which on the encrypted drive means decrypting to a blob URL and handing it to
 * the browser — leaving the app, losing the folder you were in, and on the APK doing nothing useful
 * at all. A picture is the most common thing on anybody's drive and it had no viewer.
 *
 * PDFs use the vendored pdf.js renderer. Android WebView has no built-in PDF viewer, and relying on
 * an iframe there produced an apology instead of a preview. The library is loaded only when a PDF
 * is opened, and receives the already-decrypted bytes directly.
 *
 * NO SERVER. It is handed BYTES that the caller already has - the drive decrypts, a synced folder
 * assembles chunks, This Computer reads from disk - so it works on a standalone build with no
 * instance, and an encrypted file is never fetched over the network to be looked at.
 */
(function (root) {
  'use strict';

  var PC = function () { return root.__PC || {}; };
  var H = function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };
  var toast = function (m) { try { (PC().toast || function () {})(m); } catch (_) {} };

  /* WHAT THIS CAN SHOW. By extension AND by mime, because the drive stores files uploaded from other
   * clients that may carry one and not the other - the same reason _codeable takes both. */
  var IMG_EXT = /\.(png|jpe?g|jfif|gif|webp|avif|bmp|ico|svg|heic|heif)$/i;
  var VID_EXT = /\.(mp4|m4v|mov|webm|mkv|ogv|avi|3gp)$/i;
  var AUD_EXT = /\.(mp3|m4a|aac|ogg|oga|opus|wav|flac)$/i;
  var PDF_EXT = /\.pdf$/i;

  function isImage(name, mime) {
    return IMG_EXT.test(name || '') || /^image\//i.test(String(mime || ''));
  }
  function isVideo(name, mime) {
    return VID_EXT.test(name || '') || /^video\//i.test(String(mime || ''));
  }
  function isAudio(name, mime) {
    return AUD_EXT.test(name || '') || /^audio\//i.test(String(mime || ''));
  }
  function isPdf(name, mime) {
    return PDF_EXT.test(name || '') || /^application\/pdf/i.test(String(mime || ''));
  }
  function handles(name, mime) {
    return isImage(name, mime) || isVideo(name, mime) || isAudio(name, mime) || isPdf(name, mime);
  }
  function kindOf(name, mime) {
    return isImage(name, mime) ? 'image'
         : isVideo(name, mime) ? 'video'
         : isAudio(name, mime) ? 'audio'
         : isPdf(name, mime) ? 'pdf' : '';
  }

  /* Blossom servers are allowed to return application/octet-stream (and older uploads often do).
   * A blob URL keeps that type, so Chromium hands an otherwise valid MP4 to <video> as generic
   * binary and the result is a black rectangle with controls that never start. Infer only the
   * media types we actually preview; the filename remains the authority for these generic blobs. */
  function inferredType(name, kind) {
    var ext = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
    ext = ext ? ext[1] : '';
    if (kind === 'video') return ({ mp4:'video/mp4', m4v:'video/mp4', mov:'video/quicktime',
      webm:'video/webm', ogv:'video/ogg', '3gp':'video/3gpp', mkv:'video/x-matroska',
      avi:'video/x-msvideo' })[ext] || 'video/mp4';
    if (kind === 'audio') return ({ mp3:'audio/mpeg', m4a:'audio/mp4', aac:'audio/aac',
      ogg:'audio/ogg', oga:'audio/ogg', opus:'audio/ogg', wav:'audio/wav', flac:'audio/flac' })[ext] || '';
    if (kind === 'image') return ({ jpg:'image/jpeg', jpeg:'image/jpeg', png:'image/png',
      gif:'image/gif', webp:'image/webp', avif:'image/avif', svg:'image/svg+xml' })[ext] || '';
    return kind === 'pdf' ? 'application/pdf' : '';
  }

  var _pdfjs = null;
  function loadPdfJs() {
    if (root.pdfjsLib) return Promise.resolve(root.pdfjsLib);
    if (_pdfjs) return _pdfjs;
    _pdfjs = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      var finished = false;
      var fail = function (e) {
        if (finished) return;
        finished = true; clearTimeout(timer); _pdfjs = null;
        try { s.remove(); } catch (_) {}
        reject(e);
      };
      var timer = setTimeout(function () { fail(new Error('PDF renderer timed out')); }, 10000);
      s.src = '/static/vendor/pdfjs/pdf.min.js';
      s.onload = function () {
        if (finished) return;
        if (!root.pdfjsLib) return fail(new Error('PDF renderer did not start'));
        finished = true; clearTimeout(timer);
        root.pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.js';
        resolve(root.pdfjsLib);
      };
      s.onerror = function () { fail(new Error('PDF renderer is unavailable')); };
      (document.head || document.documentElement).appendChild(s);
    });
    return _pdfjs;
  }

  async function openElsewhere(blob, name) {
    name=name||'document.pdf';
    /* Android WebView has no PDF activity inside it. Ask the platform to ACTION_VIEW the exact
     * bytes first; OpenFile keeps them in private cache and grants only the chosen viewer read
     * access. No viewer/older APK/cancel falls through to the useful Save-or-share sheet. */
    try {
      var cap=PC().capPlugin&&PC().capPlugin('OpenFile','open');
      if(cap&&cap.open){
        var bytes=new Uint8Array(await blob.arrayBuffer()),binary='';
        for(var at=0;at<bytes.length;at+=0x8000)binary+=String.fromCharCode.apply(null,bytes.subarray(at,at+0x8000));
        var opened=await cap.open({data:btoa(binary),mime:blob.type||'application/pdf',name:name});
        if(opened&&opened.ok)return 'opened';
      }
    } catch (_) { /* no viewer or native failure: retain Save/share below */ }
    var save = PC().saveBlobAs;
    if (!save) { toast('cannot open this PDF on this build'); return; }
    return Promise.resolve(save(blob, name)).catch(function (e) {
      toast('could not open that PDF: ' + ((e && e.message) || e));
    });
  }

  async function renderPdf(host, blob, name) {
    var box = host.querySelector('.pv-pdf-pages');
    if (!box) return;
    try {
      var lib = await loadPdfJs();
      var pdf = await lib.getDocument({ data: await blob.arrayBuffer() }).promise;
      box.innerHTML = '';
      for (var n = 1; n <= pdf.numPages; n++) {
        var page = await pdf.getPage(n);
        var base = page.getViewport({ scale: 1 });
        var available = Math.max(280, Math.min(1200, (box.clientWidth || 800) - 20));
        var viewport = page.getViewport({ scale: Math.min(2.25, available / base.width) });
        var canvas = document.createElement('canvas');
        canvas.className = 'pv-pdf-page'; canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height); canvas.setAttribute('aria-label', 'Page ' + n);
        box.appendChild(canvas);
        await page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise;
      }
    } catch (e) {
      box.innerHTML = '<div class="empty pv-pdf-fallback">Could not render this PDF.<br>'
        + H((e && e.message) || e)
        + '<br><button class="btn primary pv-pdf-native">Open or save in another app</button></div>';
      var native = box.querySelector('.pv-pdf-native');
      if (native) native.onclick = function () { openElsewhere(blob, name); };
    }
  }

  var _open = null;          // the one live preview: { url, close }

  function isOpen() { return !!_open; }
  function close() { if (_open) { try { _open.close(); } catch (_) {} } }

  function bytesOf(src) {
    if (!src) return null;
    if (src instanceof Blob) return src;
    try { return new Blob([src]); } catch (_) { return null; }
  }

  function fmtSize(n) {
    n = Number(n || 0);
    if (!n) return '';
    var u = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : String(n)) + ' ' + u[i];
  }

  function bodyHTML(name, mime, size, kind) {
    var head = '<div class="pv-bar">'
      + '<span class="pv-name" title="' + H(name) + '">' + H(name) + '</span>'
      + '<span class="pv-size muted small">' + H(fmtSize(size)) + '</span>'
      + '<span class="pv-acts">'
      + (kind === 'image' ? '<button class="btn btn-ghost small pv-zoom">Actual size</button>'
                            + '<button class="btn btn-ghost small pv-rot" title="Rotate">&#8635;</button>' : '')
      + '<button class="btn btn-ghost small pv-dl">Download</button>'
      + '<button class="btn btn-ghost small pv-x" aria-label="Close">&#10005;</button>'
      + '</span></div>';
    var body;
    if (kind === 'image') {
      body = '<div class="pv-body pv-img-wrap"><img class="pv-img" alt="' + H(name) + '"></div>';
    } else if (kind === 'video') {
      /* `playsinline` or iOS takes the video full-screen the moment it plays, which throws away the
       * window this was opened in. `preload="metadata"` so the poster frame and the duration are
       * there before anybody presses play, without pulling the whole file. */
      body = '<div class="pv-body pv-av-wrap"><video class="pv-vid" controls playsinline '
           + 'preload="metadata"></video><div class="pv-media-state">Loading video…</div></div>';
    } else if (kind === 'audio') {
      body = '<div class="pv-body pv-av-wrap"><audio class="pv-aud" controls preload="metadata">'
           + '</audio></div>';
    } else if (kind === 'pdf') {
      body = '<div class="pv-body pv-pdf-body"><div class="pv-pdf-pages" role="document" '
        + 'aria-label="' + H(name) + '"><div class="spinner"></div></div></div>';
    } else {
      body = '<div class="pv-body pv-nope"><div class="empty">Nothing here can show that file.</div></div>';
    }
    return head + body;
  }

  /* Mount, wire, and hand back a `close`. The two hosts differ only in where this goes and how it is
   * taken away again - exactly the split the office editor uses. */
  function mount(host, name, mime, size, kind, url, shut, blob) {
    host.innerHTML = bodyHTML(name, mime, size, kind);
    var q = function (s) { return host.querySelector(s); };

    if (kind === 'image') {
      var img = q('.pv-img'), rot = 0, actual = false;
      img.src = url;
      /* A picture that fails to decode must SAY so. An <img> whose src is a blob the browser cannot
       * read fires `error` and then draws nothing at all, which is indistinguishable from a slow
       * load and is what makes a viewer feel broken. */
      img.onerror = function () {
        var b = q('.pv-body');
        if (b) b.innerHTML = '<div class="empty">That file is not an image this browser can decode.</div>';
      };
      var paint = function () {
        img.style.transform = 'rotate(' + rot + 'deg)';
        img.classList.toggle('pv-actual', actual);
        var z = q('.pv-zoom'); if (z) z.textContent = actual ? 'Fit' : 'Actual size';
      };
      var zb = q('.pv-zoom'); if (zb) zb.onclick = function () { actual = !actual; paint(); };
      var rb = q('.pv-rot'); if (rb) rb.onclick = function () { rot = (rot + 90) % 360; paint(); };
      // Clicking the picture is the same as the zoom button - what every viewer does.
      img.onclick = function () { actual = !actual; paint(); };
      paint();
    } else if (kind === 'video' || kind === 'audio') {
      var av = q(kind === 'video' ? '.pv-vid' : '.pv-aud');
      av.src = url;
      var state = q('.pv-media-state');
      av.onloadedmetadata = function () { if (state) state.remove(); };
      /* A CODEC THIS BROWSER CANNOT PLAY MUST SAY SO. A <video> handed a container it cannot decode
       * (an .mkv or an .avi almost anywhere, HEVC in Firefox) fires `error` and then shows a black
       * rectangle with dead controls, which reads as a broken player rather than an unsupported
       * file - and on the drive that is a file somebody may have only one copy of. */
      av.onerror = function () {
        var b = q('.pv-body');
        if (b) b.innerHTML = '<div class="empty">This browser cannot play that format.'
          + '<br>Download it and open it in a real player.</div>';
      };
      /* Explicit load matters in Android WebView and after a desktop document window is recycled:
       * assigning a blob URL alone can leave the old, empty media resource selected. */
      try { av.load(); } catch (_) {}
    } else if (kind === 'pdf') {
      renderPdf(host, blob, name);
    }

    var dl = q('.pv-dl');
    if (dl) dl.onclick = function () {
      /* saveBlobAs, NEVER a bare <a download>: the APK's WebView ignores a programmatic download and
       * the desktop's app:// origin refuses one, so the button would silently do nothing on two of
       * the three platforms this ships to. */
      var save = PC().saveBlobAs;
      if (!save) { toast('cannot save on this build'); return; }
      fetch(url).then(function (r) { return r.blob(); })
        .then(function (b) { return save(b, name || 'file'); })
        .catch(function (e) { toast('could not save: ' + ((e && e.message) || e)); });
    };
    var x = q('.pv-x'); if (x) x.onclick = shut;
  }

  /**
   * Show a file. `src` is a Blob (or anything Blob() accepts); nothing is fetched.
   * @returns {boolean} false when this file is not something the viewer handles.
   */
  function open(file) {
    file = file || {};
    var name = file.name || 'file';
    var mime = file.mime || (file.blob && file.blob.type) || '';
    var blob = bytesOf(file.blob || file.bytes || file.src);
    if (!blob) { toast('there are no bytes to show'); return false; }
    if (!handles(name, mime)) return false;

    var kind = kindOf(name, mime);
    /* A PDF must be handed to the viewer as a PDF. A blob rebuilt from an ArrayBuffer has an EMPTY
     * type, and an <iframe> at a blob: url with no type is downloaded or ignored rather than
     * rendered - the picture works either way, which is exactly how this would ship half-broken. */
    var generic = !blob.type || /^application\/(octet-stream|binary)$/i.test(blob.type);
    if (generic) {
      var t = (!/^application\/(octet-stream|binary)$/i.test(mime) && mime) || inferredType(name, kind);
      if (t) { try { blob = new Blob([blob], { type: t }); } catch (_) {} }
    }

    close();                                  // one at a time; the previous URL is revoked by its own close
    var url = URL.createObjectURL(blob);
    var key = 'pv:' + Math.random().toString(36).slice(2, 9);
    var shut = function () {}, transferring = false;
    var done = function () {
      /* STOP THE SOUND. A <video> detached from the document keeps playing in Chromium until it is
       * garbage collected, so closing the window left a voice coming out of nowhere. */
      try {
        var av = document.querySelector('.pv-host .pv-vid, .pv-host .pv-aud');
        if (av) { av.pause(); av.removeAttribute('src'); av.load(); }
      } catch (_) {}
      if (_open && _open.key === key) _open = null;
      /* A monitor handoff transfers this URL to another renderer, which fetches the bytes and owns
       * a fresh URL. Revoking here would race that fetch and produce a perfectly moved blank frame. */
      if (!transferring) try { URL.revokeObjectURL(url); } catch (_) {}
      root.removeEventListener('keydown', onKey, true);
    };
    var onKey = function (e) { if (e.key === 'Escape') { e.stopPropagation(); shut(); } };

    /* ON THE WINDOWED DESKTOP IT IS A WINDOW, through the same openDoc the office editor and the
     * webxdc mini apps use, so it minimises, maximises and moves between monitors like everything
     * else. `noFeed` because it owns its window: without it, clicking another window pulls the
     * timeline into this one and the repaint destroys the frame. */
    if (root.PCOS && root.PCOS.isOn && root.PCOS.isOn() && root.PCOS.openDoc) {
      var w = root.PCOS.openDoc(key, name, 'i-eye', function () {}, true);
      if (w && root.PCOS.documentWindow) root.PCOS.documentWindow(w);
      var host = w && (w.slot || w.body);
      if (host) {
        host.classList.add('pv-host', 'pv-win');
        shut = function () { try { root.PCOS.closeDoc(key); } catch (_) {} done(); };
        if (w) w.onClose = done;
        if (w) {
          w.handoffState = function () { transferring = true; return {
            preview: true, name: String(name || ''), mime: String(mime || ''), url: String(url || '')
          }; };
          w.handoffCancel = function () { transferring = false; };
        }
        mount(host, name, mime, blob.size, kind, url, shut, blob);
        _open = { key: key, close: shut };
        root.addEventListener('keydown', onKey, true);
        return true;
      }
      try { root.PCOS.closeDoc(key); } catch (_) {}   // no slot: fall through rather than leave an empty window
    }

    // Everywhere else: a full-screen sheet, the shape webxdc already uses on a phone.
    var sheet = document.createElement('div');
    sheet.className = 'pv-sheet pv-host';
    document.body.appendChild(sheet);
    shut = function () { try { sheet.remove(); } catch (_) {} done(); };
    mount(sheet, name, mime, blob.size, kind, url, shut, blob);
    _open = { key: key, close: shut };
    root.addEventListener('keydown', onKey, true);
    return true;
  }

  async function acceptHandoff(state) {
    var s = state && typeof state === 'object' ? state : {};
    if (!s.preview || !/^blob:/i.test(String(s.url || ''))) return false;
    var response = await fetch(String(s.url)), blob = await response.blob();
    try { URL.revokeObjectURL(String(s.url)); } catch (_) {}
    return open({ name:String(s.name || 'Preview'), mime:String(s.mime || blob.type || ''), blob:blob });
  }

  root.PCPreview = { open: open, acceptHandoff: acceptHandoff, handles: handles, kindOf: kindOf,
                     isImage: isImage, isVideo: isVideo, isAudio: isAudio, isPdf: isPdf,
                     loadPdfJs: loadPdfJs, isOpen: isOpen, close: close };
})(window);
