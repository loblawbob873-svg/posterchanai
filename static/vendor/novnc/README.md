# noVNC (vendored)

The VM console in Virtual Machines (`static/js/client/vmconsole.js`) uses noVNC's RFB client.

- **Version:** noVNC **1.5.0**
- **Source:** `https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz`
- **Tarball sha256:** `6a73e41f98388a5348b7902f54b02d177cb73b7e5eb0a7a0dcf688cc2c79b42a`
- **Tree digest** (`cd static/vendor/novnc && find core vendor -type f | sort | xargs sha256sum | sha256sum`):
  `c995c15f6117c7eef2835c0bf4e685b798be29235d0e8e2a16f2855efac7092a`
- **Vendored:** `core/` and `vendor/pako/` verbatim (no edits), plus `LICENSE.txt`, `LICENSE.MPL-2.0`
  and `AUTHORS`. The `app/` UI, tests, docs and utilities are NOT included — the client draws its own
  toolbar and only needs the RFB library.
- **Licence:** noVNC core is MPL-2.0; pako is MIT (see `vendor/pako/LICENSE`).

It is loaded as native ES modules on demand (`import('/static/vendor/novnc/core/rfb.js')`), so
nothing here is in the page's boot payload. The desktop and Android bundles copy the whole
`static/vendor` tree, so it ships in both.

To upgrade: download the new release tarball, replace `core/` and `vendor/pako/` wholesale, update the
version, both digests above, and run `tests/client/test_vms_full_app.py` (it drives a real RFB
handshake through this code).
