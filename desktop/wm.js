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
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const MAGIC = Buffer.from('i3-ipc');
const HEAD = MAGIC.length + 8;

const MSG = { RUN_COMMAND: 0, GET_WORKSPACES: 1, SUBSCRIBE: 2, GET_OUTPUTS: 3, GET_TREE: 4,
              GET_MARKS: 5, GET_BAR_CONFIG: 6, GET_VERSION: 7, GET_SEATS: 101 };
const EVENT_BIT = 0x80000000;
/* Event type numbers, low bits. `window` is the one this shell lives on: a launched app's surface
 * appears as `window::new` and that is the only moment its pid can be tied to a window id. */
const EVENT = { 0: 'workspace', 1: 'output', 2: 'mode', 3: 'window', 4: 'barconfig_update', 5: 'binding',
                6: 'shutdown', 7: 'tick', 14: 'input' };

/* Recovery/diagnostic launches do not necessarily inherit Sway's environment.  The shell launcher
 * normally repairs it, but the desktop process must not turn one missing variable into one black
 * monitor: main.js asks the WM for outputs before it creates the companion surfaces.  Search only
 * this uid's private runtime directory and try newest sockets first; _connect still proves liveness,
 * so an unclean compositor restart cannot select a dead filename. */
function compositorSockets(explicit){
  if(explicit) return [String(explicit)];
  const inherited=process.env.SWAYSOCK||process.env.I3SOCK||'';
  if(inherited) return [inherited];
  if(process.platform!=='linux') return [];
  const uid=typeof process.getuid==='function'?process.getuid():null;
  const runtime=process.env.XDG_RUNTIME_DIR||(uid==null?'':'/run/user/'+uid);
  if(!runtime) return [];
  try{
    return fs.readdirSync(runtime).filter(n=>/^sway-ipc\.\d+\.\d+\.sock$/.test(n))
      .map(n=>{const p=path.join(runtime,n);let at=0;try{const s=fs.statSync(p);if(!s.isSocket())return null;at=s.mtimeMs||0;}catch(_){return null;}return {p,at};})
      .filter(Boolean).sort((a,b)=>b.at-a.at).map(x=>x.p);
  }catch(_){ return []; }
}

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

/* ── THE SHELL'S WORK AREA, PUBLISHED BY THE RENDERER ───────────────────────────────────────────
 *
 * The taskbar is drawn at the bottom of the shell's own surface and the shell is the tiled window
 * every native app floats above, so a window over that band hides it outright. No compositor here
 * knows the band exists: reserving one means a layer-shell exclusive zone, and an Electron toplevel
 * cannot make one — Wayfire duly answers its whole output as the work area. The only process that
 * can measure the bar is the page drawing it, at this zoom on this display, so it sends the answer
 * down and these two functions are how the compositor side remembers it.
 *
 * Shared between both backends on purpose: a rule one compositor obeys and the other does not is a
 * rule that silently stops existing the moment the machine changes compositor. */
function rememberWorkArea(store, area){
  const a=area||{};
  const rect={x:Math.round(Number(a.x)||0), y:Math.round(Number(a.y)||0),
              w:Math.round(Number(a.w)||0), h:Math.round(Number(a.h)||0),
              reserve:Math.max(0,Math.round(Number(a.reserve)||0))};
  if(!(rect.w>0)||!(rect.h>0)) return Array.isArray(store)?store:[];
  /* One area per output, keyed by its origin: a second shell renderer publishes the other screen's,
   * and a display change republishes rather than accumulating. */
  return (Array.isArray(store)?store:[]).filter(x=>!(x.x===rect.x&&x.y===rect.y)).concat([rect]);
}
/** The published area for one output rectangle, or null when nothing has published one — and null
 *  is deliberately not "the output": a caller that has no measurement must keep behaving as it did
 *  before this existed, not act on a work area somebody else's monitor reported. */
function workAreaFor(store, outputRect){
  const b=outputRect||{}, l=Number(b.x)||0, t=Number(b.y)||0;
  const r=l+(Number(b.width)||0), d=t+(Number(b.height)||0);
  for(const a of (Array.isArray(store)?store:[]))
    if(a.x>=l && a.y>=t && a.x+a.w<=r && a.y+a.h<=d) return a;
  return null;
}

/* A compositor is the final authority on whether a native surface is on-screen. The HTML frame can
 * briefly report a stale/oversized rectangle during monitor handoff; accepting that rectangle put
 * Telegram hundreds of pixels below the panel and made its title bar unreachable. Choose the
 * output containing the requested centre (nearest when it lands in a layout gap), cap the size to
 * that output, and clamp the final top-left. Live drag still uses move() unchanged, so crossing an
 * edge remains fluid; this is the release-time commit. */
function clampRectToOutputs(rect, outputs){
  const rows=(Array.isArray(outputs)?outputs:[]).filter(o=>o && o.active!==false && o.rect
    && Number(o.rect.width)>0 && Number(o.rect.height)>0);
  if(!rows.length) return rect;
  const cx=Number(rect.x)+Math.max(0,Number(rect.w))/2;
  const cy=Number(rect.y)+Math.max(0,Number(rect.h))/2;
  let best=null;
  for(const o of rows){
    /* The taskbar band is not somewhere a window may be placed, so it is not part of the rectangle
     * a placement is clamped into. `o.work` is the renderer's measurement; without one this is the
     * output, exactly as before. */
    const b=o.work?{x:o.work.x,y:o.work.y,width:o.work.w,height:o.work.h}:o.rect;
    const l=Number(b.x)||0, t=Number(b.y)||0, r=l+Number(b.width), d=t+Number(b.height);
    const dx=cx<l?l-cx:cx>r?cx-r:0, dy=cy<t?t-cy:cy>d?cy-d:0, dist=dx*dx+dy*dy;
    if(!best || dist<best.dist) best={dist,l,t,r,d};
  }
  const w=Math.min(Math.max(1,Math.round(Number(rect.w)||1)),best.r-best.l);
  const h=Math.min(Math.max(1,Math.round(Number(rect.h)||1)),best.d-best.t);
  return {x:Math.min(Math.max(Math.round(Number(rect.x)||0),best.l),best.r-w),
          y:Math.min(Math.max(Math.round(Number(rect.y)||0),best.t),best.d-h),w,h};
}

/** Expand a launched process into all descendants described by `{pid, ppid}` rows. */
function pidFamily(roots, rows){
  const family=new Set((roots||[]).map(Number).filter(Number.isFinite));
  let changed=true;
  while(changed){
    changed=false;
    for(const row of rows||[]){
      const pid=Number(row&&row.pid),ppid=Number(row&&row.ppid);
      if(Number.isFinite(pid)&&family.has(ppid)&&!family.has(pid)){family.add(pid);changed=true;}
    }
  }
  return family;
}

/* Linux process names may contain spaces and parentheses, so split /proc/<pid>/stat only after its
 * final ')'. The first field after it is state and the second is the parent pid. */
function procParents(){
  if(process.platform!=='linux')return [];
  const out=[];
  try{
    for(const name of fs.readdirSync('/proc')){
      if(!/^\d+$/.test(name))continue;
      try{
        const stat=fs.readFileSync('/proc/'+name+'/stat','utf8'),end=stat.lastIndexOf(')');
        if(end<0)continue;
        const fields=stat.slice(end+1).trim().split(/\s+/),ppid=Number(fields[1]);
        if(Number.isFinite(ppid))out.push({pid:Number(name),ppid});
      }catch(_){}
    }
  }catch(_){}
  return out;
}

class WM {
  constructor(sockPath){
    this.paths = compositorSockets(sockPath);
    this.path = this.paths[0] || '';
    this.sock = null;
    this.connecting = null;
    this.subSock = null;
    this.subConnecting = null;
    this.subNames = [];
    this.subRetry = null;
    this.pending = [];           // FIFO of {type, resolve, reject} — sway answers in order
    this._work = [];             // work areas published by the shell renderers (see rememberWorkArea)
    this.listeners = new Map();  // event name -> Set(fn)
    this.moves = new Map();      // con_id -> latest-wins drag queue (never replay stale positions)
  }

  available(){ return this.paths.length > 0; }

  _connect(){
    if(this.sock) return Promise.resolve(this.sock);
    if(this.connecting) return this.connecting;
    /* SAY WHAT IS TRUE ON THE MACHINE READING IT. This named SWAYSOCK, and Sway has been gone
     * since the Wayfire migration -- so the Windows build's System Settings answered "Could not
     * read displays: no compositor socket -- SWAYSOCK is not set", naming an environment variable
     * from a compositor this app no longer ships, on an operating system that has never had one.
     * Reported as "since we are not using sway anymore, we need to make sure anything we had for
     * sway is changed ... that message concerns me". The honest sentence names no variable: there
     * is no window manager here to ask, which is equally true of Windows, macOS, a plain browser
     * and a Linux desktop that is not PosterChanOS. */
    if(!this.paths.length) return Promise.reject(new Error('this machine has no window manager to ask'));
    this.connecting=(async()=>{
      let last=null;
      for(const candidate of this.paths){
        try{ const s=await this._connectPath(candidate); this.path=candidate; return s; }
        catch(e){ last=e; }
      }
      throw last||new Error('no live compositor socket');
    })().finally(()=>{this.connecting=null;});
    return this.connecting;
  }

  _connectPath(candidate){
    return new Promise((res, rej) => {
      const s = net.createConnection(candidate);
      let settled=false;
      const feed = decoder((type, json) => {
        const w = this.pending.shift();
        if(w) w.resolve(json);
      });
      s.on('data', (c) => { try{ feed(c); }catch(e){ this._failAll(e); s.destroy(); } });
      s.on('error', (e) => { this._failAll(e); if(this.sock===s)this.sock=null;
                             if(!settled){settled=true;rej(e);} });
      s.on('close', () => { this._failAll(new Error('the compositor closed the connection'));
                            if(this.sock===s)this.sock=null; });
      s.on('connect', () => { if(settled)return;settled=true;this.sock=s;res(s); });
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
  /* THE WINDOW CHROME, APPLIED AT RUNTIME — not left to a config file.
   *
   * These same lines are in os/overlay/.../files/sway.config and os/gentoo.sh, and that is where
   * they were ONLY. A config file is read once, when the session starts, and portage does not
   * silently replace an existing one on upgrade (etc-update exists precisely so it does not) — so
   * every fix to the native palette reached a FRESHLY PROVISIONED machine and no other. Reported,
   * correctly and repeatedly, as Firefox and Telegram still not matching PosterChan's windows days
   * after it was "fixed".
   *
   * swaymsg applies them to the running compositor, so an installed machine gets them on the next
   * shell start rather than on the next reinstall. Idempotent, and harmless where the config
   * already agrees. `tests/test_native_window_snap.py` checks these against the shipped config so
   * the two copies cannot drift.
   */
  static CHROME = [
    'default_floating_border normal 3',
    'titlebar_border_thickness 0',
    'titlebar_padding 8 6',
    'client.focused          #241438 #241438 #f7f4ff #16d9e3 #16d9e3',
    'client.focused_inactive #171222 #171222 #bcb3cb #4b3a65 #4b3a65',
    'client.unfocused        #100d18 #100d18 #8f879c #30263f #30263f',
    'client.urgent           #7a2145 #7a2145 #ffffff #ff4f8b #ff4f8b',
  ];

  async applyChrome(){
    for(const line of WM.CHROME){
      // One at a time: sway answers per command, and a single failure must not drop the rest.
      try{ await this.command(line); }catch(_){ }
    }
    return true;
  }

  async command(cmd){
    const r = await this._send(MSG.RUN_COMMAND, cmd);
    const rows = Array.isArray(r) ? r : [];
    const bad = rows.find(x => x && x.success === false);
    if(bad) throw new Error(bad.error || 'the compositor refused: ' + cmd);
    return rows;
  }

  tree(){ return this._send(MSG.GET_TREE, ''); }
  version(){ return this._send(MSG.GET_VERSION, ''); }
  /* See rememberWorkArea: only the renderer can measure the taskbar, so it publishes the area and
   * every rectangle decided here is clamped into it. */
  setWorkArea(area){ this._work = rememberWorkArea(this._work, area); return Promise.resolve(true); }
  async outputs(){
    const rows = await this._send(MSG.GET_OUTPUTS, '');
    for(const row of (Array.isArray(rows) ? rows : [])) row.work = workAreaFor(this._work, row && row.rect);
    return rows;
  }
  workspaces(){ return this._send(MSG.GET_WORKSPACES, ''); }

  async assignShell(id, assignment){
    const shellDisplays=require('./shell-displays.js');
    for(const cmd of shellDisplays.placement(id,assignment))await this.command(cmd);
    return true;
  }
  moveToAssignment(id, assignment){
    return this.command('[con_id='+Number(id)+'] move container to workspace number '+String(assignment&&assignment.workspace||''));
  }
  decorate(id, hosted){
    return this.command('[con_id='+Number(id)+'] '+(hosted?'border none':'border normal 3')+', sticky disable');
  }
  /* SWAY ALREADY DOES THIS AND CANNOT BE ASKED TO DO IT AGAIN. The shell is the TILED window and
   * every application floats, and sway paints floating over tiled unconditionally — so the desktop
   * is structurally below and there is no command that would improve on it. Answering FALSE (rather
   * than throwing, or pretending) is what lets main.js sink a Wayfire shell and leave a Sway one
   * alone with one call site instead of a backend test at each. */
  keepBelow(){ return Promise.resolve(false); }

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
  /* Restore in ONE compositor transaction. `scratchpad show` on its own briefly gives some clients
   * scratchpad geometry; terminals receive that resize and redraw as a tiny rectangle before the
   * later place() expands them. One chained criterion preserves the hosted body rectangle. */
  async restore(id, x, y, w, h){
    let at={x,y,w,h};
    try{ at=clampRectToOutputs(at,await this.outputs()); }catch(_){}
    return this.command('[con_id='+Number(id)+'] scratchpad show, floating enable, resize set '
      +Math.round(at.w)+' '+Math.round(at.h)+', move absolute position '
      +Math.round(at.x)+' '+Math.round(at.y));
  }

  /* Placement only means anything for a FLOATING window — a tiled one is positioned by the layout,
   * and moving it is silently a no-op. A desktop that places windows makes them floating first. */
  async place(id, x, y, w, h){
    let at={x,y,w,h};
    try{ at=clampRectToOutputs(at,await this.outputs()); }catch(_){}
    /* One Sway transaction, for the same reason restore() above is one.  `floating enable` may
     * assign the client's remembered/default floating geometry; sent separately, Foot receives
     * and damages that intermediate size before the next IPC round trip applies the hosted body.
     * Sustained Codex/Claude output makes that extra configure visible as a full-window flash.
     * A comma chain is committed together and never exposes the throwaway framebuffer size. */
    return this.command('[con_id=' + Number(id) + '] floating enable, resize set '
      + Math.round(at.w) + ' ' + Math.round(at.h) + ', move absolute position '
      + Math.round(at.x) + ' ' + Math.round(at.y));
  }

  /* Menus map transparent (sway.config) because Wayland initially centres every new toplevel.
   * Geometry and reveal must be one compositor transaction: revealing in a later command merely
   * trades the centre flash for a correctly-positioned blank flash. */
  async placeAndReveal(id, x, y, w, h){
    let at={x,y,w,h};
    try{ at=clampRectToOutputs(at,await this.outputs()); }catch(_){}
    return this.command('[con_id=' + Number(id) + '] floating enable, resize set '
      + Math.round(at.w) + ' ' + Math.round(at.h) + ', move absolute position '
      + Math.round(at.x) + ' ' + Math.round(at.y) + ', opacity set 1');
  }

  /* Commit a cross-output handoff INSIDE the destination output. Moving a floating container to a
   * workspace preserves its old absolute coordinates; on unequal or offset monitors that can leave
   * Steam half outside the new output. This uses the destination explicitly (not nearest-output
   * inference), clamps both size and position with a visible margin, and returns the final rect for
   * regression tests and diagnostics. */
  async placeOnOutput(id, outputRect, direction){
    const b=outputRect||{}, l=Number(b.x)||0, t=Number(b.y)||0;
    const ow=Math.max(1,Number(b.width)||1), oh=Math.max(1,Number(b.height)||1), gap=12;
    let cur=(await this.windows()).find(x=>Number(x.id)===Number(id));
    const r=(cur&&cur.rect)||{};
    const w=Math.min(Math.max(320,Number(r.width)||Math.round(ow*.72)),Math.max(1,ow-gap*2));
    const h=Math.min(Math.max(220,Number(r.height)||Math.round(oh*.72)),Math.max(1,oh-gap*2));
    let x=Math.min(Math.max(Number(r.x)||l+gap,l+gap),l+ow-w-gap);
    let y=Math.min(Math.max(Number(r.y)||t+gap,t+gap),t+oh-h-gap);
    if(direction==='right') x=l+gap; else if(direction==='left') x=l+ow-w-gap;
    else if(direction==='down') y=t+gap; else if(direction==='up') y=t+oh-h-gap;
    /* One compositor transaction is essential for redraw-heavy clients such as Foot. Separate
     * floating/resize/move requests expose the resized window on the source output before the move,
     * deliver an avoidable SIGWINCH, and make a streaming TUI visibly redraw twice during handoff. */
    await this.command('[con_id='+Number(id)+'] floating enable, resize set '
      +Math.round(w)+' '+Math.round(h)+', move absolute position '+Math.round(x)+' '+Math.round(y));
    return {x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h)};
  }

  /* PosterChan's taskbar can offer the same layouts for compositor-owned applications without
   * constructing a second HTML frame around them. Geometry is based on the output containing the
   * window centre and reserves the shell taskbar at the bottom. */
  async snap(id, zone){
    const row=(await this.windows()).find(w=>Number(w.id)===Number(id));
    if(!row || !row.rect) return false;
    const outputs=(await this.outputs()).filter(o=>o&&o.active!==false&&o.rect);
    const cx=Number(row.rect.x)+(Number(row.rect.width)||0)/2;
    const cy=Number(row.rect.y)+(Number(row.rect.height)||0)/2;
    let out=outputs.find(o=>cx>=o.rect.x&&cx<o.rect.x+o.rect.width&&cy>=o.rect.y&&cy<o.rect.y+o.rect.height);
    if(!out) out=outputs[0];
    if(!out) return false;
    /* `height-72` was the taskbar, guessed: 72 is the 48px bar at one particular zoom, and a desk
     * running at another gets a maximised window that stops short of the bar — or, scaled the other
     * way, one that covers it. The published area is the measurement; the constant is the fallback
     * for a renderer that has not reported one. */
    const wa=out.work;
    const b=wa?{x:wa.x,y:wa.y,width:wa.w,height:wa.h}:out.rect;
    const h=wa?Number(b.height):Math.max(1,Number(b.height)-72), half=Math.floor(Number(b.width)/2);
    let x=Number(b.x), y=Number(b.y), w=Number(b.width), height=h;
    if(zone==='left') w=half;
    else if(zone==='right'){ w=Number(b.width)-half; x+=half; }
    else if(/^(top|bottom)-(left|right)$/.test(zone)){
      const parts=zone.split('-'), hh=Math.floor(h/2);
      w=parts[1]==='left'?half:Number(b.width)-half;
      if(parts[1]==='right')x+=half;
      height=parts[0]==='top'?hh:h-hh;
      if(parts[0]==='bottom')y+=hh;
    }else if(zone!=='max') return false;
    return this.place(id,x,y,w,height);
  }

  /** Dragging changes position only, and LATEST WINS.
   *
   * Pointer frames arrive faster than sway IPC acknowledgements. Sending every one through the
   * ordinary FIFO makes Firefox trail its frame, then replay obsolete positions after the mouse
   * stops. One command may be in flight per window; while it is, all intermediate positions collapse
   * into the newest one. The full place() still runs on release to commit size and final geometry. */
  move(id, x, y){
    const key = Number(id);
    let state = this.moves.get(key);
    const at = { x: Math.round(x), y: Math.round(y) };
    if(state){ state.next = at; return state.promise; }
    state = { next: at, promise: null };
    state.promise = (async () => {
      while(state.next){
        const here = state.next; state.next = null;
        await this.command('[con_id=' + key + '] move absolute position ' + here.x + ' ' + here.y);
      }
    })().finally(() => { if(this.moves.get(key) === state) this.moves.delete(key); });
    this.moves.set(key, state);
    return state.promise;
  }

  /* A monitor handoff is an ordering barrier. Drop queued source-output coordinates and wait for
   * the one command already on the wire; otherwise it can finish after the workspace move and pull
   * the window straight back to its original monitor. */
  finishMove(id){
    const state=this.moves.get(Number(id));
    if(!state) return Promise.resolve();
    state.next=null;
    return state.promise || Promise.resolve();
  }

  /** Subscribe on its OWN socket. sway will not answer ordinary requests on a subscribed one. */
  async subscribe(names){
    const requested=Array.isArray(names)&&names.length ? names : ['window','workspace'];
    this.subNames=[...new Set([...this.subNames,...requested])];
    if(this.subSock) return;
    if(this.subConnecting) return this.subConnecting;
    clearTimeout(this.subRetry);this.subRetry=null;
    this.subConnecting=this._openSubscription().finally(()=>{this.subConnecting=null;});
    return this.subConnecting;
  }

  async _openSubscription(){
    // Resolve a live recovered socket first.  Creating against this.path directly used the first
    // stale filename and produced ERR_SOCKET_BAD_PORT when no environment variable was inherited.
    await this._connect();
    const s = net.createConnection(this.path);
    let lost=false, connected=false;
    const retry=()=>{
      if(lost)return;lost=true;
      if(this.subSock===s)this.subSock=null;
      /* main.js installs its forwarding listeners once for the lifetime of the process.  Losing
       * this socket must therefore heal HERE: main's `__forwarding` guard correctly prevents a
       * second listener set, but it also means no renderer will call subscribe again. */
      if(!this.subRetry&&this.subNames.length){
        this.subRetry=setTimeout(()=>{
          this.subRetry=null;
          this.subscribe(this.subNames).catch(()=>this._scheduleSubscriptionRetry());
        },250);
        if(this.subRetry.unref)this.subRetry.unref();
      }
    };
    const feed = decoder((type, json) => {
      if(!(type & EVENT_BIT)) return;                       // the SUBSCRIBE reply itself
      const name = EVENT[type & ~EVENT_BIT] || String(type & ~EVENT_BIT);
      for(const fn of (this.listeners.get(name) || [])) { try{ fn(json); }catch(_){} }
    });
    s.on('data', (c) => { try{ feed(c); }catch(_){ s.destroy(); } });
    s.on('close', retry);
    s.on('error', retry);
    await new Promise((res,rej) => {
      s.once('connect',()=>{connected=true;res();});
      s.once('error',e=>{if(!connected)rej(e);});
    });
    if(lost)throw new Error('compositor subscription closed while connecting');
    this.subSock = s;
    s.write(frame(MSG.SUBSCRIBE, JSON.stringify(this.subNames)));
  }

  _scheduleSubscriptionRetry(){
    if(this.subRetry||!this.subNames.length)return;
    this.subRetry=setTimeout(()=>{
      this.subRetry=null;
      this.subscribe(this.subNames).catch(()=>this._scheduleSubscriptionRetry());
    },500);
    if(this.subRetry.unref)this.subRetry.unref();
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
    const roots = [Number(pid)].concat((kin || []).map(Number));
    let family = pidFamily(roots,[]);
    for(;;){
      let list = [];
      try{ list = await this.windows(); }catch(_){ list = []; }
      /* Browsers and Telegram routinely fork before mapping their surface. Refresh ancestry each
       * pass: a one-time snapshot taken immediately after spawn misses children created later. */
      family = pidFamily([...family],procParents());
      const hit = list.find(w => family.has(Number(w.pid)));
      if(hit) return hit;
      if(Date.now() > deadline) return null;
      await new Promise(r => setTimeout(r, 250));
    }
  }

  /** A newly mapped window matching `accept`, excluding every surface present before launch. */
  async waitForNewWindow(before, ms, accept){
    const old = new Set((before || []).map(Number));
    const deadline = Date.now() + (ms || 15000);
    for(;;){
      let list=[];try{list=await this.windows();}catch(_){list=[];}
      const hit=list.find(w=>!old.has(Number(w.id))&&(!accept||accept(w)));
      if(hit)return hit;
      if(Date.now()>deadline)return null;
      await new Promise(r=>setTimeout(r,250));
    }
  }
}

const SwayWM=WM;
/* Backend selection is environment-only and therefore rollback-safe: a Sway session exports
 * SWAYSOCK, a Wayfire session exports WAYFIRE_SOCKET. The renderer sees the same theme-neutral API. */
function DesktopWM(sockPath){
  if(!sockPath&&process.env.WAYFIRE_SOCKET&&!process.env.POSTERCHAN_WM_FORCE_SWAY){
    const {WayfireWM}=require('./wm-wayfire.js');return new WayfireWM();
  }
  return new SwayWM(sockPath);
}
DesktopWM.CHROME=SwayWM.CHROME;
module.exports = { WM:DesktopWM, SwayWM, frame, decoder, flatten, clampRectToOutputs, pidFamily,
                   rememberWorkArea, workAreaFor, MSG, EVENT, EVENT_BIT };
