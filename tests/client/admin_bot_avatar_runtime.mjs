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
    if (url.endsWith('/upload-avatar')) {
      return uploadOk ? { ok: true, json: async () => ({ url: 'https://poster.place/blossom/abc.png' }) }
                      : { ok: false, statusText: 'nope', json: async () => ({ detail: 'blossom refused' }) };
    }
    return { ok: true, json: async () => ([]) };
  };
  class FileReader { readAsDataURL() { setTimeout(() => { this.result = 'data:image/png;base64,AAAA'; this.onload && this.onload(); }, 0); } }
  const ctx = { document, fetch, FileReader, console, setTimeout, setInterval: () => 0, clearInterval() {},
                window: {}, localStorage: { getItem: () => null, setItem() {} }, URL, JSON, Promise,
                pcConfirm: async () => true, csrfFetch: fetch, closeBotModal() {}, alert() {} };
  ctx.window = ctx;
  vm.runInNewContext(SRC + '\n;this.__api={saveBot, uploadBotAvatar};', ctx, { filename: 'admin-bots.js' });
  const pick = () => {
    el('bot_f_nostr_avatar_file').files = [{ name: 'fever.png', size: 1234, lastModified: 42, type: 'image/png' }];
    (handlers.change || []).forEach(h => h({ target: el('bot_f_nostr_avatar_file') }));
  };
  return { el, calls, api: ctx.__api, pick };
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
{ // an upload that fails does not save a bot that looks right and has no picture
  const w = world({ id: '27', uploadOk: false });
  w.pick(); await settle();
  await w.api.saveBot(); await settle();
  check(saves(w.calls).length === 0, 'saved anyway after the avatar upload failed');
  check(/avatar could not be uploaded/.test(w.el('botModalError').textContent), 'no message said why');
}

if (failures.length) { console.error(failures.map(f => '✗ ' + f).join('\n')); process.exit(1); }
console.log('admin bot avatar: picked → uploaded → saved');
