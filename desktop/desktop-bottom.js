'use strict';

/* A desktop surface is a background, not an ordinary top-level window. Wayfire 0.10 has no
 * lower-view IPC method, so clicking Electron's full-output shell can otherwise obscure every
 * normal window. Restore only the most recently focused normal view on that output/workspace.
 * This deliberately does not make applications always-on-top and therefore preserves their z
 * order and normal click-to-raise behaviour. */
function pickDesktopSibling(rows, shellIds, shell){
  const ids=new Set(Array.from(shellIds||[], Number));
  const workspace=String((shell&&shell.workspace)??'');
  return (rows||[]).filter(row=>row&&Number.isFinite(Number(row.id))
    && !ids.has(Number(row.id)) && !row.stashed
    && String(row.workspace??'')===workspace)
    .sort((a,b)=>(Number(b.focusTime)||0)-(Number(a.focusTime)||0))[0]||null;
}

/* EXCEPT WHEN THE DESKTOP HAS A WINDOW OF ITS OWN ON SCREEN, and this guard is what made that
 * impossible for as long as it existed.
 *
 * System Settings, Task Manager, Virtual Machines, Remote Desktop and folders are drawn INSIDE the
 * shell surface -- they are not toplevels -- so the only way to give one the keyboard is to focus
 * the desktop. This handler answered every such focus by focusing an application again, within
 * milliseconds, unconditionally. Measured on the laptop over raw Wayfire IPC, with a plain `foot`
 * window as the only other view: `window-rules/focus-view` on the shell moved its focus timestamp
 * and foot's moved 1.2ms later, every time -- and with foot CLOSED the shell held focus
 * indefinitely, which is what rules out the pointer and the compositor's own policy.
 *
 * That is "Running Global then clicking on System Settings causes the windows to conflict, System
 * settings never gets focus" and "social is stuck behind terminal and can't move". Neither was
 * fixable from the renderer: it was asking for the front through `pc:wm:shell-front`, main was
 * recording the wish, `sinkShellSurfaces` was honouring it -- and this second, older mechanism,
 * written before any of that existed, was overriding all of it. Two things lowered the desktop and
 * only one of them had ever heard of the exception.
 *
 * `wantsFront` is that exception. It is deliberately a callback rather than a flag: the set it
 * reads is written by IPC from whichever renderer owns the surface, and it changes between the
 * event and the deferred call that acts on it. */
function createDesktopBottomGuard({backend, shellIds, windows, focus, defer, wantsFront}={}){
  let queued=false;
  const later=defer||((fn)=>setTimeout(fn,0));
  const asked=(id)=>{ try{ return typeof wantsFront==='function' && wantsFront(Number(id))===true; }
                      catch(_){ return false; } };
  return function onWindowEvent(ev){
    const shell=ev&&ev.wayfireView;
    if(backend!=='wayfire'||!shell||ev.change!=='view-focused'
      ||!new Set(Array.from(shellIds(),Number)).has(Number(shell.id))||queued)return;
    if(asked(shell.id))return;
    queued=true;
    later(async()=>{
      try{
        /* Re-checked here, not only at the event: `windows()` is a round trip, and the renderer
         * publishes the wish from the same click that produced this focus. Asked only at the top,
         * a desktop window opened one tick before the IPC landed was still shoved behind. */
        if(asked(shell.id))return;
        const ids=new Set(Array.from(shellIds(),Number));
        const sibling=pickDesktopSibling(await windows(),ids,shell);
        if(asked(shell.id))return;
        if(sibling)await focus(sibling.id);
      }catch(_){}finally{queued=false;}
    });
  };
}

module.exports={pickDesktopSibling,createDesktopBottomGuard};
