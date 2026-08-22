/* The encrypted drive, from the extension. Its ONLY HTTP path.
 *
 * Everything else this extension does is relay-only, deliberately. A page screenshot cannot go over a
 * relay — NIP-44 refuses plaintext over 65535 bytes and a note is one event — so it goes where the
 * app puts note attachments: an AES-GCM blob on the instance's Blossom server, referenced from the
 * note by sha256.
 *
 * TWO KEYS, AND THEY ARE NOT THE SAME ONE. The vault key seals passwords and notes. Drive blobs are
 * sealed with the account's MASTER key, which the server holds WRAPPED (NIP-44 to the account's own
 * key) and hands back on request — so the extension unwraps it with the signing key it already has,
 * and the server never sees it. Using the vault key here would produce a blob the app cannot open,
 * which is worse than refusing: it would look like it worked.
 *
 * The index is deliberately NOT touched. `pcres:` attachments carry their own name and mime on the
 * note, so the app can render one with no index entry at all — and writing that index from here would
 * be a read-modify-write of the whole drive from a browser extension, where an empty read written
 * back over a full index is the one failure this project keeps a recovery script for.
 */
(function () {
  const NT = () => (self.NostrTools || self.nostrTools);

  const _b64 = (u8) => { let s = ''; for (const b of u8) s += String.fromCharCode(b); return btoa(s); };
  const _u8 = (b64) => { const s = atob(b64), u = new Uint8Array(s.length);
                         for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; };

  async function _sha256Hex(bytes) {
    const d = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    return [...d].map((x) => x.toString(16).padStart(2, '0')).join('');
  }

  /* The account's master key, unwrapped. `/client/files-index` answers a POST proving ownership with a
   * signed kind-27235, and returns a pointer whose `mk` is the key NIP-44'd to the account itself. */
  async function masterKey(cfg, skBytes, sign, decryptSelf) {
    const auth = await sign(27235, 'files-index', [['p', cfg.pubkey]]);
    const r = await fetch(cfg.api + '/client/files-index', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pubkey: cfg.pubkey, auth: btoa(JSON.stringify(auth)) }),
    });
    if (!r.ok) throw new Error('the drive would not answer (HTTP ' + r.status + ')');
    const j = await r.json();
    const wrapped = j && j.ok && j.index && j.index.mk;
    if (!wrapped)
      throw new Error('this account has no encrypted drive yet — open Files in PosterChan once, then try again');
    const T = NT();
    const plain = decryptSelf
      ? await decryptSelf(wrapped)
      : T.nip44.v2.decrypt(wrapped, T.nip44.v2.utils.getConversationKey(skBytes, cfg.pubkey));
    const mk = JSON.parse(plain).k;
    const raw = _u8(mk);
    if (raw.length !== 32) throw new Error('the drive key came back the wrong size — nothing was changed');
    return raw;
  }

  /* AES-GCM with the IV PREPENDED — byte-for-byte what the app's `_masterEncrypt` writes, because the
   * app is what has to read it back. A different envelope here is a blob that decrypts to nothing on
   * the only screen it is ever opened from. */
  async function seal(mk, bytes) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const k = await crypto.subtle.importKey('raw', mk, 'AES-GCM', false, ['encrypt']);
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, k, bytes));
    const out = new Uint8Array(iv.length + ct.length);
    out.set(iv, 0); out.set(ct, iv.length);
    return out;
  }

  /* BUD-02 upload: PUT the bytes with a kind-24242 authorisation naming their hash. `keep` marks the
   * blob as drive content so the age sweep never collects it — a note's picture must outlive any
   * retention policy meant for throwaway media. */
  /* WHERE the blob goes, which must be where the APP will look for it. `mediaServer()` in the client
   * returns the user's own Blossom server when they have configured one and the instance's built-in
   * `/blossom` otherwise — so the pairing carries that answer rather than this guessing. An older
   * pairing has no `media`, and falling back to the instance is right for the default case and
   * exactly wrong for a custom one, which is why the popup says which host it used. */
  function blobBase(cfg) {
    return (cfg.media || (cfg.api ? cfg.api + '/blossom' : '')).replace(/\/+$/, '');
  }

  async function upload(cfg, sealed, sign) {
    const base = blobBase(cfg);
    if (!base) throw new Error('this pairing carries no address for your file storage');
    const sha = await _sha256Hex(sealed);
    const at = Math.floor(Date.now() / 1000);
    const auth = await sign(24242, 'Upload blob', [
      ['t', 'upload'], ['x', sha], ['expiration', String(at + 300)],
    ]);
    const r = await fetch(base + '/upload', {
      method: 'PUT',
      headers: {
        'Authorization': 'Nostr ' + btoa(JSON.stringify(auth)),
        'Content-Type': 'application/octet-stream',
        'X-Keep': '1',            // drive content: exempt from the age sweep, like the app's uploads
        'X-No-Mirror': '1',       // never DR-mirror an encrypted personal blob to public backups
      },
      body: sealed,
    });
    if (r.status === 413) throw new Error('the picture is larger than this server accepts');
    if (r.status === 401 || r.status === 403)
      throw new Error('this account is not allowed to upload to that server');
    if (!r.ok) throw new Error('the upload failed (HTTP ' + r.status + ')');
    let out = null;
    try { out = await r.json(); } catch (_) {}
    // Trust OUR hash of the bytes we sealed, not the server's echo: the note points at the blob by
    // hash, and taking that from the response would let a wrong answer produce an unreadable note.
    return { sha, host: base, url: (out && out.url) || (base + '/' + sha) };
  }

  self.PCDrive = { masterKey, seal, upload, blobBase, b64: _b64 };
})();
