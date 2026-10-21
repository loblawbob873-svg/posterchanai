// THE SHIPPED concord-git.js CORE, run against real signed NIP-34 events.
//
// Every rule here is Armada's (src/lib/gitActivity.ts, ci.ts, gitRepositoryResolver.ts,
// gitworkshopUrl.ts), so an expected value below is what ARMADA would show or publish for the same
// input — not what this file happens to do. A rule that drifts makes the two clients disagree about
// the same channel.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
vm.runInThisContext(fs.readFileSync('static/vendor/nostr/nostr.bundle.js','utf8'));
const ctx=vm.createContext({console,URL,TextEncoder,TextDecoder,setTimeout,clearTimeout});
vm.runInContext(fs.readFileSync('static/js/client/concord-git.js','utf8'),ctx);
const C=ctx.PCConcordGit.core,NT=NostrTools,nip19=NT.nip19;
const plain=v=>JSON.parse(JSON.stringify(v));
const key=()=>{const sk=NT.generateSecretKey();return {sk,pk:NT.getPublicKey(sk)};};
let clock=1_700_000_000;
const sign=(who,kind,tags,content='',at)=>NT.finalizeEvent({kind,created_at:at??++clock,tags,content},who.sk);
const owner=key(),maint=key(),alice=key(),bob=key(),stranger=key();
const coord='30617:'+owner.pk+':ngit',fork='30617:'+alice.pk+':ngit-fork';

// ── addresses ────────────────────────────────────────────────────────────────────────────────────
assert.equal(C.parseAddress(coord).identifier,'ngit');
assert.equal(C.parseAddress('30617:'+owner.pk+':a:b').identifier,'a:b','identifier keeps its colons');
assert.equal(C.parseAddress('30617:'+owner.pk.toUpperCase()+':x'),undefined,'uppercase owner refused');
assert.equal(C.parseAddress('30023:'+owner.pk+':x'),undefined);
assert.equal(C.parseAddress('30617:'+owner.pk+':'),undefined,'empty identifier refused');
assert.equal(C.normRelay('relay.ngit.dev/'),'wss://relay.ngit.dev');
assert.equal(C.normRelay('https://x.example'),undefined,'an https URL is not a relay');
assert.equal(C.normRelay('localhost:7777'),'ws://localhost:7777');

// ── announcements: newest wins; maintainers exclude the owner; only https clone/web ─────────────
const annOld=sign(owner,30617,[['d','ngit'],['name','ngit'],['relays','wss://relay.ngit.dev','wss://relay.ngit.dev/'],['maintainers',maint.pk,owner.pk],['clone','https://x/ngit.git','git@x:ngit'],['web','http://insecure']]);
const A1=C.parseAnnouncement(annOld);
assert.deepEqual(plain(A1.relays),['wss://relay.ngit.dev'],'multi-value relays tag, deduped after normalizing');
assert.deepEqual(plain(A1.maintainers),[maint.pk]);
assert.deepEqual(plain(A1.cloneUrls),['https://x/ngit.git']);
assert.deepEqual(plain(A1.webUrls),[]);

// ── the timeline: interval gating, any-a matching, trust, deletions ─────────────────────────────
const t0=clock;
const before=sign(alice,1621,[['a',coord],['subject','opened before attaching']],'x',t0+1);
const attachedAt=t0+10;
const issue=sign(alice,1621,[['a',fork],['a',coord,'wss://relay.ngit.dev'],['subject','  Crash on push  '],['t','Bug'],['t','bug']],'body',attachedAt+1);
const comment=sign(bob,1111,[['E',issue.id,'',alice.pk],['K','1621'],['P',alice.pk],['e',issue.id,'',alice.pk],['k','1621'],['p',alice.pk]],'me too',attachedAt+2);
const strangerStatus=sign(stranger,1632,[['e',issue.id,'','root']],'',attachedAt+3);
const maintStatus=sign(maint,1632,[['e',issue.id,'','root'],['a',coord]],'',attachedAt+4);
const lateComment=sign(bob,1111,[['E',issue.id,'',alice.pk],['K','1621']],'after detach',attachedAt+50);
const deletedComment=sign(bob,1111,[['E',issue.id,'',alice.pk],['K','1621']],'oops',attachedAt+5);
const delByAuthor=sign(bob,5,[['e',deletedComment.id],['k','1111']],'',attachedAt+6);
const delByStranger=sign(stranger,5,[['e',comment.id],['k','1111']],'',attachedAt+7);
const twoRoots=sign(bob,1111,[['E',issue.id],['E',before.id],['K','1621']],'ambiguous',attachedAt+8);
const events=[annOld,before,issue,comment,strangerStatus,maintStatus,lateComment,deletedComment,delByAuthor,delByStranger,twoRoots];
const atts=[{address:C.parseAddress(coord),relayHints:[],attachedAt,detachedAt:attachedAt+20}];
let acts=plain(C.timelineActivities(events,atts,[A1]));
const ids=acts.map(a=>a.id);
assert.ok(ids.includes(issue.id),'a ticket is matched through its SECOND `a` tag');
assert.ok(!ids.includes(before.id),'opened before the attachment: hidden');
assert.ok(ids.includes(comment.id),'a stranger\'s kind-5 cannot hide someone else\'s comment');
assert.ok(!ids.includes(deletedComment.id),'deleted by its own author: hidden');
assert.ok(!ids.includes(lateComment.id),'after detachedAt: hidden');
assert.ok(!ids.includes(strangerStatus.id),'a status from outside the trust set is ignored');
assert.ok(ids.includes(maintStatus.id),'a maintainer status is shown');
assert.ok(!ids.includes(twoRoots.id),'a comment with two E roots is not a comment on either');
assert.deepEqual(acts.map(a=>a.createdAt),[...acts.map(a=>a.createdAt)].sort((a,b)=>b-a),'newest first');
const opened=acts.find(a=>a.id===issue.id);
assert.equal(opened.ticket.subject,'Crash on push','subject trimmed');
assert.deepEqual(opened.ticket.labels,['bug'],'labels lowercased + deduped');
assert.equal(opened.repository.coordinate,coord,'the ATTACHED repository is reported, not the first a tag');

// A newer announcement that drops the maintainer takes their status away; a stale one cannot restore it.
const annNew=sign(owner,30617,[['d','ngit'],['relays','wss://relay.ngit.dev']],'',attachedAt+30);
acts=plain(C.timelineActivities(events,atts,[C.parseAnnouncement(annNew),A1]));
assert.ok(!acts.some(a=>a.id===maintStatus.id),'removed maintainer: status no longer trusted');
acts=plain(C.timelineActivities(events,atts,[A1,C.parseAnnouncement(annNew)]));
assert.ok(!acts.some(a=>a.id===maintStatus.id),'order of announcements does not matter — newest wins');

// Re-attach: a second interval shows activity from then on.
const atts2=[...atts,{address:C.parseAddress(coord),relayHints:[],attachedAt:attachedAt+40}];
assert.ok(plain(C.timelineActivities(events,atts2,[A1])).some(a=>a.id===lateComment.id),'re-attached interval shows later activity');

// ── grouping in the mixed timeline: adjacent only, chat breaks a group, day breaks a group ─────────
const i2=sign(alice,1621,[['a',coord],['subject','second']],'',attachedAt+9);
const i3=sign(alice,1621,[['a',coord],['subject','third']],'',attachedAt+11);
const g=plain(C.timelineActivities([annOld,issue,i2,i3],[{address:C.parseAddress(coord),relayHints:[],attachedAt}],[A1]));
let merged=plain(C.mergeTimeline([],g));
assert.equal(merged.length,1,'three tickets by one author on one day fold into one row');
assert.equal(merged[0].entries.length,3);
merged=plain(C.mergeTimeline([{id:'chat',at:(attachedAt+10)*1000,text:'hi'}],g));
assert.deepEqual(merged.map(x=>x.ccGit?'git:'+x.entries.length:'chat'),['git:2','chat','git:1'],'a chat message between them breaks the group');
const nextDay=sign(alice,1621,[['a',coord],['subject','tomorrow']],'',attachedAt+2*86400);
assert.equal(plain(C.mergeTimeline([],plain(C.timelineActivities([issue,nextDay],[{address:C.parseAddress(coord),relayHints:[],attachedAt}],[A1])))).length,2,'a new day starts a new group');
assert.equal(C.mergeTimeline([{id:'x',at:1}],[]).length,1,'no git activity: the chat list is returned as is');

// ── templates: Armada's tags, in Armada's order ─────────────────────────────────────────────────
const repo={address:C.parseAddress(coord),maintainers:[maint.pk,owner.pk]};
assert.deepEqual(plain(C.issueTemplate(repo,'Title','Body','wss://r',[['imeta','url https://x/a.png']],['Bug',' bug ','UI  kit'],'PosterChan')),{
  kind:1621,content:'Body',tags:[['a',coord,'wss://r'],['subject','Title'],['alt','git repository issue: Title'],['p',owner.pk],['p',maint.pk],
    ['t','bug'],['t','ui kit'],['imeta','url https://x/a.png'],['client','PosterChan']]});
const T=C.parseTicket(issue);
assert.deepEqual(plain(C.commentTemplate(T,'hi','wss://r',[],'PosterChan')),{kind:1111,content:'hi',tags:[
  ['E',issue.id,'wss://r',alice.pk],['K','1621'],['P',alice.pk],['e',issue.id,'wss://r',alice.pk],['k','1621'],['p',alice.pk],['client','PosterChan']]});
assert.deepEqual(plain(C.statusTemplate(T,repo,1632,'wss://r','PosterChan')),{kind:1632,content:'',tags:[
  ['e',issue.id,'wss://r','root'],['a',coord,'wss://r'],['alt','git issue status'],['p',alice.pk],['p',owner.pk],['p',maint.pk],['client','PosterChan']]});
assert.deepEqual(plain(C.deletionTemplate({id:comment.id,kind:1111})),{kind:5,content:'',tags:[['e',comment.id],['k','1111']]});
// A template signed and parsed back is a comment on the ticket (the round trip other clients do).
assert.equal(C.parseComment(NT.finalizeEvent({...C.commentTemplate(T,'x','',[]),created_at:clock},bob.sk)).ticketId,issue.id);
assert.equal(C.parseStatus(NT.finalizeEvent({...C.statusTemplate(T,repo,1631,''),created_at:clock},maint.sk)).ticketId,issue.id);

// ── labels, status words, options ───────────────────────────────────────────────────────────────
assert.deepEqual(plain(C.normalizeLabels(['A','a','  b  c ','x'.repeat(60),...'123456789'.split('')])),['a','b c','x'.repeat(40),'1','2','3','4','5']);
assert.equal(C.statusFromKind(1631,1621),'resolved');assert.equal(C.statusFromKind(1631,1618),'merged');
assert.equal(C.statusFromKind(undefined,1621),'open');assert.equal(C.statusFromKind(1633,1621),'draft');
assert.deepEqual(plain(C.statusOptions({kind:1621},'open')).map(o=>o.kind),[1631,1632]);
assert.deepEqual(plain(C.statusOptions({kind:1618},'open')).map(o=>o.kind),[1631,1633,1632],'draft is offered for pull requests only');
assert.deepEqual(plain(C.statusOptions({kind:1621},'closed')).map(o=>o.kind),[1630]);
assert.deepEqual(plain(C.statusOptions({kind:1618},'draft')).map(o=>o.kind),[1630,1631,1632]);
assert.ok(C.canSetStatus(T,{owner:owner.pk,maintainers:[maint.pk]},alice.pk),'the ticket author may');
assert.ok(C.canSetStatus(T,{owner:owner.pk,maintainers:[maint.pk]},maint.pk),'a maintainer may');
assert.ok(!C.canSetStatus(T,{owner:owner.pk,maintainers:[maint.pk]},bob.pk),'a commenter may not');

// ── CI: a concluded result beats progress; one run per (signer, commit, workflow) ─────────────────
const job=sign(owner,9841,[['job','build'],['name','Build'],['conclusion','failure'],['logs','javascript:alert(1)']],'tail of log');
const prog=sign(owner,39842,[['a',coord],['c','abc1234def'],['w','.github/workflows/ci.yml'],['status','in_progress'],['q',job.id,'','prov','build']],'',clock+100);
const done=sign(owner,9842,[['a',coord],['c','abc1234def'],['w','.github/workflows/ci.yml'],['conclusion','failure'],['q',job.id,'','prov','build']],'',clock+50);
const runs=plain(C.assembleCIRuns([job,prog,done]));
assert.equal(runs.length,1);assert.equal(runs[0].id,done.id,'a 9842 result supersedes a NEWER 39842 progress');
assert.equal(runs[0].jobs[0].result.conclusion,'failure');assert.equal(runs[0].jobs[0].result.logs,undefined,'a non-http logs URL is dropped');
assert.equal(C.ciWorkflowName(runs[0]),'ci');assert.equal(C.ciSummary(C.ciGroups(runs)),'1 failing');
assert.ok(!plain(C.timelineActivities([prog,done],atts,[A1])).some(a=>a.type==='ci-run'),'CI after detach is hidden');

// ── links + paste resolution ────────────────────────────────────────────────────────────────────
const ws=C.gitworkshopUrl(T,nip19);
const npub=nip19.npubEncode(alice.pk);
assert.ok(ws.startsWith('https://gitworkshop.dev/'+npub+'/ngit-fork/issues/nevent1'),'first `a` tag, no relay hint → no relay segment: '+ws);
const T2=C.parseTicket(sign(alice,1621,[['a',coord,'wss://relay.ngit.dev']]));
assert.ok(C.gitworkshopUrl(T2,nip19).includes('/'+nip19.npubEncode(owner.pk)+'/relay.ngit.dev/ngit/issues/'));
const naddr=nip19.naddrEncode({kind:30617,pubkey:owner.pk,identifier:'ngit',relays:['wss://relay.ngit.dev/']});
let r=plain(await C.resolveInput(naddr,nip19));assert.equal(r.address.coordinate,coord);assert.deepEqual(r.relayHints,['wss://relay.ngit.dev']);
r=plain(await C.resolveInput('nostr:'+naddr,nip19));assert.equal(r.address.coordinate,coord);
r=plain(await C.resolveInput('nostr://'+nip19.npubEncode(owner.pk)+'/relay.ngit.dev/ngit',nip19));assert.equal(r.address.coordinate,coord);assert.deepEqual(r.relayHints,['wss://relay.ngit.dev']);
r=plain(await C.resolveInput('nostr://alice@example.com/ngit',nip19,async n=>n==='alice@example.com'?owner.pk:null));assert.equal(r.address.coordinate,coord);
await assert.rejects(()=>C.resolveInput('nostr://bob@example.com/ngit',nip19,async()=>null),/NIP-05/);
await assert.rejects(()=>C.resolveInput('nostr://example.com/ngit',nip19),/npub or a NIP-05/,'a bare domain is ambiguous');
await assert.rejects(()=>C.resolveInput(nip19.naddrEncode({kind:30023,pubkey:owner.pk,identifier:'x'}),nip19),/30617/);
r=plain(await C.resolveInput('https://gitworkshop.dev/'+nip19.npubEncode(owner.pk)+'/relay.ngit.dev/ngit/issues',nip19));assert.equal(r.address.coordinate,coord);assert.deepEqual(r.relayHints,['wss://relay.ngit.dev']);
r=plain(await C.resolveInput('https://gitworkshop.dev/'+nip19.npubEncode(owner.pk)+'/ngit',nip19));assert.equal(r.address.coordinate,coord);
r=plain(await C.resolveInput('https://poster.place/r/'+nip19.npubEncode(owner.pk)+'/ngit',nip19));assert.equal(r.address.coordinate,coord);
await assert.rejects(()=>C.resolveInput('https://github.com/x/y',nip19),/naddr/);

// ── directory search ranking ────────────────────────────────────────────────────────────────────
const mk=(id,name,desc,relays=['wss://r'],at=1)=>({identifier:id,name,description:desc,relays,createdAt:at});
const dir=[mk('ngit-relay','ngit-relay','',undefined,5),mk('ngit','ngit','',undefined,1),mk('tools','Tools','uses ngit',undefined,9),mk('engit','engit','',undefined,2),mk('ngit-x','ngit-x','',[],99)];
assert.deepEqual(plain(C.searchDirectory(dir,'ngit')).map(x=>x.identifier),['ngit','ngit-relay','engit','tools'],'exact > prefix > substring > description; no-relay repos dropped');
assert.deepEqual(plain(C.searchDirectory(dir,'')).map(x=>x.identifier),['tools','ngit-relay','engit','ngit'],'empty query: recently announced');

// ── Projects: NOT interval-gated, counts exclude deleted comments, trusted status only ────────────
const items=plain(C.projectItems(events,[A1]));
const it=items.find(x=>x.ticket.id===issue.id);
assert.ok(items.some(x=>x.ticket.id===before.id),'Projects shows the ticket opened before attaching');
assert.equal(it.commentCount,2,'comment + lateComment; the self-deleted one is not counted, the two-root one is not a comment');
assert.equal(it.status,'closed','the maintainer close (old announcement) — the stranger close is ignored');
assert.deepEqual(plain(C.filterItems(items,{type:'issue',state:'open'})).map(x=>x.ticket.id),[before.id]);
assert.deepEqual(plain(C.filterItems(items,{type:'issue',state:'all',query:'crash BUG'})).map(x=>x.ticket.id),[issue.id],'multi-term AND search over title + labels');
assert.deepEqual(plain(C.suggestLabels(items,coord)).slice(0,2),['bug','enhancement']);

// ── row previews never leak markup ──────────────────────────────────────────────────────────────
assert.equal(C.previewText('# Title\n\n**bold** [link](https://x) ![img](https://x/a.png) ```code```'),'Title bold link');
console.log('OK');
