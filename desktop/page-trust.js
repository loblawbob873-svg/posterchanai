/* The preload is attached to both the bundled app and three small local helper pages.  `file:` is
 * not a trust boundary: a downloaded HTML file has the same scheme as shell.html.  Reduce that
 * capability to the exact files shipped beside this module, after URL decoding and path
 * normalisation, so encoded traversal and lookalike names cannot acquire the native bridge. */
'use strict';

const path = require('path');
const { fileURLToPath } = require('url');

const LOCAL_PAGES = ['boot.html', 'shell.html', 'picker.html'];

function samePath(a, b) {
  const left = path.resolve(a), right = path.resolve(b);
  return process.platform === 'win32'
    ? left.toLowerCase() === right.toLowerCase()
    : left === right;
}

function isTrustedPage(raw, localDir) {
  let u;
  try { u = new URL(String(raw || '')); } catch (_) { return false; }
  if (u.protocol === 'app:') return u.hostname === 'posterchan' && !u.port && !u.username && !u.password;
  if (u.protocol !== 'file:' || u.hostname) return false;
  let candidate;
  try { candidate = fileURLToPath(u); } catch (_) { return false; }
  const dir = path.resolve(localDir || __dirname);
  return LOCAL_PAGES.some((name) => samePath(candidate, path.join(dir, name)));
}

module.exports = { LOCAL_PAGES, isTrustedPage };
