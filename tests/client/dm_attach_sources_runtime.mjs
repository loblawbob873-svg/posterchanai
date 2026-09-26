/* Messages: a Music track and a Synced Folder file can be attached, and every source obeys the 🔒.
 *
 * Runs the SHIPPED dmAttachFiles / dmAttachMenu / _dmDecryptedDriveFile / dmPickMedia out of the
 * client source against stubs, and checks what a user would see: what lands in the message box,
 * which upload path carried it, and that a Music pick never inserts a link to ciphertext.
 *
 * Reported: "Messages -> Can't add Music or Synced Folder files to Message". Music is an ENCRYPTED
 * folder, which the drive picker hides unless the caller opts in (`allowEncrypted`), and Messages
 * never did; a synced folder was reachable from no attach control at all.
 */
import vm from 'node:vm';
import { clientSource } from './client_source.mjs';

const source = clientSource();
function lift(decl){
  const start = source.indexOf(decl);
  if(start < 0) throw new Error('missing from the client source: ' + decl);
  let depth = 0, end = -1;
  for(let i = source.indexOf('{', start); i < source.length; i++){
    if(source[i] === '{') depth++;
    else if(source[i] === '}' && --depth === 0){ end = i + 1; break; }
  }
  return source.slice(start, end);
}
const code = [
  lift('async function dmAttachFiles(inp, files, st){'),
  lift('function dmAttachMenu(anchor, inp, fileInput, st){'),
  lift('async function _dmDecryptedDriveFile(sha, name, type){'),
  lift('function dmPickMedia(inp, st){'),
].join('\n');

const failures = [];
const check = (ok, what) => { if(!ok) failures.push(what); };
const tick = () => new Promise(r => setTimeout(r, 0));

function world(opts = {}){
  const w = {
    lock: !!opts.lock, standalone: !!opts.standalone, toasts: [], uploads: [], encUploads: [],
    events: [], picker: null, menu: null, decrypted: [],
    synced: opts.synced === undefined ? new File(['synced bytes'], 'report.pdf', { type: 'application/pdf' }) : opts.synced,
  };
  const inp = { value: '', dispatchEvent(ev){ w.events.push(ev.type); return true; } };
  w.inp = inp;
  const ctx = {
    File, Blob, Event, setTimeout, Promise, console,
    toast: t => w.toasts.push(String(t)),
    dmEncOn: () => w.lock,
    _standalone: () => w.standalone,
    uploadBlob: async f => { w.uploads.push(f); return 'https://media.example/' + f.name.replace(/\W/g, '') + '-plain'; },
    uploadSharedEnc: async f => { w.encUploads.push(f); return 'https://media.example/' + f.name.replace(/\W/g, '') + '-sealed'; },
    encryptExistingUrl: async u => u + '#sealed',
    encFileUrl: async (sha, type) => { w.decrypted.push(sha); return opts.undecryptable ? null : 'blob:decrypted-' + sha; },
    fetch: async url => ({ blob: async () => new Blob(['PLAINTEXT-' + url], { type: '' }) }),
    blossomPicker: (target, onPick, o) => { w.picker = { target, onPick, opts: o || {} }; },
    openMenuPopover: (anchor, rows, cb) => { w.menu = { rows, cb }; },
    pickSyncedFile: async () => w.synced,
    PCWebxdc: null, window: {},
  };
  vm.runInNewContext(code + '\nthis.api={dmAttachFiles,dmAttachMenu,dmPickMedia};', ctx, { filename: 'app-dm-attach.js' });
  w.api = ctx.api;
  return w;
}

/* ---- 🌸 Files: the Music folder is offered, and a track arrives as a PLAYABLE file ------------- */
{
  const w = world();
  w.api.dmPickMedia(w.inp)();
  check(w.picker && w.picker.opts.allowEncrypted === true,
        'the Messages drive picker must opt into encrypted folders, or Music is not in it at all');
  await w.picker.onPick({ url: 'https://media.example/ciphertextsha', type: 'audio/mpeg', ext: 'mp3',
                          sha: 'ab'.repeat(32), enc: true, name: 'song.mp3' });
  check(w.decrypted.length === 1 && w.decrypted[0] === 'ab'.repeat(32), 'the picked track must be decrypted by its sha');
  check(w.uploads.length === 1 && w.uploads[0].name === 'song.mp3' && w.uploads[0].type === 'audio/mpeg',
        'a decrypted track must upload as the named, typed file: ' + JSON.stringify(w.uploads.map(f => [f.name, f.type])));
  check(!w.inp.value.includes('ciphertextsha'), 'a link to the ciphertext must never reach the message: ' + w.inp.value);
  check(w.inp.value.includes('songmp3-plain'), 'the playable copy must be in the message box: ' + w.inp.value);
  check(w.events.includes('input'), 'the composer must hear about it (strip, draft, Send button)');
  check(w.toasts.some(t => /readable copy/.test(t)), 'with the lock off it must SAY a readable copy is being sent');
}
{
  const w = world({ lock: true });
  w.api.dmPickMedia(w.inp)();
  await w.picker.onPick({ url: 'u', type: 'audio/mpeg', ext: 'mp3', sha: 'cd'.repeat(32), enc: true, name: 'song.mp3' });
  check(w.encUploads.length === 1 && w.uploads.length === 0, 'with 🔒 on a Music track must be sealed to the conversation');
  check(w.inp.value.includes('-sealed'), 'the sealed link must be what is inserted');
}
{
  const w = world({ undecryptable: true });
  w.api.dmPickMedia(w.inp)();
  await w.picker.onPick({ url: 'u', type: 'audio/mpeg', ext: 'mp3', sha: 'ef'.repeat(32), enc: true, name: 'song.mp3' });
  check(w.inp.value === '' && w.uploads.length === 0, 'a track that cannot be decrypted must insert nothing');
  check(w.toasts.some(t => /couldn't attach/.test(t)), 'and must say why');
}
{   // unchanged: a public drive file with the lock off is still inserted as its link
  const w = world();
  w.api.dmPickMedia(w.inp)();
  await w.picker.onPick({ url: 'https://media.example/pic', type: 'image/png', ext: 'png', sha: '', enc: false, name: 'p.png' });
  check(w.inp.value === 'https://media.example/pic.png', 'a public drive pick must still insert its link: ' + w.inp.value);
  check(w.uploads.length === 0, 'and must not re-upload it');
}

/* ---- 📎 menu: a synced folder is a source, in both composers --------------------------------- */
{
  const w = world();
  const input = { clicked: 0, click(){ this.clicked++; } };
  w.api.dmAttachMenu({}, w.inp, input);
  const keys = w.menu.rows.map(r => r[0]);
  check(keys.includes('file') && keys.includes('synced'), 'the 📎 menu must offer a synced folder: ' + keys);
  await w.menu.cb('synced');
  check(w.uploads.length === 1 && w.uploads[0].name === 'report.pdf', 'a synced pick must upload that file');
  check(w.inp.value.includes('reportpdf-plain') && w.events.includes('input'), 'and land in the message box');
  await w.menu.cb('file');
  check(input.clicked === 1, 'the device file row must still open the device picker');
}
{
  const w = world({ lock: true });
  w.api.dmAttachMenu({}, w.inp, { click(){} });
  await w.menu.cb('synced');
  check(w.encUploads.length === 1 && w.uploads.length === 0, 'a synced pick must obey 🔒 like any other file');
}
{
  const w = world({ synced: null });
  w.api.dmAttachMenu({}, w.inp, { click(){} });
  await w.menu.cb('synced');
  check(w.inp.value === '' && w.uploads.length === 0 && w.encUploads.length === 0, 'cancelling the synced picker must attach nothing');
}
{
  const w = world({ standalone: true });
  w.api.dmAttachMenu({}, w.inp, { click(){} });
  check(!w.menu.rows.some(r => r[0] === 'synced'), 'with no instance there is no synced-folder store to offer');
}

/* ---- the one upload loop ------------------------------------------------------------------- */
{
  const w = world();
  const st = { textContent: '' };
  const n = await w.api.dmAttachFiles(w.inp, [new File(['a'], 'a.txt'), new File(['b'], 'b.txt')], st);
  check(n === 2 && w.inp.value.split(' ').length === 2, 'two files → two links: ' + w.inp.value);
  check(st.textContent === '', 'the status line must clear when done');
}

if(failures.length){ console.error(failures.map(f => '✗ ' + f).join('\n')); process.exit(1); }
console.log('dm attach sources: music + synced folder + lock all hold');
await tick();
