/* Admin → Bots: a picked avatar file becomes the bot's avatar URL — on pick, and on Save.
 *
 * Reported: "avatar url does not get set when uploading avatar file". The file input waited for a
 * separate ⬆ Upload button and Save ignored it, so bot `fever` was saved five times after choosing a
 * picture and never had a nostr_profile_picture (no upload-avatar request in the log at all).
 *
 * Runs the SHIPPED static/js/admin-bots.js under node with a minimal DOM: real change events, real
 * saveBot(), a recording fetch.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.join(here, '..', '..', 'static', 'js', 'admin-bots.js'), 'utf8');
const failures = [];
const check = (ok, what) => { if (!ok) failures.push(what); };

function world({ id = '', nsec = '', uploadOk = true } = {}) {
  const els = {}, handlers = {}, calls = [];
  const state = { closed: 0 };
  const el = (idv) => els[idv] || (els[idv] = {
    id: idv, value: '', checked: false, textContent: '', innerHTML: '', dataset: {}, files: null,
    style: { display: '' }, hidden: false, classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    addEventListener() {}, setAttribute() {}, removeAttribute() {}, querySelector: () => null,
  });
  el('bot_f_id').value = id; el('bot_f_nostr_nsec').value = nsec; el('bot_f_name').value = 'fever';
  el('bot_f_type').value = 'text'; el('bot_f_platform').value = 'nostr';
  el('bot_grp_advanced').style.display = 'none';
  const document = {
    getElementById: el, querySelector: () => null, querySelectorAll: () => [],
    addEventListener: (type, fn) => (handlers[type] = handlers[type] || []).push(fn),
  };
  const fetch = async (url, opt = {}) => {
    const body = opt.body ? JSON.parse(opt.body) : null;
    calls.push({ url, method: opt.method || 'GET', body });
    if (url.endsWith('/talk/face')) {
      const n = calls.filter(c => c.url.endsWith('/talk/face')).length;
      return { ok: true, json: async () => ({ sha: String(n).repeat(64), mouth: { found: true, x: 0.1 * n, y: 0.6, w: 0.1, angle: n === 1 ? 12 : 0 } }) };
    }
    if (url.endsWith('/upload-avatar')) {
      return uploadOk ? { ok: true, json: async () => ({ url: 'https://poster.place/blossom/abc.png' }) }
                      : { ok: false, statusText: 'nope', json: async () => ({ detail: 'blossom refused' }) };
    }
    if (/\/api\/admin\/bots$/.test(url) && opt.method === 'POST') return { ok: true, json: async () => ({ id: 99, ...(body || {}) }) };
    return { ok: true, json: async () => ([]) };
  };
  class FileReader { readAsDataURL() { setTimeout(() => { this.result = 'data:image/png;base64,AAAA'; this.onload && this.onload(); }, 0); } }
  const ctx = { document, fetch, FileReader, console, setTimeout, setInterval: () => 0, clearInterval() {},
                window: {}, localStorage: { getItem: () => null, setItem() {} }, URL, JSON, Promise,
                pcConfirm: async () => true, csrfFetch: fetch, closeBotModal() { state.closed++; }, alert() {} };
  ctx.window = ctx;
  vm.runInNewContext(SRC + '\n;this.__api={saveBot, uploadBotAvatar};', ctx, { filename: 'admin-bots.js' });
  const pick = () => {
    el('bot_f_nostr_avatar_file').files = [{ name: 'fever.png', size: 1234, lastModified: 42, type: 'image/png' }];
    (handlers.change || []).forEach(h => h({ target: el('bot_f_nostr_avatar_file') }));
  };
  const addFace = () => {
    el('bot_talk_face_file').files = [{ name: 'face.png', size: 99, lastModified: 1, type: 'image/png' }];
    (handlers.change || []).forEach(h => h({ target: el('bot_talk_face_file') }));
  };
  const click = (attrs) => {
    const target = { id: '', closest: sel => (Object.keys(attrs).some(k => sel === `[${k}]`)
      ? { dataset: Object.fromEntries(Object.entries(attrs).map(([k, v]) => [k.replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase()), v])) }
      : null) };
    (handlers.click || []).forEach(h => h({ target }));
  };
  const drag = (x, y) => {
    el('bot_talk_stage').getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100 });
    (handlers.pointerdown || []).forEach(h => h({ target: el('bot_talk_mouth'), clientX: x, clientY: y, preventDefault() {} }));
    (handlers.pointerup || []).forEach(h => h({}));
  };
  document.addEventListener = (type, fn) => (handlers[type] = handlers[type] || []).push(fn);
  document.removeEventListener = () => {};
  const slide = (id, v, type = 'input') => { el(id).value = String(v); (handlers[type] || []).forEach(h => h({ target: el(id) })); };
  return { el, calls, api: ctx.__api, pick, addFace, click, drag, slide, state };
}
const settle = () => new Promise(r => setTimeout(r, 20));
const uploads = c => c.filter(x => x.url.endsWith('/upload-avatar'));
const saves = c => c.filter(x => /\/api\/admin\/bots(\/\d+)?$/.test(x.url) && x.method !== 'GET');

{ // picking a file on a bot that has a key uploads it at once
  const w = world({ id: '27' });
  w.pick(); await settle();
  check(uploads(w.calls).length === 1, 'picking a picture did not upload it');
  check(w.el('bot_f_nostr_profile_picture').value === 'https://poster.place/blossom/abc.png',
        'the uploaded URL was not put in the avatar field');
  await w.api.saveBot(); await settle();
  check(uploads(w.calls).length === 1, 'Save uploaded the same picture a second time');
  const s = saves(w.calls)[0];
  check(s && s.body.config.nostr_profile_picture === 'https://poster.place/blossom/abc.png',
        'the saved config has no avatar URL: ' + JSON.stringify(s && s.body.config));
}
{ // picked but never uploaded (no key when picked) — Save uploads it FIRST, then saves the URL
  const w = world({ id: '', nsec: '' });
  w.pick(); await settle();
  check(uploads(w.calls).length === 0, 'uploaded without any key to sign it with');
  w.el('bot_f_nostr_nsec').value = 'nsec1generated';
  await w.api.saveBot(); await settle();
  const order = w.calls.map(c => c.url.endsWith('/upload-avatar') ? 'upload' : (saves([c]).length ? 'save' : ''));
  check(order.filter(Boolean).join(',') === 'upload,save', 'Save did not upload the pending picture first: ' + order);
  check(saves(w.calls)[0].body.config.nostr_profile_picture === 'https://poster.place/blossom/abc.png',
        'the pending picture did not reach the saved config');
}
{ // A FAILED AVATAR DOES NOT BLOCK THE SAVE ("i could not even save the bot due to that upload error"):
  // the bot saves with the picture it had, the dialog stays open saying why, and Save retries the picture.
  const w = world({ id: '27', uploadOk: false });
  w.pick(); await settle();
  await w.api.saveBot(); await settle();
  check(saves(w.calls).length === 1, 'the bot was not saved because its avatar failed');
  check(!saves(w.calls)[0].body.config.nostr_profile_picture, 'a picture that never uploaded was saved as the avatar');
  check(/Saved, but the avatar could not be uploaded/.test(w.el('botModalError').textContent) &&
        /blossom refused/.test(w.el('botModalError').textContent), 'the message did not say it saved, or why the avatar failed');
  check(w.state.closed === 0, 'the dialog closed over the warning');
  const tried = uploads(w.calls).length;                // picking the file already tried once, Save once more
  await w.api.saveBot(); await settle();
  check(uploads(w.calls).length === tried + 1, 'the next Save did not retry the avatar');
}
{ // ...and a NEW bot saved that way takes its id, so the retry updates it instead of creating a second one
  const w = world({ nsec: 'nsec1generated', uploadOk: false });
  w.pick(); await settle();
  await w.api.saveBot(); await settle();
  check(saves(w.calls).length === 1 && saves(w.calls)[0].method === 'POST', 'the new bot was not created');
  check(String(w.el('bot_f_id').value) === '99', 'the created bot did not take its id');
  await w.api.saveBot(); await settle();
  check(saves(w.calls)[1].method === 'PUT' && /\/api\/admin\/bots\/99$/.test(saves(w.calls)[1].url), 'the retry created a SECOND bot');
}

{ // talking replies: at most 10 faces (talkbot_service.MAX_FACES); an 11th is refused, not uploaded
  const w = world({ id: '28' });
  const faces = () => JSON.parse(w.el('bot_f_talk_faces').value || '[]');
  for (let i = 0; i < 11; i++) { w.addFace(); await settle(); }
  check(faces().length === 10, 'expected 10 faces (an 11th refused), got ' + faces().length);
  check(w.calls.filter(c => c.url.endsWith('/talk/face')).length === 10, 'an 11th face was uploaded anyway');
}

{ // talking replies: each face has its OWN mouth, all saved
  const w = world({ id: '27' });
  const faces = () => JSON.parse(w.el('bot_f_talk_faces').value || '[]');
  for (let i = 0; i < 3; i++) { w.addFace(); await settle(); }
  check(faces().length === 3, 'expected 3 faces, got ' + faces().length);
  w.click({ 'data-talk-face': '0' });                 // select face 1 …
  w.drag(40, 70);                                     // … and move ITS mouth
  const f = faces();
  check(Math.abs(f[0].mouth.x - 0.4) < 1e-9 && Math.abs(f[0].mouth.y - 0.7) < 1e-9, 'dragging did not move face 1\'s mouth');
  check(Math.abs(f[2].mouth.x - 0.3) < 1e-9, 'dragging face 1 moved another face\'s mouth');
  w.click({ 'data-talk-del': '1' });                  // remove face 2
  check(faces().map(x => x.sha[0]).join('') === '13', 'removing face 2 removed the wrong one: ' + faces().map(x => x.sha[0]));
  await w.api.saveBot(); await settle();
  const saved = JSON.parse(saves(w.calls)[0].body.config.talk_faces || '[]');
  check(saved.length === 2 && saved[0].mouth.x === f[0].mouth.x, 'Save did not persist the faces and their mouths');
}

{ // "for talking bots, we need mouth tilt like meme builder": a tilt per face, previewed on the marker
  const w = world({ id: '27' });
  const faces = () => JSON.parse(w.el('bot_f_talk_faces').value || '[]');
  w.addFace(); await settle(); w.addFace(); await settle();
  w.click({ 'data-talk-face': '0' });
  check(w.el('bot_talk_mouth_a').value == 12, 'the detected tilt did not reach the slider: ' + w.el('bot_talk_mouth_a').value);
  check(w.el('bot_talk_mouth_adeg').textContent === '+12°', 'the tilt label is wrong: ' + w.el('bot_talk_mouth_adeg').textContent);
  w.slide('bot_talk_mouth_a', -20);                     // moving the slider, not only releasing it
  check(faces()[0].mouth.angle === -20, 'the tilt slider did not set face 1\'s tilt: ' + faces()[0].mouth.angle);
  check(/rotate\(-20deg\)/.test(w.el('bot_talk_mouth').style.transform || ''), 'the marker does not preview the tilt: ' + w.el('bot_talk_mouth').style.transform);
  check(faces()[1].mouth.angle === 0, 'tilting face 1 tilted face 2');
  w.slide('bot_talk_mouth_a', 90, 'change');
  check(faces()[0].mouth.angle === 45, 'the tilt was not clamped to the renderer\'s range: ' + faces()[0].mouth.angle);
  await w.api.saveBot(); await settle();
  const saved = JSON.parse(saves(w.calls)[0].body.config.talk_faces || '[]');
  check(saved[0].mouth.angle === 45 && saved[1].mouth.angle === 0, 'Save did not keep each face\'s tilt');
}

if (failures.length) { console.error(failures.map(f => '✗ ' + f).join('\n')); process.exit(1); }
console.log('admin bot avatar: picked → uploaded → saved');
