/* Execute the shipped publish() guard: a destructive kind-3 shrink is confirmed only for an
 * explicit user edit, and its recovery baseline moves only after relay acceptance. */
import fs from 'node:fs';
const src=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const a=src.indexOf('  async function publish(kind, content, tags, opts){');
const b=src.indexOf('  // A guest tried to do something',a);
if(a<0||b<0) throw Error('publish moved');
const shipped=src.slice(a,b);

const person=n=>String(n).padStart(64,'0');
const old=Array.from({length:1163},(_,i)=>person(i));
const small=Array.from({length:8},(_,i)=>['p',person(i)]);
const settings={followsSafetyCache:old,followsSafetyResetAt:10,followsCount:8};
globalThis.ClientSettings={get:(k,d)=>k in settings?settings[k]:d,set:(k,v)=>{settings[k]=v;}};
globalThis.ME={pubkey:'f'.repeat(64)};
globalThis.GUEST=false; globalThis.signer={}; globalThis._guestPrompt=()=>{};
globalThis._followSafetyMembers=()=>settings.followsSafetyCache;
globalThis.Store={query:()=>[],saveEvent:()=>{},removeEvent:()=>{}};
globalThis.InstEmoji={loaded:true,SC_RE:/$a/}; globalThis._enrichTags=(k,t)=>t;
globalThis.invalidateCounts=()=>{}; globalThis.applySobLive=()=>{};
globalThis.window={}; globalThis.toast=()=>{};
/* `publish` is lifted on its own, so anything it CLOSES OVER has to be supplied here. Fedi-only
   mode added a `_FEDI_SOCIAL_KINDS` read inside it, and without this the harness dies with a bare
   ReferenceError -- which reads as "the follows guard is broken" and is nothing of the sort. Lifted
   from app.js rather than retyped, so the set cannot drift from the one the code uses. */
globalThis._FEDI_SOCIAL_KINDS = (() => {
  const m = /_FEDI_SOCIAL_KINDS\s*=\s*new Set\(\[([^\]]*)\]\)/.exec(src);
  return new Set(m ? m[1].split(',').map(x => Number(x.trim())).filter(n => !Number.isNaN(n)) : []);
})();
globalThis._fediOnly = () => false;
let signed=0,published=0,answer=true,decision=false,questions=[];
globalThis.sign=async(kind,content,tags)=>{signed++;return {id:'new',kind,content,tags,pubkey:ME.pubkey,created_at:200};};
globalThis.Relay={publish:async()=>{published++;return {ok:answer};}};
globalThis.uiConfirm=async(message,opts)=>{questions.push({message,opts});return decision;};

const run=new Function(`${shipped}; return publish;`)();
const result={};

decision=false;
result.cancel=await run(3,'',small,{userFollowEdit:true});
result.cancelSigned=signed; result.cancelPublished=published;
result.cancelBaseline=settings.followsSafetyCache.length;

decision=true; answer=false;
result.failed=await run(3,'',small,{userFollowEdit:true});
result.failureBaseline=settings.followsSafetyCache.length;
result.failureReset=settings.followsSafetyResetAt;

answer=true;
result.success=await run(3,'',small,{userFollowEdit:true});
result.successBaseline=settings.followsSafetyCache.length;
result.successReset=settings.followsSafetyResetAt;
result.question=questions[0];

let autoConfirmCalls=questions.length;
settings.followsSafetyCache=old; settings.followsSafetyResetAt=10;
try{ await run(3,'',small); result.autoBlocked=false; }catch(e){ result.autoBlocked=/shrink guard/.test(String(e)); }
result.autoAsked=questions.length-autoConfirmCalls;
process.stdout.write(JSON.stringify(result));
