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
    if(url && url!=='about:blank' && wc && typeof wc.reloadIgnoringCache==='function'){
      await new Promise(resolve=>{
        let done=false;
        const finish=()=>{ if(done)return; done=true; clearTimeout(timer); resolve(); };
        const timer=setTimeout(finish,15000);
        if(typeof wc.once==='function'){
          wc.once('did-finish-load',finish);
          wc.once('did-fail-load',finish);
          wc.once('render-process-gone',finish);
        }
        try{ wc.reloadIgnoringCache(); }catch(_){ finish(); }
      });
    }else{
      try{ await Promise.resolve(navigate(browser)); }catch(_){}
    }
    browser.show();
    count++;
  }
  return count;
}

module.exports={recoverSurfaces};
