'use strict';

/* Reload every mapped output through the canonical navigator. Calling BrowserWindow.reload() is
 * insufficient: a secondary renderer whose first navigation failed is still about:blank, and a
 * reload faithfully keeps it blank. Kept pure so the release test can begin with that exact state. */
function recoverSurfaces(surfaces, navigate){
  let count=0;
  for(const surface of surfaces || []){
    const browser=surface && surface.browser;
    if(!browser || browser.isDestroyed()) continue;
    navigate(browser);
    browser.show();
    count++;
  }
  return count;
}

module.exports={recoverSurfaces};
