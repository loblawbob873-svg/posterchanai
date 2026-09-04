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

function createDesktopBottomGuard({backend, shellIds, windows, focus, defer}={}){
  let queued=false;
  const later=defer||((fn)=>setTimeout(fn,0));
  return function onWindowEvent(ev){
    const shell=ev&&ev.wayfireView;
    if(backend!=='wayfire'||!shell||ev.change!=='view-focused'
      ||!new Set(Array.from(shellIds(),Number)).has(Number(shell.id))||queued)return;
    queued=true;
    later(async()=>{
      try{
        const ids=new Set(Array.from(shellIds(),Number));
        const sibling=pickDesktopSibling(await windows(),ids,shell);
        if(sibling)await focus(sibling.id);
      }catch(_){}finally{queued=false;}
    });
  };
}

module.exports={pickDesktopSibling,createDesktopBottomGuard};
