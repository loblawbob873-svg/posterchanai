/* PosterChan as the SHELL of a Wayland compositor — the window-management half.
 *
 * WHY A COMPOSITOR AND NOT AN EMBEDDED VIEW. The goal is that a browser and a Steam game appear on
 * the PosterChan desktop, and those two have opposite requirements from an embedder's point of view:
 * a browser could be reparented into our window (X11 XReparentWindow, Win32 SetParent) but a GAME
 * cannot — reparenting costs the direct-rendering path, Vulkan surfaces do not survive it, and any
 * screencast approach adds a copy per frame to the one workload that cannot afford one. The only
 * arrangement where both are true at once is the ordinary one: a compositor owns the screen, the
 * browser and the game are ordinary clients, and PosterChan decides where they go. They are "inside
 * PosterChan" because PosterChan IS the desktop, and the game runs at native speed because nothing
 * is intercepting its frames.
 *
 * AND WE DO NOT WRITE THE COMPOSITOR. sway is wlroots-based, mature, ships XWayland (which is how
 * Steam and most games get on screen at all), and speaks a documented JSON IPC. This file is a
 * client of that IPC — the whole of PosterChan's window control is protocol, not pixels, which is
 * also what makes it testable on a machine with no display at all.
 *
 * THE WIRE FORMAT, because getting it wrong is silent: a 14-byte header of the magic string
 * "i3-ipc", then a uint32 little-endian payload length, then a uint32 little-endian type — followed
 * by that many bytes of JSON. Replies carry the same header. An EVENT is the same shape with the
 * high bit of the type set, which is why the type is read as unsigned: read signed, every event
 * arrives as a large negative number and matches no case.
 */
'use strict';
const net = require('net');
const { spawn } = require('child_process');

const MAGIC = Buffer.from('i3-ipc');
const HEAD = MAGIC.length + 8;

const MSG = { RUN_COMMAND: 0, GET_WORKSPACES: 1, SUBSCRIBE: 2, GET_OUTPUTS: 3, GET_TREE: 4,
              GET_MARKS: 5, GET_BAR_CONFIG: 6, GET_VERSION: 7, GET_SEATS: 101 };
const EVENT_BIT = 0x80000000;
/* Event type numbers, low bits. `window` is the one this shell lives on: a launched app's surface
 * appears as `window::new` and that is the only moment its pid can be tied to a window id. */
const EVENT = { 0: 'workspace', 2: 'mode', 3: 'window', 4: 'barconfig_update', 5: 'binding',
                6: 'shutdown', 7: 'tick', 14: 'input' };

function frame(type, payload){
  const body = Buffer.from(payload == null ? '' : String(payload), 'utf8');
  const buf = Buffer.allocUnsafe(HEAD + body.length);
  MAGIC.copy(buf, 0);
  buf.writeUInt32LE(body.length, MAGIC.length);
  buf.writeUInt32LE(type >>> 0, MAGIC.length + 4);
  body.copy(buf, HEAD);
  return buf;
}

/* A stream, not a message queue. Replies arrive in pieces and several can share one chunk — reading
 * "one chunk is one message" works on a quiet socket and falls apart the moment anything is busy,
 * which is exactly when a shell is doing something interesting. */
function decoder(onMessage){
  let buf = Buffer.alloc(0);
  return (chunk) => {
    buf = buf.length ? Buffer.concat([buf, chunk]) : chunk;
    for(;;){
      if(buf.length < HEAD) return;
      if(!buf.subarray(0, MAGIC.length).equals(MAGIC))
        throw new Error('not an i3-ipc stream — the socket is something else');
      const len = buf.readUInt32LE(MAGIC.length);
      const type = buf.readUInt32LE(MAGIC.length + 4);      // UNSIGNED: events set the high bit
      if(buf.length < HEAD + len) return;
      const body = buf.subarray(HEAD, HEAD + len).toString('utf8');
      buf = buf.subarray(HEAD + len);
      let json = null;
      try{ json = body ? JSON.parse(body) : null; }catch(_){ json = null; }
      onMessage(type, json);
    }
  };
}

/** Flatten sway's tree into the windows a person can see. Containers and workspaces are not apps. */
function flatten(node, out, ws){
  if(!node || typeof node !== 'object') return out;
  const here = node.type === 'workspace' ? node.name : ws;
  const isWindow = (node.type === 'con' || node.type === 'floating_con')
                   && !((node.nodes || []).length) && !((node.floating_nodes || []).length)
                   && (node.app_id || (node.window_properties && node.window_properties.class)
                       || node.window || node.pid);
  if(isWindow){
    const p = node.window_properties || {};
    out.push({
      id: node.id,
      pid: node.pid || 0,
      /* app_id is Wayland; class is X11 through XWayland — Steam and most games are the second, so a
       * shell that reads only app_id sees an unnamed window for every game it launches. */
      app: node.app_id || p.class || p.instance || '',
      title: node.name || p.title || '',
      workspace: here || '',
      focused: !!node.focused,
      fullscreen: !!node.fullscreen_mode,
      floating: node.type === 'floating_con',
      xwayland: !node.app_id && !!(p.class || node.window),
      /* PARKED IN THE SCRATCHPAD — which is where `hide` puts a window, so it is also how the shell
       * can tell that something it believes is on screen is not. sway models the scratchpad as a
       * workspace, so this is a fact already in the tree; nothing extra is asked for it. */
      stashed: here === '__i3_scratch',
      rect: node.rect || null,
    });
  }
  for(const k of ['nodes', 'floating_nodes'])
    for(const c of (node[k] || [])) flatten(c, out, here);
  return out;
}

class WM {
  constructor(sockPath){
    this.path = sockPath || process.env.SWAYSOCK || process.env.I3SOCK || '';
    this.sock = null;
    this.subSock = null;
    this.pending = [];           // FIFO of {type, resolve, reject} — sway answers in order
    this.listeners = new Map();  // event name -> Set(fn)
  }

  available(){ return !!this.path; }

  _connect(){
    if(this.sock) return Promise.resolve(this.sock);
    if(!this.path) return Promise.reject(new Error('no compositor socket — SWAYSOCK is not set'));
    return new Promise((res, rej) => {
      const s = net.createConnection(this.path);
      const feed = decoder((type, json) => {
        const w = this.pending.shift();
        if(w) w.resolve(json);
      });
      s.on('data', (c) => { try{ feed(c); }catch(e){ this._failAll(e); s.destroy(); } });
      s.on('error', (e) => { this._failAll(e); this.sock = null; rej(e); });
      s.on('close', () => { this._failAll(new Error('the compositor closed the connection'));
                            this.sock = null; });
      s.on('connect', () => { this.sock = s; res(s); });
    });
  }

  _failAll(err){
    const q = this.pending; this.pending = [];
    for(const w of q) w.reject(err);
  }

  _send(type, payload){
    return this._connect().then((s) => new Promise((resolve, reject) => {
      this.pending.push({ type, resolve, reject });
      s.write(frame(type, payload));
    }));
  }

  /* A command's reply is an ARRAY of results, one per command in the string, and a failure is
   * reported IN it rather than as an error — `{success:false, error:"..."}` with a perfectly
   * ordinary transport. Read as "it returned, so it worked", every refusal is silent. */
  async command(cmd){
    const r = await this._send(MSG.RUN_COMMAND, cmd);
    const rows = Array.isArray(r) ? r : [];
    const bad = rows.find(x => x && x.success === false);
    if(bad) throw new Error(bad.error || 'the compositor refused: ' + cmd);
    return rows;
  }

  tree(){ return this._send(MSG.GET_TREE, ''); }
  version(){ return this._send(MSG.GET_VERSION, ''); }
  outputs(){ return this._send(MSG.GET_OUTPUTS, ''); }
  workspaces(){ return this._send(MSG.GET_WORKSPACES, ''); }

  async windows(){ return flatten(await this.tree(), [], ''); }

  /* Addressing a window by id is `[con_id=N]`, and it is the only stable handle: a title changes as
   * a page loads and an app_id is shared by every window of an app. */
  focus(id){ return this.command('[con_id=' + Number(id) + '] focus'); }
  /* A WINDOW THAT IS ALREADY GONE SATISFIES A REQUEST TO CLOSE IT.
   *
   * sway answers `No matching node` for a con_id it no longer has, and `command` turns that into a
   * throw — correctly, since a silent refusal is worse. But this call is made from BOTH ways a
   * frame closes: the ✕, and the desktop noticing the APP closed itself, which is exactly the case
   * where the id is stale by the time we ask. The rejection reached the renderer, where nothing was
   * awaiting it, and the client's unhandledrejection handler put "action failed" on screen every
   * time somebody quit firefox from its own menu. The postcondition holds, so this is success. */
  async close(id){
    try{ return await this.command('[con_id=' + Number(id) + '] kill'); }
    catch(e){
      if(/no matching node/i.test(String((e && e.message) || ''))) return [];
      throw e;
    }
  }
  fullscreen(id, on){ return this.command('[con_id=' + Number(id) + '] fullscreen '
                                          + (on === false ? 'disable' : 'enable')); }
  floating(id, on){ return this.command('[con_id=' + Number(id) + '] floating '
                                        + (on === false ? 'disable' : 'enable')); }
  /* STASHED, NOT MINIMISED — the compositor has no such state, and this is what it has instead.
   *
   * A native window ALWAYS floats above the shell's own surface: the shell is a tiled window in the
   * compositor and firefox is a floating one, and no z-order this page can express reaches across
   * that boundary. So an HTML window dragged over a native one goes UNDER it, and minimising a
   * native window by hiding our frame leaves the app itself sitting on the desktop with no title
   * bar. The scratchpad is a real hiding place: the window keeps running, keeps its size, and comes
   * back where it was.
   *
   * `move scratchpad` on a window already there is harmless, and `scratchpad show` cycles when
   * several are hidden — so both are addressed by con_id, never by the bare command. */
  hide(id){ return this.command('[con_id=' + Number(id) + '] move scratchpad'); }
  show(id){ return this.command('[con_id=' + Number(id) + '] scratchpad show'); }

  /* Placement only means anything for a FLOATING window — a tiled one is positioned by the layout,
   * and moving it is silently a no-op. A desktop that places windows makes them floating first. */
  async place(id, x, y, w, h){
    await this.floating(id, true);
    await this.command('[con_id=' + Number(id) + '] resize set '
                       + Math.round(w) + ' ' + Math.round(h));
    return this.command('[con_id=' + Number(id) + '] move absolute position '
                        + Math.round(x) + ' ' + Math.round(y));
  }

  /** Dragging changes position only. Resizing/re-floating the client on every pointer frame makes
   * Firefox repaint and Telegram fall seconds behind the frame; the full place() runs on release. */
  move(id, x, y){
    return this.command('[con_id=' + Number(id) + '] move absolute position '
                        + Math.round(x) + ' ' + Math.round(y));
  }

  /** Subscribe on its OWN socket. sway will not answer ordinary requests on a subscribed one. */
  async subscribe(names){
    if(this.subSock) return;
    const s = net.createConnection(this.path);
    this.subSock = s;
    const feed = decoder((type, json) => {
      if(!(type & EVENT_BIT)) return;                       // the SUBSCRIBE reply itself
      const name = EVENT[type & ~EVENT_BIT] || String(type & ~EVENT_BIT);
      for(const fn of (this.listeners.get(name) || [])) { try{ fn(json); }catch(_){} }
    });
    s.on('data', (c) => { try{ feed(c); }catch(_){ s.destroy(); } });
    s.on('close', () => { this.subSock = null; });
    s.on('error', () => { this.subSock = null; });
    await new Promise((res) => s.on('connect', res));
    s.write(frame(MSG.SUBSCRIBE, JSON.stringify(names || ['window', 'workspace'])));
  }

  on(name, fn){
    if(!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
    return () => this.listeners.get(name).delete(fn);
  }

  /* LAUNCH, AND THEN FIND WHAT WE LAUNCHED.
   *
   * Spawned here rather than through the compositor's `exec` so the pid is ours to keep: sway's
   * `exec` answers success and tells you nothing about what it started. The window is matched by
   * PID and not by title or app_id — a browser's title is the page and its app_id is shared with
   * every other window it already had open, so both of those pick the wrong window on the second
   * launch. Steam is the case that proves it: it starts, forks, and the window that appears belongs
   * to a CHILD, so the match walks the tree for any window whose pid is the one we started or one
   * of its descendants.
   *
   * Detached and with stdio ignored, or the app dies with the shell and a game holds the pipe open
   * until somebody reads it. */
  launch(argv, opts){
    const o = opts || {};
    const child = spawn(argv[0], argv.slice(1), {
      detached: true, stdio: 'ignore', cwd: o.cwd || undefined,
      env: Object.assign({}, process.env, o.env || {}),
    });
    /* AN 'error' EVENT WITH NO LISTENER IS RE-THROWN, and in the main process that is a modal
     * JavaScript error dialog sitting on top of the desktop. It is how a missing binary presented
     * itself: "Firefox ENOENT javascript error" instead of "Firefox is not installed". spawn reports
     * ENOENT asynchronously — after it has returned — so no try/catch around this call can see it,
     * and the listener is the only thing that can. */
    let onFail = null;
    const failed = new Promise((res) => { onFail = res; });
    child.on('error', (e) => onFail(
      e && e.code === 'ENOENT' ? argv[0] + ' is not installed'
      : e && e.code === 'EACCES' ? argv[0] + ' is not executable'
      : String((e && e.message) || e)));
    child.unref();
    return { pid: child.pid, failed };
  }

  /** The window belonging to `pid` (or a child of it), once it exists. Null if it never appears. */
  async waitForWindow(pid, ms, kin){
    const deadline = Date.now() + (ms || 15000);
    const family = new Set([Number(pid)].concat((kin || []).map(Number)));
    for(;;){
      let list = [];
      try{ list = await this.windows(); }catch(_){ list = []; }
      const hit = list.find(w => family.has(Number(w.pid)));
      if(hit) return hit;
      if(Date.now() > deadline) return null;
      await new Promise(r => setTimeout(r, 250));
    }
  }
}

module.exports = { WM, frame, decoder, flatten, MSG, EVENT, EVENT_BIT };
