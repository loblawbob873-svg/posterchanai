/* Folder sync — when to sweep. The policy, and nothing else.
 *
 * THIS FILE USED TO BE THE EXECUTOR. It was replaced by the engine (syncstate.js decides what should
 * happen) and syncexec.js (which does it), because the shape underneath it could not be made safe:
 * one shared document that every device read, edited and wrote back is last-writer-wins on the
 * record of whether your files exist, and no amount of merging, re-reading or server-side guarding
 * changed that. Two devices syncing at once lost each other's work, and a document that failed to
 * load read as "every file you have was deleted".
 *
 * What survives here is the one piece that was never the problem: the question of whether a sweep
 * should run right now at all, which is a battery and network policy and lives in foldersync.js.
 */
(function(root){
  'use strict';
  const S = root.PCFolderSync || (typeof require === 'function' ? require('./foldersync.js') : null);
  if(!S) throw new Error('syncrun.js needs foldersync.js');

  /* Should a sweep run at all right now — the battery policy, plus the folder's own state. Kept as
   * one function so a caller has one thing to poll, and so the reason is a sentence the UI can show
   * ("waiting until you plug in") instead of a silent no. */
  function due(state, prefs){ return S.shouldSync(state, prefs); }

  const API = { due };
  root.PCSyncRun = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
