/* Runtime regression for the native folder entry point used by Files and the desktop chooser. */
const assert = require('assert');

global.window = global;
global.localStorage = { getItem(){ return null; }, setItem(){} };
global.__PC = {
  VIEW: 'files', ME: null,
  $(){ return null; }, enc(v){ return String(v); }, toast(){},
  authFetch(){ throw new Error('network must not be used'); },
  ensureAiSession(){}, uiPrompt(){}, uiConfirm(){},
};
global.pcHost = {
  pickDirectory(){},
  async list(path){
    if(path === '/gone') { const e = new Error('bridge invocation failed'); e.code = 'ENOENT'; throw e; }
    return { path, entries: [{name:'src',path:path+'/src',dir:true},{name:'index.js',path:path+'/index.js'}] };
  },
};

require('../../static/js/client/code.js');

(async()=>{
  assert(PCCode, 'Code module did not initialise');
  assert.equal(await PCCode.openHostFolder('/project'), true);
  assert.equal(PCCode._state.hostRoot, '/project');
  assert.deepEqual(PCCode._state.tree.map(e=>[e.name,e.dir]), [['src',true],['index.js',false]]);

  PCCode._state.open = [{path:'unsaved.js',text:'changed',disk:''}];
  assert.equal(await PCCode.openHostFolder({path:'/gone'}), false);
  assert.equal(PCCode._state.hostRoot, '/project', 'a failed shortcut replaced the working directory');
  assert.equal(PCCode._state.open.length, 1, 'a failed shortcut discarded the open buffer');

  assert.equal(PCCode._missingPathError({code:'ENOENT'}), true);
  assert.equal(PCCode._missingPathError({cause:{code:'ENOENT'}}), true);
  assert.equal(PCCode._missingPathError(new Error('no such file or directory')), true);
  assert.equal(PCCode._missingPathError(new Error('permission denied')), false);
  console.log('code host folder runtime: ok');
})().catch(e=>{ console.error(e); process.exitCode=1; });
