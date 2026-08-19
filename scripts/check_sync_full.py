#!/usr/bin/env python3
"""THE FULL TEST: real client pages as devices, syncing real bytes through the real server.

Everything is the shipped article except the disk: each "device" is a headless Chrome running the
real /client page (real engine, executor, crypto, io, manifests, Blossom, relay, drive key), with
`window.pcFs` injected as a virtual disk holding real bytes. What is asserted is what four days of
field failures were about:

  1. Device B receives EVERY file device A holds, byte-identical.
  2. A real deletion on B propagates to A — exactly that file, into .pc-trash, nothing else.
  3. A device whose scan LIES (empty listing over a full disk) deletes NOTHING anywhere.
  4. Restore from trash STICKS — the sweep that follows it does not put the file straight back.
  5. Both devices end on one drive key; no "sealed with a different key" in any report.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT_A = int(os.environ.get("PC_CHECK_PORT") or 9551)
PORT_B = PORT_A + 1
PROF = (os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-syncfull")

# A virtual disk with the REAL adapter surface. Bytes live in the page as Uint8Arrays; hashing uses
# the same WebCrypto the client uses. `confirmGone` answers honestly off the disk map — and the
# `lying` switch makes scanPage return an empty listing while the disk stays full, which is the
# exact failure shape that used to delete folders.
VDISK = r"""
(() => {
  const D = window.__vdisk = { files: {}, bin: {}, trash: [], lying: false, moved: [] };
  const sha = async (u8) => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', u8)))
    .map(b => b.toString(16).padStart(2, '0')).join('');
  const stat = (r) => ({ size: D.files[r].bytes.length, mtime: D.files[r].mtime });
  window.pcFs = {
    chunkBytes: 4 * 1024 * 1024,
    list: async () => [{ id: 'vdisk', dir: '/vdisk' }],
    scanPage: async (id, so, off, lim) => {
      const k = D.lying ? [] : Object.keys(D.files).sort();
      const end = Math.min(k.length, (off||0) + (lim||1000)), files = {};
      for(let i = off||0; i < end; i++){
        const e = Object.assign({}, stat(k[i]));
        if(so && so.hash) e.csum = await sha(D.files[k[i]].bytes);
        files[k[i]] = e;
      }
      return { files, skipped: [], total: k.length, done: end >= k.length };
    },
    read: async (id, r) => D.files[r].bytes,
    readPart: async (id, r, off, len) => D.files[r].bytes.subarray(off, off + len),
    /* A TRASH PATH IS HASHED FROM THE TRASH, like `move` reads it and like both real bridges do —
       they resolve any relative path, and .pc-trash is just a directory to them. Reading only the
       live tree returned the hash of an EMPTY buffer for every trashed file: not an error, a wrong
       answer, which the reconcile then correctly refused to act on ("different bytes"). */
    hashFile: async (id, r) => { const src = r.indexOf('.pc-trash/') === 0 ? D.bin : D.files;
      return src[r] ? sha(src[r].bytes) : null; },
    write: async (id, r, bytes, mtime) => {
      D.files[r] = { bytes: new Uint8Array(bytes), mtime: mtime || 1000 };
      return { size: D.files[r].bytes.length, mtime: D.files[r].mtime };   // like the desktop bridge
    },
    writePart: async (id, r, off, bytes) => {
      const cur = (D.parts = D.parts || {})[r] || new Uint8Array(0);
      const next = new Uint8Array(Math.max(cur.length, off + bytes.length));
      next.set(cur); next.set(new Uint8Array(bytes), off); D.parts[r] = next;
    },
    partSize: async (id, r) => ((D.parts || {})[r] || new Uint8Array(0)).length,
    hashPart: async (id, r) => sha((D.parts || {})[r] || new Uint8Array(0)),
    discardPart: async (id, r) => { if(D.parts) delete D.parts[r]; },
    writeCommit: async (id, r, mtime) => {
      D.files[r] = { bytes: D.parts[r], mtime: mtime || 1000 }; delete D.parts[r];
      return { size: D.files[r].bytes.length, mtime: D.files[r].mtime };   // like the desktop bridge
    },
    /* Moves happen BOTH ways across the trash boundary — a conflict rename out of the live tree,
       and a restore back into it — so the source is whichever side holds the path. */
    move: async (id, from, to) => {
      const src = from.indexOf('.pc-trash/') === 0 ? D.bin : D.files;
      const dst = to.indexOf('.pc-trash/') === 0 ? D.bin : D.files;
      dst[to] = src[from]; delete src[from]; D.moved.push([from, to]);
    },
    /* THE TRASH KEEPS THE BYTES. It used to drop them on the floor, which made "Restore from
       trash" — the one recovery a person reaches for when a sweep has taken files — impossible to
       drive here at all. */
    trash: async (id, r, when) => {
      const at = '.pc-trash/x/' + r;
      D.bin[at] = D.files[r];
      D.trash.push(r); delete D.files[r]; return at;
    },
    listTrash: async (id) => Object.keys(D.bin).map(at => ({ at, to: at.slice('.pc-trash/x/'.length) })),
    emptyTrash: async () => { D.bin = {}; return 0; },
    purgeTrash: async (id, rels) => { let removed = 0, missing = 0;
      for(const r of (rels || [])){
        if(r.indexOf('.pc-trash/') !== 0) continue;      // the bridges refuse it; so does this
        if(D.bin[r]){ delete D.bin[r]; removed++; } else missing++;
      }
      return { removed, missing, failed: [] }; },
    trashStat: async () => ({ files: Object.keys(D.bin).length, bytes: 0 }),
    sweepParts: async () => 0,
    confirmGone: async (id, r) => ({ gone: !(r in D.files), parentAlive: true }),
    watch: async () => {}, unwatch: async () => {},
    power: async () => ({ onBattery: false, charging: true }),
  };
})()"""

LOGIN = r"""(async (nsec) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  try{ sessionStorage.clear(); }catch(_){}
  document.body.classList.remove('guest');
  const g=$('#auth-gate'); if(g) g.classList.remove('hidden');
  const l=$('#auth-login'); if(l) l.classList.remove('hidden');
  const nb=$('#btn-nsec'); if(nb) nb.click(); await sleep(80);
  const inp=$('#nsec-input'); if(!inp) return false;
  inp.value = nsec;
  const go=$('#btn-nsec-login'); if(!go) return false;
  go.click();
  for(let i=0;i<40;i++){ await sleep(250); if(window.__PC && __PC.me && __PC.me()) return true; }
  return false;
})"""

ADD_FOLDER = r"""(() => {
  const me = window.__PC.me().pubkey;
  localStorage.setItem('pc_sync_folders_' + me, JSON.stringify([
    { id: 'vdisk', key: 'E2EPair', dir: '/vdisk', name: 'E2EPair',
      excludes: [], prefs: {}, lastSyncAt: 0, lastFullScanAt: 0 }]));
  return true;
})"""

SWEEP = r"""(async () => {
  try{
    const f = window.PCSync.folders()[0];
    const rep = await window.PCSync.sweep(f, { manual: true });
    return { ok: true,
             uploaded: (rep.plan && rep.plan.upload || []).length,
             downloaded: (rep.plan && rep.plan.download || []).length,
             trashed: (rep.plan && rep.plan.deleteLocal || []).length,
             removedRemote: (rep.removedRemote || []).length,
             held: (rep.unconfirmedAbsent || []).length,
             failed: (rep.failed || []).map(x => x.path + ':' + x.error).slice(0, 5),
             skipped: !!rep.skipped, why: rep.why || '' };
  }catch(e){ return { ok: false, err: String((e && e.message) || e) }; }
})"""

DISK = r"""(async () => {
  const out = {};
  for(const [k, v] of Object.entries(window.__vdisk.files))
    out[k] = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', v.bytes)))
      .map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  return { files: out, trash: window.__vdisk.trash.slice(),
           bin: Object.keys(window.__vdisk.bin) };
})"""

# Restore everything in this device's .pc-trash, through the SHIPPED loop (which now tells the
# sweep that follows it what was put back). uiConfirm is stubbed: there is nobody to press it.
RESTORE = r"""(async () => {
  const was = window.__PC.uiConfirm;
  window.__PC.uiConfirm = async () => true;
  try{
    const f = window.PCSync.folders()[0];
    const r = await window.PCSync.restoreTrash(f.id);
    return r || { done: 0 };
  }catch(e){ return { err: String((e && e.message) || e) }; }
  finally{ window.__PC.uiConfirm = was; }
})"""


# Reconcile the trash through the SHIPPED loop. The difference from RESTORE is the whole point:
# this one PROVES each row first, so a deletion every device agreed to is removed rather than put
# back. uiConfirm is stubbed — there is nobody to press it.
RECONCILE = r"""(async () => {
  const was = window.__PC.uiConfirm;
  window.__PC.uiConfirm = async () => true;
  try{
    const f = window.PCSync.folders()[0];
    const r = await window.PCSync.reconcileTrash(f.id);
    return r || { restored: [], purged: 0, kept: 0 };
  }catch(e){ return { err: String((e && e.message) || e) }; }
  finally{ window.__PC.uiConfirm = was; }
})"""


class Tab:
    def __init__(self, port, profile):
        self.port, self.profile = port, profile
        self.proc, self.ws, self.n = None, None, 0

    async def start(self, chrome, url):
        import websockets
        subprocess.run(["rm", "-rf", self.profile], check=False)
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            return False
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        await self.call("Page.addScriptToEvaluateOnNewDocument", {"source": VDISK})
        await self.call("Page.navigate", {"url": url})
        for _ in range(80):
            await asyncio.sleep(0.25)
            if await self.js("!!(window.__PC && window.PCSync && window.pcFs)"):
                return True
        return False

    async def call(self, method, params=None):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            r = json.loads(await self.ws.recv())
            if r.get("id") == self.n:
                return r.get("result")

    async def js(self, expr, aw=False):
        r = await self.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True, "awaitPromise": aw,
                             "timeout": 120000})
        if r.get("exceptionDetails"):
            if os.environ.get("PC_DEBUG"):
                print("  DEBUG:", json.dumps(r["exceptionDetails"])[:700])
            return None
        return r["result"].get("value")

    def stop(self):
        try:
            self.proc and self.proc.terminate()
        except Exception:
            pass
        subprocess.run(["rm", "-rf", self.profile], check=False)


async def drive(url):
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.services.nostr import bech32 as _b32
    _nsec_sk = bytes.fromhex(secrets.token_hex(32))
    nsec = _b32.encode("nsec", _nsec_sk)
    a, b = Tab(PORT_A, PROF + "-a"), Tab(PORT_B, PROF + "-b")
    problems = []
    try:
        ok = await asyncio.gather(a.start(chrome, url), b.start(chrome, url))
        if not all(ok):
            print("SKIP  a client never finished loading")
            return 2
        for t in (a, b):
            if not await t.js(f"({LOGIN})({json.dumps(nsec)})", aw=True):
                print("SKIP  login failed")
                return 2
        reg = await a.js("""(async () => {
          const auth = await window.__PC.signAuth('login');
          const r = await fetch('/api/auth/nostr-login', { method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ pubkey: window.__PC.me().pubkey, auth: btoa(JSON.stringify(auth)) }) });
          return r.ok; })()""", aw=True)
        if not reg:
            print("SKIP  could not register the throwaway account")
            return 2
        # The throwaway needs the upload privilege a fresh account doesn't get (that gate is its own
        # feature and not what this check is about). The check runs on the node, so grant directly.
        from app.services.nostr import bip340 as _b340
        pk_hex = _b340.pubkey_from_seckey(_b32.decode_key(nsec)).hex() if hasattr(_b32, "decode_key") else None
        try:
            from app.database import SessionLocal
            from app.models import User
            from app.services.nostr import nostr_service as _ns
            db = SessionLocal()
            u = db.query(User).filter(User.nostr_npub == _ns.npub_of(
                _b340.pubkey_from_seckey(__import__("app.services.nostr.bech32", fromlist=["decode"]).decode("nsec", nsec) if False else _nsec_sk).hex())).first()
            if u:
                u.can_blossom = True
                db.commit()
            db.close()
        except Exception as e:
            print(f"SKIP  could not grant upload privilege: {e}")
            return 2
        # Admit-and-VERIFY: the signup fires a relay reload, but this check must not depend on
        # winning that race (a deploy restarting the relay can eat one). Trigger again from here
        # and poll until a probe write for the account actually lands.
        from app.services.nostr_relay.thread import trigger_block_reload
        from app.services import nostr_store as _nstore
        from app.services.nostr_store import user_storage_seckey as _ussk
        from app.database import SessionLocal as _SL
        from app.models import User as _User
        from app.services.nostr import nostr_service as _ns2
        _db = _SL()
        _u = _db.query(_User).filter(_User.nostr_npub == _ns2.npub_of(
            _b340.pubkey_from_seckey(_nsec_sk).hex())).first()
        _sk2 = _ussk(_db, _u)
        _db.close()
        admitted = False
        trigger_block_reload()          # once — hammering it only builds a control backlog
        for _try in range(20):
            await asyncio.sleep(6)
            if await asyncio.get_event_loop().run_in_executor(None, lambda: asyncio.run(
                    _nstore.put_doc(3052, _sk2, "pcai:sync-check-probe", {"t": _try}, encrypt=False))):
                admitted = True
                break
        if not admitted:
            print("SKIP  the relay never admitted the throwaway's keys")
            return 2

        # ---- 1. A holds 12 files (one multi-MB); B must receive every byte -----------------------
        await a.js("""(() => { const D = window.__vdisk;
          for(let i=0;i<11;i++){ const u=new Uint8Array(1000+i*37); u.fill(i+1);
            D.files['dir'+(i%3)+'/f'+i+'.bin']={bytes:u,mtime:1000+i}; }
          const big=new Uint8Array(6*1024*1024); for(let i=0;i<big.length;i+=4096) big[i]=i&255;
          D.files['big/video.bin']={bytes:big,mtime:2000};
          return true; })()""")
        for t in (a, b):
            await t.js(f"({ADD_FOLDER})()")
        r1 = {}
        for _try in range(3):
            r1 = await a.js(f"({SWEEP})()", aw=True) or {}
            if r1.get("ok") and not r1.get("failed"):
                break
            await asyncio.sleep(6)
        print("  A first sweep:", json.dumps(r1))
        if not r1.get("ok") or r1.get("failed"):
            problems.append(f"A's first sweep: {r1.get('err') or r1.get('failed')}")
        r2 = {}
        for _try in range(3):
            r2 = await b.js(f"({SWEEP})()", aw=True) or {}
            if r2.get("ok") and r2.get("downloaded"):
                break
            await asyncio.sleep(6)
        print("  B first sweep:", json.dumps(r2))
        da, db = await a.js(f"({DISK})()", aw=True), await b.js(f"({DISK})()", aw=True)
        if not db or db["files"] != da["files"]:
            missing = set((da or {}).get("files", {})) - set((db or {}).get("files", {}))
            problems.append(f"B did not replicate A byte-for-byte ({len(missing)} missing/differ: "
                            f"{sorted(missing)[:4]})")
        else:
            print(f"  replicated: {len(db['files'])} files byte-identical")

        # ---- 1.5 THE DIRTY JOIN — the most common real join, and the one that was never tested:
        # a THIRD cold device (fresh profile on A's port after A closes? — no: reuse B's page with
        # cleared state) already HOLDING data: identical copies must settle to ZERO conflicts,
        # divergent bytes must conflict EXACTLY once each, and old conflict-named debris syncs as
        # ordinary files. "i readded pictures on phone and it instantly has 373 conflicts" — if
        # identical bytes conflict, this fails loudly here instead of on somebody's phone.
        await b.js("""(async () => {
          const D = window.__vdisk;
          // a REAL re-add: the journal is cleared through the same API the remove flow uses; the
          // device id stays (that is what a phone keeps across remove-and-re-add)
          await window.PCSync.docs.saveIndex('E2EPair', {});
          try{ await window.PCSync.docs.state && null; }catch(_){}
          const me = window.__PC.me().pubkey;
          localStorage.setItem('pc_sync_folders_' + me, JSON.stringify([
            { id: 'vdisk', key: 'E2EPair', dir: '/vdisk', name: 'E2EPair',
              excludes: [], prefs: {}, lastSyncAt: 0, lastFullScanAt: 0 }]));
          // divergent DIFFERENT mtimes but identical bytes for 5 existing files (the settle case)
          for(let i = 1; i <= 5; i++){ const f = D.files['dir' + (i % 3) + '/f' + i + '.bin'];
            if(f) f.mtime = 9999 + i; }
          // 2 genuinely divergent files
          for(const p of ['dir0/f6.bin', 'dir1/f7.bin']){
            if(D.files[p]){ const u = new Uint8Array(D.files[p].bytes.length + 3); u.fill(77); D.files[p] = { bytes: u, mtime: 8888 }; } }
          // 1 piece of old conflict-named debris on disk
          D.files['old (conflict from Android-dead, 2026-08-16).bin'] = { bytes: new Uint8Array([1,2,3]), mtime: 100 };
          return true; })()""", aw=True)
        rj = await b.js(f"({SWEEP})()", aw=True) or {}
        print("  B dirty join:", json.dumps(rj))
        conf = (rj.get("ok") and rj.get("failed") == []) and True
        nconf = (rj.get("conflicts") if "conflicts" in (rj or {}) else None)
        dbj = await b.js(f"({DISK})()", aw=True) or {}
        made = [p for p in dbj.get("files", {}) if "(conflict from" in p and "Android-dead" not in p]
        if len(made) != 2:
            problems.append(f"dirty join minted {len(made)} conflict copies, wanted EXACTLY 2 "
                            f"(the divergent pair) — identical bytes must settle: {made[:5]}")
        else:
            print("  dirty join: identical settled, exactly 2 real conflicts kept both")

        # ---- 2. a real delete on B reaches A: exactly one file, into trash ----------------------
        await b.js("(() => { delete window.__vdisk.files['dir0/f0.bin']; return true; })()")
        r3 = await b.js(f"({SWEEP})()", aw=True) or {}
        print("  B delete sweep:", json.dumps(r3))
        if r3.get("removedRemote") != 1:
            problems.append(f"B's deletion published {r3.get('removedRemote')} tombstones, wanted 1 "
                            f"(held={r3.get('held')})")
        r4 = await a.js(f"({SWEEP})()", aw=True) or {}
        da2 = await a.js(f"({DISK})()", aw=True) or {}
        if da2.get("trash") != ["dir0/f0.bin"]:
            problems.append(f"A trashed {da2.get('trash')}, wanted exactly ['dir0/f0.bin']")
        else:
            print("  deletion propagated: exactly one file, into .pc-trash")

        # ---- 3. THE KILLER: B's scan lies (empty listing) — nothing may be deleted anywhere -----
        await b.js("(() => { window.__vdisk.lying = true; return true; })()")
        r5 = await b.js(f"({SWEEP})()", aw=True) or {}
        print("  B lying sweep:", json.dumps(r5))
        if (r5.get("removedRemote") or 0) > 0:
            problems.append(f"a lying scan published {r5['removedRemote']} deletions — THE bug")
        await b.js("(() => { window.__vdisk.lying = false; return true; })()")
        r6 = await a.js(f"({SWEEP})()", aw=True) or {}
        da3 = await a.js(f"({DISK})()", aw=True) or {}
        if len(da3.get("trash", [])) != 1:
            problems.append(f"after the lying scan, A's trash is {da3.get('trash')} — files were deleted")
        else:
            print("  lying scan deleted nothing anywhere")

        # ---- 3.4 RECONCILE MUST NOT PUT BACK WHAT EVERY DEVICE AGREED TO DELETE -----------------
        # B deleted dir0/f0.bin and A applied it, so A's .pc-trash holds exactly the bytes the
        # tombstone describes. "Restore from trash" would put it back and republish it to B — the
        # rescue button undoing a deletion somebody meant, which is how a folder of deleted files
        # keeps coming back. Reconcile proves it instead: the record is a tombstone whose checksum
        # matches this copy, so the copy is redundant and goes.
        # The reconcile DELETES what it proves, and the restore scenario below needs that same trash.
        # Snapshot it rather than reordering the two — each is testing a different half and neither
        # should be arranged around the other.
        await a.js("(() => { window.__vdisk._keep = Object.assign({}, window.__vdisk.bin); "
                   "return true; })()")
        db0 = await a.js(f"({DISK})()", aw=True) or {}
        if db0.get("bin"):
            rc = await a.js(f"({RECONCILE})()", aw=True) or {}
            print("  A reconcile trash:", json.dumps(rc))
            if rc.get("why"):
                print("     kept because:", json.dumps(rc["why"]))
            if rc.get("err"):
                problems.append(f"reconcile trash threw: {rc['err']}")
            elif rc.get("restored"):
                problems.append("reconcile PUT BACK a file every device agreed to delete: "
                                f"{rc['restored']}")
            elif not rc.get("purged"):
                problems.append(f"reconcile proved nothing and removed nothing: {rc}")
            else:
                db1 = await a.js(f"({DISK})()", aw=True) or {}
                if "dir0/f0.bin" in db1.get("files", {}):
                    problems.append("reconcile restored the deleted file onto the disk")
                elif db1.get("bin"):
                    problems.append(f"reconcile left the proved-redundant copy: {db1['bin'][:4]}")
                else:
                    print("  reconcile removed the agreed deletion and restored nothing")
        else:
            problems.append("nothing in A's trash to reconcile — the delete step did not land")

        await a.js("(() => { window.__vdisk.bin = window.__vdisk._keep; return true; })()")

        # ---- 3.45 THE TRASH IS BROWSED AS A FOLDER ----------------------------------------------
        # It used to be a collapsed block of DATES with a Restore button per date: you could see that
        # "2026-08-19 · 107 files" existed and nothing about WHAT they were, which is the only
        # question somebody standing in front of 107 deleted files is asking. Reported as "Trash on
        # this device is terrible UI, make it a Folder like the regular folders with a list of
        # contents". So it is a directory row in the folder it belongs to, and entering it gives the
        # ordinary listing — driven here through the real DOM, because a redesign that was only read
        # is a redesign that was not checked.
        tr = await a.js("""(async () => {
          window.__trErr = null;
          window.addEventListener('unhandledrejection', e => {
            window.__trErr = String((e.reason && (e.reason.stack || e.reason.message)) || e.reason); });
          window.addEventListener('error', e => { window.__trErr = String(e.message) + ' @' + e.lineno; });
          window.__PC.switchView('blossom');
          for(let i = 0; i < 60 && !document.querySelector('.folder-chip[data-synckey]'); i++)
            await new Promise(r => setTimeout(r, 100));
          const chip = document.querySelector('.folder-chip[data-synckey]');
          if(!chip) return { err: 'no synced-folder chip in Files' };
          chip.click();
          for(let i = 0; i < 80 && !document.querySelector('#bl-grid .file-card, #bl-grid .empty'); i++)
            await new Promise(r => setTimeout(r, 100));
          const door = [...document.querySelectorAll('#bl-grid [data-dir]')]
            .find(el => el.getAttribute('data-dir') === '.pc-trash');
          if(!door) return { err: 'no .pc-trash folder row at the root of the synced folder',
                             saw: [...document.querySelectorAll('#bl-grid [data-dir]')]
                                    .map(e => e.getAttribute('data-dir')).slice(0, 8),
                             root: (window.PCSync.folders()[0] || {}).key,
                             trash: (await window.pcFs.listTrash((window.PCSync.folders()[0]||{}).id) || []).length,
                             err2: window.__trErr,
                             grid: (document.querySelector('#bl-grid') || {}).innerHTML ?
                                   document.querySelector('#bl-grid').innerHTML.slice(0, 300) : 'NO GRID' };
          door.click();
          /* Descend to the files. The trash keeps the engine's own dated sub-folders, so the first
           * screen inside it is directories — which is the point of making it a folder rather than a
           * flat list, and is what a person walks through too. */
          for(let hop = 0; hop < 4; hop++){
            for(let i = 0; i < 80; i++){
              await new Promise(r => setTimeout(r, 100));
              if(document.querySelector('#bl-grid .tr-back, #bl-grid .file-card')) break;
            }
            if(document.querySelector('#bl-grid .tr-back')) break;
            const down = document.querySelector('#bl-grid .file-card.isdir[data-dir]');
            if(!down) break;
            down.click();
          }
          const names = [...document.querySelectorAll('#bl-grid .fname')].map(e => e.textContent);
          return { names, back: document.querySelectorAll('.tr-back').length,
                   gone: document.querySelectorAll('.tr-gone').length,
                   reconcile: !!document.querySelector('#tr-reconcile'),
                   deleteEverywhere: !!document.querySelector('#ss-del'),
                   crumbs: [...document.querySelectorAll('.fx-crumb, .crumb')].map(e => e.textContent) };
        })()""", aw=True) or {}
        if tr.get("err"):
            problems.append(f"trash folder: {tr['err']} saw={tr.get('saw')} "
                            f"root={tr.get('root')!r} trash={tr.get('trash')} "
                            f"grid={tr.get('grid')!r} err2={tr.get('err2')!r}")
        else:
            print("  trash as a folder:", json.dumps({k: tr.get(k) for k in
                                                      ("names", "back", "gone", "reconcile")}))
            if not tr.get("names"):
                problems.append("the trash folder listed no files — it is a folder with no contents")
            elif tr.get("back") != 1 or tr.get("gone") != 1:
                problems.append(f"trash rows carry the wrong verbs: back={tr.get('back')} "
                                f"gone={tr.get('gone')} (want one of each)")
            elif not tr.get("reconcile"):
                problems.append("no Reconcile Trash action inside the trash folder")
            elif tr.get("deleteEverywhere"):
                # Its one action publishes a deletion, and every file here is already deleted —
                # pressing it would tell the other devices to delete files they already deleted,
                # which is how a wave of stale tombstones starts.
                problems.append("the 'Delete on every device' bar is shown INSIDE the trash")
            else:
                print("  the trash browses as a folder: names, two verbs, no delete-everywhere bar")

        # ---- 3.5 RESTORE FROM TRASH MUST STICK --------------------------------------------------
        # "i did restore from trash and it clears then goes right back to restore 172 from trash".
        # Putting a file back was a silent act: the bytes returned to the disk and nothing else
        # changed, so the next sweep re-derived the intent from versions and timestamps — and it
        # derives the opposite, because the restored bytes ARE the bytes the tombstone describes.
        # With this device's journal entry missing (struck by a lost compare-and-swap, cleared by an
        # era change) a hashed scan reads "deleted elsewhere, and this copy is the deleted version"
        # and trashes it straight back, reporting success every round.
        #
        # A's journal is cleared here, which is the real shape of it AND what forces the hashed scan
        # the branch needs — the same re-add the dirty join above models.
        await a.js("""(async () => {
          await window.PCSync.docs.saveIndex('E2EPair', {});
          return true; })()""", aw=True)
        rr = await a.js(f"({RESTORE})()", aw=True) or {}
        print("  A restore from trash:", json.dumps(rr))
        if rr.get("err"):
            problems.append(f"restore from trash threw: {rr['err']}")
        elif not rr.get("done"):
            problems.append(f"restore from trash put nothing back: {rr}")
        da4 = await a.js(f"({DISK})()", aw=True) or {}
        if "dir0/f0.bin" not in da4.get("files", {}):
            problems.append("the restored file is not on A's disk — the sweep undid the restore "
                            f"(trash now {da4.get('bin')})")
        elif da4.get("bin"):
            problems.append(f"restore left {len(da4['bin'])} file(s) in .pc-trash — it went "
                            f"straight back: {da4['bin'][:4]}")
        else:
            print("  restore from trash stuck: the file is back and the trash is empty")
        # AND IT SURVIVES THE NEXT SWEEP, which is the half the person actually sees: the restore
        # reports success, the button clears, and one sweep later the file is back in .pc-trash and
        # the button reads "Restore N from trash" again. A deep sweep, because the branch that
        # undoes it needs the content hash the trash-restored bytes will match.
        r6b = await a.js("""(async () => {
          const f = window.PCSync.folders()[0];
          const rep = await window.PCSync.sweep(f, { manual: true, deep: true });
          return { trashed: (rep.plan && rep.plan.deleteLocal || []).length }; })()""", aw=True) or {}
        da5 = await a.js(f"({DISK})()", aw=True) or {}
        if "dir0/f0.bin" not in da5.get("files", {}) or da5.get("bin"):
            problems.append("the sweep after the restore put the file straight back in the trash "
                            f"({json.dumps(r6b)}, trash={da5.get('bin')})")
        else:
            print("  and the sweep after it left the restore alone")

        # …and the other device gets it back, which is what makes it a restore and not a local undo.
        r7 = await b.js(f"({SWEEP})()", aw=True) or {}
        dbf = await b.js(f"({DISK})()", aw=True) or {}
        if "dir0/f0.bin" not in dbf.get("files", {}):
            problems.append(f"B never got the restored file back ({json.dumps(r7)[:200]})")
        else:
            print("  the restore reached the other device")

        # ---- 4. one drive key, no wrong-key anywhere --------------------------------------------
        ka = await a.js("window.__PC.driveKeyWrapped()")
        kb = await b.js("window.__PC.driveKeyWrapped()")
        if not ka or ka != kb:
            problems.append("the pair ended on two drive keys")
        else:
            print("  one drive key on both devices")
        for who, r in (("A", r4), ("B", r6)):
            for f in (r or {}).get("failed", []):
                if "different key" in f:
                    problems.append(f"{who} hit wrong-key: {f}")
    finally:
        # Leave no trace: the throwaway's sync records are junk rows on a real relay otherwise.
        try:
            await a.js("""(async () => {
              for(const k of ['E2EPair']){
                try{ await window.PCSync.edit.forget(k); }catch(_){}
              } return true; })()""", aw=True)
        except Exception:
            pass
        a.stop()
        b.stop()
    if problems:
        for p in problems:
            print("FAIL ", p)
        return 1
    print("PASS  full sync loop: replicate, delete, survive a lying scan, restore from trash, one key")
    return 0


def main():
    try:
        urllib.request.urlopen(BASE + "/client", timeout=5)
    except Exception as e:
        print(f"SKIP  no instance at {BASE} ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
