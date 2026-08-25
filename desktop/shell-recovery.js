'use strict';

/* Reload every mapped output through the canonical navigator. Calling BrowserWindow.reload() is
 * insufficient: a secondary renderer whose first navigation failed is still about:blank, and a
 * reload faithfully keeps it blank. Kept pure so the release test can begin with that exact state. */
async function recoverSurfaces(surfaces, navigate){
  let count=0;
  for(const surface of surfaces || []){
    const browser=surface && surface.browser;
    if(!browser || browser.isDestroyed()) continue;
    const wc=browser.webContents;
    const url=wc && typeof wc.getURL==='function' ? String(wc.getURL()||'') : String(browser.url||'');
    /* Reload working surfaces in place, one at a time. Navigating every monitor concurrently made
     * both renderer processes rebuild the full client together; under real load one GPU surface
     * remained mapped but black. Only a genuinely uninitialised surface needs canonical navigation. */
    let loaded=false;
    if(url && url!=='about:blank' && wc && typeof wc.reloadIgnoringCache==='function'){
      loaded=await new Promise(resolve=>{
        let done=false;
        const finish=ok=>{ if(done)return; done=true; clearTimeout(timer); resolve(Boolean(ok)); };
        const timer=setTimeout(()=>finish(false),15000);
        if(typeof wc.once==='function'){
          wc.once('did-finish-load',()=>finish(true));
          wc.once('did-fail-load',()=>finish(false));
          wc.once('render-process-gone',()=>finish(false));
        }
        try{ wc.reloadIgnoringCache(); }catch(_){ finish(false); }
      });
    }
    /* A mapped surface is not a healthy surface. The old implementation treated timeout,
     * did-fail-load and render-process-gone as success, then showed the failed renderer. On a
     * two-monitor shell that is literally one working desktop and one permanent black monitor.
     * Canonical navigation is the recovery path for both an uninitialised renderer and a reload
     * that did not finish; it is awaited so "shown" means the navigation settled. */
    if(!loaded){
      try{ await Promise.resolve(navigate(browser)); loaded=true; }catch(_){ loaded=false; }
    }
    if(!loaded) continue;
    browser.show();
    count++;
  }
  return count;
}

module.exports={recoverSurfaces};
