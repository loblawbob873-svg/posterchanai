pdf.js 3.11.174 — Mozilla, Apache-2.0 (see LICENSE).

Vendored, not fetched: a strict CSP and the two shells (the APK's WebView, the desktop's `app://`
origin) rule out a CDN, and `mobile/build-www.sh` copies `static/vendor` wholesale so the APK gets
this for free.

WHY THE 3.x LEGACY BUILD and not the current one: 4.x/5.x/6.x ship ES modules only. The UMD
`legacy/build/pdf.min.js` is the last shape that loads from a plain <script> on both shells without
a bundler step, and this repo deliberately has no bundler for the client.

WHY IT IS IN THE CLIENT AT ALL. The obvious alternative is to render pages server-side — PyMuPDF is
already a dependency for the media tools. It cannot be used here: drive files are ENCRYPTED and the
server has no key, so a server-side renderer would mean uploading the decrypted bytes back, which
gives away the one property the encrypted drive exists for.

Files are loaded on demand (see preview.js `_pdfjs`), never precached: the worker alone is 1.1MB and
most sessions open no PDF at all.
