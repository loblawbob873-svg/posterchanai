const path = require('path');

let mode = 'empty', decrypts = 0, encrypts = 0, publishes = 0;
global.window = global;
global.document = { querySelector(){ return null; }, addEventListener(){} };
global.Relay = {
  async query(){
    return [{ created_at:1, content: mode === 'empty' ? '' : 'valid-ciphertext' }];
  },
};
global.__PC = {
  VIEW:'timeline', ME:{pubkey:'a'.repeat(64)},
  $(){ return null; }, enc:String, toast(){}, async uiConfirm(){ return true; },
  modal(){}, closeModal(){}, isView(){ return false; },
  async nip44dec(){ decrypts++; return '{}'; },
  async nip44enc(){ encrypts++; return 'ciphertext'; },
  async publish(){ publishes++; return {ok:true}; },
};

require(path.resolve(__dirname, '../../static/js/client/budget.js'));

(async()=>{
  let emptyError = '';
  try{ await global.PCBudget.addParsed('rent', 10); }catch(e){ emptyError = e.message; }
  mode = 'valid';
  let largeError = '';
  try{ await global.PCBudget.addParsed('x'.repeat(66000), 10); }catch(e){ largeError = e.message; }
  process.stdout.write(JSON.stringify({emptyError, largeError, decrypts, encrypts, publishes}));
})().catch(e=>{ console.error(e); process.exit(1); });
