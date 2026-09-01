/* THE MESSAGE YOU JUST SENT MUST BE IN THE THREAD.
 *
 * Reported as "i send dm to user, then the conversation goes blank". The pane renders
 * `dmPeers.get(pk)`, so if our own copy never lands there the thread paints EMPTY — and on a
 * conversation with no history that is a blank screen where the message should be.
 *
 * `ingestWrap` has several honest ways to decline — the unwrap throws (a remote signer that timed
 * out, a busy worker), the id is already in `_wrapTried`, the rumor comes back the wrong kind — and
 * every one of them returned false into a call that ignored the answer.
 *
 * This drives the shipped sendDm with an ingest that refuses, and asserts the echo lands under the
 * right peer with the SAME outer id, so the relay's copy de-duplicates instead of showing twice.
 */
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
function extract(decl){
  const start = source.indexOf(decl);
  if(start < 0) throw new Error('missing: ' + decl);
  let open = source.indexOf('{', start), depth = 0, end = -1;
  for(let i=open; i<source.length; i++){
    if(source[i]==='{') depth++;
    else if(source[i]==='}' && --depth===0){ end=i+1; break; }
  }
  if(end < 0) throw new Error('extract failed: ' + decl);
  return source.slice(start, end);
}

const PK = 'a'.repeat(64);
function run({ ingestReturns }){
  const dmPeers = new Map();
  const calls = { refreshed: 0 };
  const context = {
    dmPeers,
    signer:{ nip17wrap: async (pk, text) => ({ toPeer:{id:'peer-id'}, toSelf:{id:'self-id'} }) },
    Store:{ saveEvent(){} },
    ingestWrap: async () => ingestReturns,
    Relay:{ publish: async () => ({ok:true}), publishTo: async () => 1 },
    _keepDmOpen(){}, _scheduleDmRefresh(){ calls.refreshed++; },
    dmInboxRelays: async () => [], toast(){}, Date, Math, String, setTimeout, Promise,
  };
  vm.runInNewContext(extract('function _dmEcho(pk, text, id){') + '\n'
                   + extract('async function sendDm(pk, text){') + '\nthis.send=sendDm;',
                   context, {filename:'app-dm.js'});
  return { context, dmPeers, calls };
}

// THE REPORT: the ingest refuses, and the thread would otherwise be empty.
{
  const { context, dmPeers, calls } = run({ ingestReturns:false });
  await context.send(PK, 'hello there');
  const thread = dmPeers.get(PK) || [];
  if(thread.length !== 1) throw new Error('a refused ingest left the conversation empty: ' + thread.length);
  if(thread[0].id !== 'self-id') throw new Error('the echo does not carry the wrap id: ' + thread[0].id);
  if(thread[0].mine !== true) throw new Error('the echo is not marked as ours');
  if(thread[0].text !== 'hello there') throw new Error('the echo lost its text');
  if(!calls.refreshed) throw new Error('nothing asked the thread to repaint');
}

// A SUCCESSFUL ingest must not be duplicated by the echo.
{
  const { context, dmPeers } = run({ ingestReturns:true });
  await context.send(PK, 'hello there');
  if((dmPeers.get(PK) || []).length !== 0)
    throw new Error('the echo ran even though ingestWrap accepted the wrap');
}

// THE RELAY'S OWN COPY must de-duplicate against the echo — same outer id, so ingestWrap's
// `arr.find(m=>m.id===ev.id)` recognises it.
{
  const { context, dmPeers } = run({ ingestReturns:false });
  await context.send(PK, 'twice?');
  await context.send(PK, 'twice?');
  const thread = dmPeers.get(PK) || [];
  if(thread.length !== 1) throw new Error('the same message echoed twice: ' + thread.length);
}

console.log('ok');
