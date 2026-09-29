pdf-lib 1.17.1 — Andrew Dillon, MIT (see LICENSE.md). `dist/pdf-lib.min.js` from the npm package,
byte-identical (sha256 0f9a5cad07941f0826586c94e089d89b918c46e5c17cf2d5a3c6f666e3bc694f).

Vendored for the PDF editor in Preview (static/js/client/pdfedit.js), which WRITES the edited
document in the page — pdf.js (../pdfjs) only reads. The UMD build, so it loads from a plain
<script> on both shells without a bundler, the same reason pdf.js is the 3.x legacy build.
Loaded only when somebody presses Edit on a PDF; never precached.
