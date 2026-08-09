/* The exclusion matcher, shared by the desktop walker and the sync engine.
 *
 * A re-export rather than a second implementation, on purpose: two copies of "is this path excluded"
 * drift, and the way they drift is that the scanner and the planner disagree about a folder — which
 * shows up as files being deleted from other devices when someone adds an exclusion. foldersync.js
 * is the one definition; this pulls it into the Electron main process, which cannot use the client's
 * globals.
 *
 * TWO PATHS, because the file lives somewhere different once packaged. In the repo it is
 * `../static/js/client/`; in an installed app the only things inside the asar are `desktop/*` and
 * the bundled `www/**` (see package.json "files"), so the same module arrives as
 * `./www/static/js/client/`. Requiring only the repo path works perfectly on this machine and throws
 * MODULE_NOT_FOUND on every user's install — the exact class of bug the desktop build has hit before,
 * which is why check_desktop_standalone.py exists.
 *
 * The REPO path is tried first, and that order is load-bearing in the other direction: desktop/www is
 * a BUILD ARTIFACT, rebuilt by build-www.sh, so in a checkout it is a snapshot of whatever the source
 * looked like at the last build. Preferring it meant dev silently ran a stale copy of this module —
 * which is precisely how this was found, one edit after adding excluder(). Packaged, the repo path
 * does not exist and the bundle is the only candidate, so the fallback still does its job.
 */
'use strict';
const path = require('path');

function load(){
  const tries = [
    path.join(__dirname, '..', 'static', 'js', 'client', 'foldersync.js'),    // repo checkout
    path.join(__dirname, 'www', 'static', 'js', 'client', 'foldersync.js'),   // packaged
  ];
  for(const p of tries){
    try{ return require(p); }catch(e){ if(e && e.code !== 'MODULE_NOT_FOUND') throw e; }
  }
  // Never take the app down over an exclusion list. Syncing everything is wrong but recoverable;
  // failing to start is not, and the folder-sync UI can report the degraded state.
  return null;
}

const S = load();
module.exports = {
  available: !!S,
  excluder: S ? S.excluder : () => (() => false),
};
