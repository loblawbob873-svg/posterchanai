/* Folder imports register EXACTLY the folders their files are filed under, encrypted ones included,
 * and an encrypted or restored file keeps its type icon. Runs the SHIPPED FilesIdx.addFolder /
 * isEncFolder / _norm, uploadFilesSeq, extOfBlob and the icon helpers under node. */
'use strict';
const { clientSource, clientSourceAt, installStateGlobals } = require('./client_source.cjs');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const app = clientSourceAt(process.env.PC_INSTALLED_APP_JS ||
  path.resolve(__dirname, '../../static/js/client/app.js'));

function fn(head) {
  const i = app.indexOf(head), begin = app.indexOf('{', i);
  if (i < 0 || begin < 0) throw new Error('missing ' + head);
  let depth = 0, quote = '', escaped = false;
  for (let p = begin; p < app.length; p++) {
    const c = app[p];
    if (quote) { if (escaped) escaped = false; else if (c === '\\') escaped = true; else if (c === quote) quote = ''; continue; }
    if (c === "'" || c === '"' || c === '`') { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return app.slice(i, p + 1);
  }
  throw new Error('unterminated ' + head);
}
function constLine(head) {
  const i = app.indexOf(head); if (i < 0) throw new Error('missing ' + head);
  return app.slice(i, app.indexOf('};', i) + 2);
}
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };

const ctx = { console, Set, Map, Promise, Math, Date, JSON, String, Object, Array,
  toast: () => {}, _uploadBadge: () => {}, $: () => null, enc: s => String(s),
  mediaServer: () => 'https://blossom.test', setTimeout: f => { f(); return 1; },
  VIEW: 'blossom', renderBlossom: () => {}, _signUploadBatch: async () => null, _blossomDenied: () => false,
  requestBlossomAccess: () => {}, uploadMusicTrack: async () => {}, _refreshBlobHave: async () => {},
  _looksAudio: () => false, _musicHasSrc: () => false, Relay: {} };
vm.createContext(installStateGlobals(ctx) && ctx);
vm.runInContext(`
let _filesFolder=null,_uploadCancel=false,_uploading=0,_uploadBatchAuth=null,_filesGridList=null,_blobHave=new Set(),_blobSizes=new Map();
${constLine('const _MIME_EXT={')}
const FilesIdx = {
  data: {folders:['Music'], files:{}, encFolders:['Private']}, _pullDone:true, pushes:0,
  ${fn('    _norm(){').trim()},
  push(){ this.pushes++; }, beginBatch(){}, async endBatch(){ return true; },
  ${fn('    folders(){').trim()},
  ${fn('    isEncFolder(name){').trim()},
  ${fn('    addFolder(name, enc, exact){').trim()},
  setFile(sha, m){ this.data.files[sha]=m; },
};
let n=0;
async function uploadBlob(f, o){ const h=(++n).toString(16).padStart(64,'0'); o.hashOut.sha=h; return 'https://blossom.test/'+h; }
async function uploadEncFile(f, folder){ const h=(++n).toString(16).padStart(64,'e'); FilesIdx.setFile(h,{name:f.name,folder,enc:true}); return h; }
function _shaFromUrl(u){ const m=String(u).match(/([0-9a-f]{64})/); return m?m[1]:''; }
${fn('function _uploadTargetFolder(')}
${fn('function _rememberUploadedBlob(')}
${fn('async function uploadFilesSeq(')}
${fn('function extOfBlob(')}
${fn('function _fxFileGlyph(')}
${fn('function _fxFolderIcon(')}
${fn('function _fxEncIcon(')}
${fn('function _fxIcon(')}
globalThis.T = { FilesIdx, uploadFilesSeq, extOfBlob, _fxFolderIcon, _fxEncIcon, _fxIcon, setFolder: f => { _filesFolder = f; } };
`, ctx);

(async () => {
  const { FilesIdx, uploadFilesSeq, extOfBlob, _fxFolderIcon, _fxEncIcon } = ctx.T;

  // A typed name is still capped; an import PATH is registered exactly.
  check(FilesIdx.addFolder('x'.repeat(60)) && FilesIdx.folders().includes('x'.repeat(40)), 'typed name capped at 40');
  const longPath = 'Photos/2024/Summer trip to the coast/Day 3 morning at the beach';
  check(FilesIdx.addFolder(longPath, false, true) && FilesIdx.folders().includes(longPath), 'exact path registered verbatim');

  // A folder import from Home: every file's folder must be a REGISTERED folder, long names included.
  const deep = 'Holiday photos from the long summer of 2024/Day three at the lake and the hills/img.jpg';
  await uploadFilesSeq([
    { name: 'img.jpg', type: 'image/jpeg', size: 3, webkitRelativePath: deep },
    { name: 'a.txt', type: 'text/plain', size: 1, webkitRelativePath: 'Holiday photos from the long summer of 2024/a.txt' },
  ]);
  const registered = new Set(FilesIdx.folders());
  for (const [sha, m] of Object.entries(FilesIdx.data.files))
    check(registered.has(m.folder), 'plain import filed under an unregistered folder: ' + m.folder);

  // Into an ENCRYPTED folder: its subfolders are registered and stay encrypted.
  ctx.T.setFolder('Private');
  FilesIdx.data.files = {};
  await uploadFilesSeq([
    { name: 'p.jpg', type: 'image/jpeg', size: 3, webkitRelativePath: 'Camera/raw/p.jpg' },
    { name: 'q.jpg', type: 'image/jpeg', size: 3, webkitRelativePath: 'Camera/q.jpg' },
  ]);
  const reg2 = new Set(FilesIdx.folders());
  const encFiles = Object.values(FilesIdx.data.files);
  check(encFiles.length === 2 && encFiles.every(m => m.enc), 'encrypted import used the encrypted path');
  for (const m of encFiles) {
    check(reg2.has(m.folder), 'encrypted import filed under an unregistered folder: ' + m.folder);
    check(FilesIdx.isEncFolder(m.folder), 'registered subfolder lost its encryption: ' + m.folder);
  }
  check(!FilesIdx.data.encFolders.includes('Private/raw'), 'a subfolder is not a second encryption root');

  // A restored index knows the type the server does not.
  check(extOfBlob({ type: 'application/octet-stream', url: 'https://b.test/' + 'a'.repeat(64) }, { name: 'IMG_1', mime: 'image/jpeg' }) === 'jpg',
    'restored mime ignored behind a generic server type');
  check(extOfBlob({ type: 'video/mp4' }, { name: 'clip', mime: 'image/jpeg' }) === 'mp4', 'a specific server type still wins');

  // Icons: sprites, not emoji; an encrypted file keeps its type glyph plus a lock badge.
  const encIcon = _fxEncIcon('jpg', 'image/jpeg');
  check(/fx-file-image/.test(encIcon) && /#i-lock/.test(encIcon), 'encrypted photo icon: ' + encIcon);
  check(/fx-file-document/.test(_fxEncIcon('', 'application/pdf').replace('fx-file-pdf', 'fx-file-document')), 'encrypted pdf icon');
  for (const f of ['Music', 'Private', 'Private/raw', 'Posts'])
    check(!/[📁🔒🎵]/u.test(_fxFolderIcon(f)) && /<svg/.test(_fxFolderIcon(f)), 'folder icon is an emoji for ' + f);
  check(/#i-lock/.test(_fxFolderIcon('Private/raw')) && /#i-folder/.test(_fxFolderIcon('Posts')) && /#i-music/.test(_fxFolderIcon('Music')),
    'folder icon kinds');

  if (fails.length) { console.log('FAIL\n' + fails.join('\n')); process.exit(1); }
  console.log('ALL OK');
})().catch(e => { console.log('FAIL ' + (e && e.stack || e)); process.exit(1); });
