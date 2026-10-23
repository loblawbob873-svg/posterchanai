/* CONCORD × GIT — a community channel that follows NIP-34 repositories, the way Armada does it.
 *
 * WIRE FORMAT IS ARMADA'S, NOT OURS. A repository is attached to a CHANNEL (never the community) as
 * `custom["armada.git"].repositories` on the channel's control entity — half-open intervals
 * [attachedAt, detachedAt) written by cord-reader.js (createChannelEditWrap / createChannelWrap).
 * Everything this file publishes is byte-for-byte what Armada's src/lib/gitActivity.ts builds
 * (issue 1621, NIP-22 comment 1111, status 1630-1633, NIP-09 deletion), so an issue filed here shows
 * up in Armada, ngit and gitworkshop, and the other way round.
 *
 * WHAT A CHANNEL SHOWS (Armada's buildGitTimelineActivities, ported rule for rule):
 *   - a ticket is found through ANY of its `a` tags, not just the first (forks tag several);
 *   - its "opened" row shows only if it was opened inside one of that repo's intervals;
 *   - a comment / status row shows only if IT happened inside an interval — so detaching a repo
 *     hides what happens after, and re-attaching shows it again from then on, in every client;
 *   - a status counts only from the ticket author, the repo owner or a maintainer of the NEWEST
 *     announcement (a stale one cannot restore a removed maintainer);
 *   - a NIP-09 deletion counts only from the deleted event's own author.
 * The Projects view deliberately ignores intervals (Armada: attachedAt 0) — it is the repo, not the
 * channel's history.
 *
 * NETWORK. Every relay call goes through `window.PCConcordRelay` — the wrapped door concord.js
 * builds — so repository relays are dialled through this node's tunnel like every other relay
 * Concord touches (tests/test_concord_relay_calls_go_through_the_tunnel.py covers this file too).
 * Reads go to the attachment's relay hints, then the announcement's `relays`; publishes go to the
 * same set. The community's own relays are never used for git, exactly as in Armada.
 *
 * COST. One bounded refresh per repository per minute while its channel or the Projects view is on
 * screen, and nothing at all otherwise. Events are cached per repository (newest 600, ~400 KB) so a
 * channel paints its git rows before any socket opens.
 *
 * The first half of this file is DOM-free and is run under node by
 * tests/client/concord_git_core_runtime.mjs; the second half is the UI.
 */
(function(root){
  'use strict';

  /* ───────────────────────────── core (no DOM) ───────────────────────────── */
  const ANN=30617,PR=1618,ISSUE=1621,OPEN=1630,APPLIED=1631,CLOSED=1632,DRAFT=1633,COMMENT=1111,DELETION=5;
  const CI_JOB=9841,CI_RESULT=9842,CI_PROGRESS=39842;
  const STATUS_KINDS=[OPEN,APPLIED,CLOSED,DRAFT],TICKET_KINDS=[ISSUE,PR],CI_KINDS=[CI_JOB,CI_RESULT,CI_PROGRESS];
  const CI_CONCLUSIONS=['success','failure','neutral','cancelled','skipped','timed_out','startup_failure'];
  const DISCOVERY_RELAY='wss://index.ngit.dev';
  const MAX_LABELS=8,MAX_LABEL_LENGTH=40;
  const HEX=/^[0-9a-f]{64}$/;

  const isId=v=>typeof v==='string'&&HEX.test(v);
  const unique=a=>[...new Set(a)];
  const firstTag=(ev,n)=>{const t=(ev.tags||[]).find(x=>x[0]===n);return t?t[1]:undefined;};
  const tagValues=(ev,n)=>(ev.tags||[]).filter(t=>t[0]===n).flatMap(t=>t.slice(1).filter(Boolean));
  const firstLine=s=>{const l=String(s||'').split('\n').find(x=>x.trim());return l?l.trim():undefined;};

  // Armada's normalizeRelayUrl: ws/wss only (a bare host becomes wss), trailing slash dropped.
  function normRelay(url){
    let v=String(url||'').trim();if(!v)return undefined;
    if(/^[a-z][a-z\d+.-]*:\/\//i.test(v)&&!/^wss?:\/\//i.test(v))return undefined;
    if(!/^wss?:\/\//i.test(v)){const secure=!/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(v);v=(secure?'wss':'ws')+'://'+v;}
    try{const u=new URL(v);if(u.protocol!=='ws:'&&u.protocol!=='wss:')return undefined;return u.toString().replace(/\/$/,'');}catch(_){return undefined;}
  }
  const normRelays=vals=>unique((vals||[]).filter(v=>typeof v==='string'&&/^wss?:\/\//i.test(v.trim())).map(normRelay).filter(Boolean));
  const httpsUrls=vals=>unique((vals||[]).flatMap(v=>{try{const u=new URL(v);return u.protocol==='https:'?[u.href]:[];}catch(_){return [];}}));

  function parseAddress(value){
    const parts=String(value||'').split(':');if(parts.length<3)return undefined;
    const kind=Number(parts[0]),owner=parts[1],identifier=parts.slice(2).join(':');
    if(kind!==ANN||!isId(owner)||!identifier)return undefined;
    return {kind:ANN,owner,identifier,coordinate:ANN+':'+owner+':'+identifier};
  }
  function parseAnnouncement(ev){
    if(!ev||ev.kind!==ANN||!isId(ev.pubkey))return undefined;
    const d=(firstTag(ev,'d')||'').trim();if(!d)return undefined;
    const address=parseAddress(ANN+':'+ev.pubkey+':'+d);if(!address)return undefined;
    return {event:ev,address,owner:ev.pubkey,identifier:d,name:(firstTag(ev,'name')||'').trim()||d,
      description:(firstTag(ev,'description')||'').trim()||undefined,relays:normRelays(tagValues(ev,'relays')),
      maintainers:unique(tagValues(ev,'maintainers').filter(pk=>isId(pk)&&pk!==ev.pubkey)),
      cloneUrls:httpsUrls(tagValues(ev,'clone')),webUrls:httpsUrls(tagValues(ev,'web')),createdAt:ev.created_at};
  }
  function parseTicket(ev){
    if(!ev||!TICKET_KINDS.includes(ev.kind)||!isId(ev.pubkey))return undefined;
    const addrs=(ev.tags||[]).filter(t=>t[0]==='a').map(t=>{const a=parseAddress(t[1]);if(!a)return undefined;const h=normRelay(t[2]||'');return h?{...a,relayHint:h}:a;}).filter(Boolean);
    const bn=(firstTag(ev,'branch-name')||'').trim(),base=(firstTag(ev,'base')||'').trim(),head=(firstTag(ev,'head')||'').trim();
    return {event:ev,id:ev.id,kind:ev.kind,type:ev.kind===ISSUE?'issue':'pull-request',
      subject:(firstTag(ev,'subject')||'').trim()||firstLine(ev.content)||'(no subject)',content:ev.content||'',
      labels:unique(tagValues(ev,'t').map(l=>l.trim().toLowerCase()).filter(Boolean)),
      repositoryAddress:addrs[0],repositoryAddresses:addrs,
      branches:ev.kind===PR&&(bn||base||head)?{name:bn||undefined,base:base||undefined,head:head||undefined}:undefined,
      author:ev.pubkey,createdAt:ev.created_at};
  }
  function parseStatus(ev){
    if(!ev||!STATUS_KINDS.includes(ev.kind)||!isId(ev.pubkey))return undefined;
    const targets=(ev.tags||[]).filter(t=>t[0]==='e'&&(t[3]==='root'||t[3]===undefined));
    const ticketId=targets.length===1?targets[0][1]:undefined;
    if(!isId(ticketId))return undefined;
    return {event:ev,kind:ev.kind,ticketId,author:ev.pubkey,createdAt:ev.created_at};
  }
  function parseComment(ev){
    if(!ev||ev.kind!==COMMENT||!isId(ev.pubkey))return undefined;
    const roots=(ev.tags||[]).filter(t=>t[0]==='E'),ticketId=roots.length===1?roots[0][1]:undefined;
    const ticketKind=Number(firstTag(ev,'K'));
    if(!isId(ticketId)||!TICKET_KINDS.includes(ticketKind))return undefined;
    return {event:ev,id:ev.id,ticketId,ticketKind,content:ev.content||'',author:ev.pubkey,createdAt:ev.created_at};
  }
  function statusFromKind(kind,ticketKind){
    if(kind===CLOSED)return 'closed';if(kind===DRAFT)return 'draft';
    if(kind===APPLIED)return ticketKind===ISSUE?'resolved':'merged';return 'open';
  }
  const newestFirst=(a,b)=>b.createdAt-a.createdAt||String(a.event.id).localeCompare(String(b.event.id));
  function trustedAuthors(ticket,repo){return new Set([ticket.author,repo&&repo.owner,...((repo&&repo.maintainers)||[])].filter(Boolean));}
  function resolveStatus(ticket,repo,events){
    const trust=trustedAuthors(ticket,repo);
    return (events||[]).map(parseStatus).filter(s=>s&&s.ticketId===ticket.id&&trust.has(s.author)).sort(newestFirst)[0];
  }
  function attachedAt(a,createdAt){return a.attachedAt<=createdAt&&(a.detachedAt===undefined||createdAt<a.detachedAt);}
  /* The wire form (cord-reader's inspectControl) → parsed attachments; bad entries are skipped. */
  function attachmentsOf(channel){
    const out=[];
    for(const r of (channel&&channel.repositories)||[]){
      const address=parseAddress(r&&r.address);if(!address||!Number.isSafeInteger(r.attachedAt))continue;
      if(r.detachedAt!==undefined&&(!Number.isSafeInteger(r.detachedAt)||r.detachedAt<r.attachedAt))continue;
      out.push({address,relayHints:normRelays(r.relayHints||[]).slice(0,8),attachedAt:r.attachedAt,...(r.detachedAt===undefined?{}:{detachedAt:r.detachedAt})});
    }
    return out;
  }
  const isActive=a=>a.detachedAt===undefined;
  function normalizeLabels(labels){
    const seen=new Set();
    for(const v of labels||[]){const l=String(v).trim().toLowerCase().replace(/\s+/g,' ').slice(0,MAX_LABEL_LENGTH);if(l)seen.add(l);if(seen.size>=MAX_LABELS)break;}
    return [...seen];
  }
  function collectDeletions(events){
    const out=new Map();
    for(const ev of events||[]){if(!ev||ev.kind!==DELETION||!isId(ev.pubkey))continue;
      for(const t of ev.tags||[]){if(t[0]!=='e'||!isId(t[1]))continue;const s=out.get(t[1]);if(s)s.add(ev.pubkey);else out.set(t[1],new Set([ev.pubkey]));}}
    return out;
  }
  const isDeleted=(dels,id,author)=>!!(dels.get(id)&&dels.get(id).has(author));

  /* ── templates: Armada's, tag for tag (gitActivity.ts buildGit*Template + the `client` tag) ── */
  function issueTemplate(repo,subject,body,relayHint,media,labels,client){
    const to=unique([repo.address.owner,...(repo.maintainers||[])]).filter(isId);
    return {kind:ISSUE,content:String(body||''),tags:[['a',repo.address.coordinate,relayHint||''],['subject',subject],['alt','git repository issue: '+subject],
      ...to.map(pk=>['p',pk]),...normalizeLabels(labels).map(l=>['t',l]),...(media||[]).map(t=>[...t]),['client',client||'PosterChan']]};
  }
  function commentTemplate(ticket,content,relayHint,media,client){
    const h=relayHint||'';
    return {kind:COMMENT,content:String(content||''),tags:[['E',ticket.id,h,ticket.author],['K',String(ticket.kind)],['P',ticket.author],
      ['e',ticket.id,h,ticket.author],['k',String(ticket.kind)],['p',ticket.author],...(media||[]).map(t=>[...t]),['client',client||'PosterChan']]};
  }
  function statusTemplate(ticket,repo,kind,relayHint,client){
    const to=unique([ticket.author,repo.address.owner,...(repo.maintainers||[])]).filter(isId),h=relayHint||'';
    return {kind,content:'',tags:[['e',ticket.id,h,'root'],['a',repo.address.coordinate,h],['alt','git '+(ticket.type==='issue'?'issue':'pull request')+' status'],
      ...to.map(pk=>['p',pk]),['client',client||'PosterChan']]};
  }
  function deletionTemplate(target){return {kind:DELETION,content:'',tags:[['e',target.id],['k',String(target.kind)]]};}
  /* Who may change a ticket's status, and what each state offers (Armada's statusOptions). */
  function canSetStatus(ticket,repo,viewer){return !!viewer&&trustedAuthors(ticket,repo).has(viewer);}
  function statusOptions(ticket,status){
    const done=ticket.kind===ISSUE?'Mark resolved':'Mark merged';
    if(status==='closed'||status==='merged'||status==='resolved')return [{kind:OPEN,label:'Reopen'}];
    if(status==='draft')return [{kind:OPEN,label:'Mark open'},{kind:APPLIED,label:done},{kind:CLOSED,label:'Close'}];
    return [{kind:APPLIED,label:done},...(ticket.kind===PR?[{kind:DRAFT,label:'Mark draft'}]:[]),{kind:CLOSED,label:'Close'}];
  }

  /* ── CI (ngit-ci kinds; Armada src/lib/ci.ts) ── */
  const conclusionOf=v=>CI_CONCLUSIONS.includes(v)?v:undefined;
  function ciAddresses(ev){const seen=new Set(),out=[];for(const t of ev.tags||[]){if(t[0]!=='a'||!t[1])continue;const a=parseAddress(t[1]);if(!a||seen.has(a.coordinate))continue;seen.add(a.coordinate);out.push(a);}return out;}
  function parseCIJob(ev){
    if(!ev||ev.kind!==CI_JOB)return undefined;const job=(firstTag(ev,'job')||'').trim();if(!job)return undefined;
    let logs=(firstTag(ev,'logs')||'').trim();try{const u=new URL(logs);logs=/^https?:$/.test(u.protocol)?u.href:undefined;}catch(_){logs=undefined;}
    return {event:ev,id:ev.id,author:ev.pubkey,job,name:(firstTag(ev,'name')||'').trim()||undefined,conclusion:conclusionOf((firstTag(ev,'conclusion')||'').trim()),logs,createdAt:ev.created_at};
  }
  function parseCIRun(ev){
    if(!ev||(ev.kind!==CI_RESULT&&ev.kind!==CI_PROGRESS))return undefined;
    const repositoryAddresses=ciAddresses(ev);if(!repositoryAddresses.length)return undefined;
    const raw=(firstTag(ev,'status')||'').trim();
    const status=ev.kind===CI_RESULT?'concluded':raw==='queued'||raw==='concluded'?raw:'in_progress';
    const jobs=[],seen=new Set();
    for(const t of ev.tags||[]){if(t[0]!=='q'||!t[1])continue;const job=(t[4]||'').trim()||t[1];if(seen.has(job))continue;seen.add(job);jobs.push({job,eventId:t[1],provider:(t[3]||'').trim()||undefined});}
    const v=n=>(firstTag(ev,n)||'').trim()||undefined;
    return {event:ev,id:ev.id,author:ev.pubkey,repositoryAddresses,commit:v('c'),workflow:v('w'),trigger:v('o'),ref:v('r'),status,conclusion:conclusionOf((firstTag(ev,'conclusion')||'').trim()),jobs,createdAt:ev.created_at};
  }
  function assembleCIRuns(events){
    const results=new Map();for(const ev of events||[]){const j=parseCIJob(ev);if(j)results.set(j.id,j);}
    const by=new Map();
    for(const ev of events||[]){const run=parseCIRun(ev);if(!run)continue;const key=run.author+'\0'+(run.commit||'')+'\0'+(run.workflow||'');
      const cur=by.get(key);if(cur){const cd=run.event.kind===CI_RESULT,ed=cur.event.kind===CI_RESULT;
        if(cd!==ed){if(!cd)continue;}else if(!(run.createdAt>cur.createdAt||(run.createdAt===cur.createdAt&&run.id.localeCompare(cur.id)<0)))continue;}
      by.set(key,run);}
    return [...by.values()].map(r=>({...r,jobs:r.jobs.map(j=>({...j,result:results.get(j.eventId)}))})).sort((a,b)=>b.createdAt-a.createdAt||a.id.localeCompare(b.id));
  }
  const ciWorkflowName=run=>{const p=(run.workflow||'').trim();if(!p)return 'workflow';return (p.split('/').pop()||'').replace(/\.ya?ml$/i,'')||'workflow';};
  const ciOutcome=run=>run.status==='concluded'?(run.conclusion||'neutral'):run.status;
  const ciFailing=o=>o==='failure'||o==='timed_out'||o==='startup_failure';
  function ciGroups(runs){
    const g=new Map();for(const r of runs){const n=ciWorkflowName(r);if(g.has(n))g.get(n).push(r);else g.set(n,[r]);}
    return [...g].map(([name,rs])=>{const s=[...rs].sort((a,b)=>b.createdAt-a.createdAt||a.id.localeCompare(b.id));return {name,runs:s,latest:s[0]};})
      .sort((a,b)=>b.latest.createdAt-a.latest.createdAt||a.name.localeCompare(b.name));
  }
  function ciSummary(groups){
    const o=groups.map(g=>ciOutcome(g.latest)),f=o.filter(ciFailing).length,r=o.filter(x=>x==='in_progress'||x==='queued').length,p=o.filter(x=>x==='success').length;
    const parts=[f?f+' failing':'',r?r+' running':'',p?p+' passing':''].filter(Boolean);
    return parts.length?parts.join(', '):groups.length+' workflows';
  }

  /* ── the channel timeline: Armada's buildGitTimelineActivities, rule for rule ── */
  function timelineActivities(events,attachments,repos){
    const attached=new Map();
    for(const a of attachments||[]){const l=attached.get(a.address.coordinate);if(l)l.push(a);else attached.set(a.address.coordinate,[a]);}
    const dels=collectDeletions(events),tickets=new Map(),ticketRepo=new Map();
    for(const ev of events||[]){const t=parseTicket(ev);if(!t||isDeleted(dels,t.id,t.author))continue;
      const repo=t.repositoryAddresses.find(a=>attached.has(a.coordinate));if(!repo)continue;tickets.set(t.id,t);ticketRepo.set(t.id,repo);}
    const annBy=new Map();
    for(const r of repos||[]){const c=annBy.get(r.address.coordinate);if(!c||r.createdAt>c.createdAt)annBy.set(r.address.coordinate,r);}
    const out=[],inside=(coord,at)=>(attached.get(coord)||[]).some(i=>attachedAt(i,at));
    for(const t of tickets.values()){const repo=ticketRepo.get(t.id);if(inside(repo.coordinate,t.createdAt))out.push({type:'ticket-opened',ticket:t,repository:repo,createdAt:t.createdAt,id:t.id,author:t.author});}
    for(const ev of events||[]){
      const c=parseComment(ev);
      if(c){if(isDeleted(dels,c.id,c.author))continue;const t=tickets.get(c.ticketId),repo=t&&ticketRepo.get(t.id);
        if(t&&repo&&inside(repo.coordinate,c.createdAt))out.push({type:'comment',comment:c,ticket:t,repository:repo,createdAt:c.createdAt,id:c.id,author:c.author});continue;}
      const s=parseStatus(ev);if(!s||isDeleted(dels,s.event.id,s.author))continue;
      const t=tickets.get(s.ticketId),repo=t&&ticketRepo.get(t.id);if(!t||!repo||!inside(repo.coordinate,s.createdAt))continue;
      const ann=annBy.get(repo.coordinate),trust=new Set([t.author,ann?ann.owner:repo.owner,...((ann&&ann.maintainers)||[])]);
      if(trust.has(s.author))out.push({type:'status-change',status:s,ticket:t,repository:repo,createdAt:s.createdAt,id:s.event.id,author:s.author});
    }
    for(const run of assembleCIRuns(events)){const repo=run.repositoryAddresses.find(a=>attached.has(a.coordinate));
      if(repo&&inside(repo.coordinate,run.createdAt))out.push({type:'ci-run',run,repository:repo,createdAt:run.createdAt,id:run.id,author:run.author});}
    const seen=new Set();
    return out.sort((a,b)=>b.createdAt-a.createdAt||a.id.localeCompare(b.id)).filter(x=>!seen.has(x.id)&&seen.add(x.id));
  }
  /* Which adjacent rows fold into one (Armada channelTimeline.ts): same day, and tickets by the same
   * author/type/repo, comments or statuses on the same ticket, or any CI. A chat message between two
   * entries always breaks the group — the merge below runs over the MIXED list for that reason. */
  function groupKey(a){
    const day=new Date(a.createdAt*1000).toDateString();
    if(a.type==='ticket-opened')return day+'|t|'+a.author+'|'+a.ticket.kind+'|'+a.repository.coordinate;
    if(a.type==='comment')return day+'|c|'+a.ticket.id;
    if(a.type==='status-change')return day+'|s|'+a.ticket.id;
    return day+'|ci';
  }
  /* Interleave git activity into a chat list (oldest first, ties by id) and fold adjacent groups.
   * Chat rows pass through untouched; each folded git group becomes ONE `{ccGit:true, at, entries}`. */
  function mergeTimeline(chat,activities){
    if(!activities||!activities.length)return chat;
    const items=(chat||[]).map((m,i)=>({at:Number(m&&m.at)||0,id:String((m&&(m.id||m.rumorId))||''),i,m}))
      .concat(activities.map(a=>({at:a.createdAt*1000,id:a.id,a})));
    items.sort((x,y)=>x.at-y.at||(x.m&&y.m?x.i-y.i:x.id.localeCompare(y.id)));
    const out=[];let open=null;
    for(const it of items){
      if(it.m){open=null;out.push(it.m);continue;}
      const k=groupKey(it.a);
      if(open&&open.key===k){open.entries.push(it.a);continue;}
      open={ccGit:true,key:k,at:it.at,entries:[it.a]};out.push(open);
    }
    return out;
  }

  /* ── links + input resolution ── */
  function gitworkshopUrl(ticket,nip19){
    const a=ticket&&ticket.repositoryAddress;if(!a||!nip19)return undefined;
    try{const npub=nip19.npubEncode(a.owner),nevent=nip19.neventEncode({id:ticket.id,author:ticket.author,kind:ticket.kind});
      const h=a.relayHint&&normRelay(a.relayHint);
      const seg=h?(h.startsWith('wss://')?encodeURIComponent(h.slice(6)):encodeURIComponent('ws:'+h.slice(5))):'';
      return 'https://gitworkshop.dev/'+npub+'/'+(seg?seg+'/':'')+encodeURIComponent(a.identifier)+'/'+(ticket.type==='issue'?'issues':'prs')+'/'+nevent;}catch(_){return undefined;}
  }
  /* What can be pasted (Armada's resolveGitRepositoryInput, plus two forms people actually have):
   *   naddr1… (kind 30617) · nostr:naddr1… · nostr://<npub>[/<relay-host>]/<id> · nostr://name@domain/<id>
   *   https://gitworkshop.dev/<npub>[/<relay>]/<id>[/…] · https://<this-or-any-node>/r/<npub>/<id>
   * `resolveNip05` is injected (it needs the network). */
  async function resolveInput(input,nip19,resolveNip05){
    const value=String(input||'').trim().replace(/^nostr:(?=naddr1)/i,'');
    if(!value)throw new Error('Enter a repository address.');
    if(/^naddr1/i.test(value)){
      let d;try{d=nip19.decode(value);}catch(_){throw new Error('That naddr could not be decoded.');}
      if(d.type!=='naddr')throw new Error('Unsupported Nostr address kind.');
      if(d.data.kind!==ANN)throw new Error('Only kind-30617 repository addresses can be connected.');
      const address=parseAddress(ANN+':'+String(d.data.pubkey).toLowerCase()+':'+d.data.identifier);
      if(!address)throw new Error('Repository address has an invalid owner or identifier.');
      return {address,relayHints:normRelays(d.data.relays||[])};
    }
    if(/^30617:/.test(value)){const address=parseAddress(value);if(!address)throw new Error('Repository address has an invalid owner or identifier.');return {address,relayHints:[]};}
    let url;try{url=new URL(value);}catch(_){throw new Error('Enter an naddr, a nostr:// address or a repository link.');}
    const npubOwner=s=>{try{const d=nip19.decode(s);if(d.type==='npub'&&isId(d.data))return d.data;}catch(_){}throw new Error('Repository owner is not a valid npub.');};
    let segs;try{segs=url.pathname.split('/').filter(Boolean).map(decodeURIComponent);}catch(_){throw new Error('Repository identifier is invalid.');}
    if(url.protocol==='https:'&&url.hostname==='gitworkshop.dev'){
      if(segs.length<2||!/^npub1/.test(segs[0]))throw new Error('That gitworkshop link does not name a repository.');
      const owner=npubOwner(segs[0]);let relay='',id=segs[1];
      if(segs.length>=3&&!['issues','prs','proposals','actions'].includes(segs[2])&&(segs[1].includes('.')||segs[1].startsWith('ws:'))){relay=segs[1];id=segs[2];}
      const address=parseAddress(ANN+':'+owner+':'+id);if(!address)throw new Error('Repository address has an invalid owner or identifier.');
      const hint=relay?normRelay(relay.startsWith('ws:')?'ws://'+relay.slice(3):'wss://'+relay):'';
      return {address,relayHints:hint?[hint]:[]};
    }
    /* A GRASP clone address, https://<node>/git/<npub>/<id>.git -- what "Copy clone URL" hands out and what a
     * repository's own page shows first (2026-10-21: "concord git repository address not working"). The node
     * that serves it also serves the repository's relay at wss://<node>/git. */
    if(/^https?:$/.test(url.protocol)&&segs[0]==='git'&&segs.length>=3&&/^npub1/.test(segs[1])){
      const address=parseAddress(ANN+':'+npubOwner(segs[1])+':'+segs[2].replace(/\.git$/,''));if(!address)throw new Error('Repository address has an invalid owner or identifier.');
      const hint=normRelay((url.protocol==='http:'?'ws://':'wss://')+url.host+'/git');
      return {address,relayHints:hint?[hint]:[]};
    }
    if(/^https?:$/.test(url.protocol)&&segs[0]==='r'&&segs.length===3&&/^npub1/.test(segs[1])){
      const address=parseAddress(ANN+':'+npubOwner(segs[1])+':'+segs[2]);if(!address)throw new Error('Repository address has an invalid owner or identifier.');
      return {address,relayHints:[]};
    }
    if(url.protocol!=='nostr:')throw new Error('Enter an naddr, a nostr:// address or a repository link.');
    if(url.password||url.port||url.search||url.hash)throw new Error('Invalid nostr:// repository address.');
    if(segs.length<1||segs.length>2)throw new Error('Repository identifier is required.');
    const identifier=segs[segs.length-1],hinted=segs.length===2?[normRelay('wss://'+segs[0])].filter(Boolean):[];
    let owner;
    if(url.hostname.toLowerCase().startsWith('npub1'))owner=npubOwner(url.hostname.toLowerCase());
    else{
      const name=url.username?decodeURIComponent(url.username)+'@'+url.hostname:'';
      if(!name)throw new Error('Repository owner must be an npub or a NIP-05 address.');
      owner=resolveNip05?await resolveNip05(name):null;
      if(!isId(owner))throw new Error('Could not verify that NIP-05 repository owner.');
    }
    const address=parseAddress(ANN+':'+owner+':'+identifier);if(!address)throw new Error('Repository address has an invalid owner or identifier.');
    return {address,relayHints:hinted};
  }
  /* Directory search (Armada useGitRepositoryDirectory): exact > prefix > substring (id/name) > description. */
  function searchDirectory(repos,query,limit=8){
    const q=String(query||'').trim().toLowerCase();
    const usable=(repos||[]).filter(r=>r.relays&&r.relays.length);
    if(!q)return [...usable].sort((a,b)=>b.createdAt-a.createdAt).slice(0,limit);
    const rank=r=>{const id=r.identifier.toLowerCase(),n=r.name.toLowerCase();
      if(id===q||n===q)return 0;if(id.startsWith(q)||n.startsWith(q))return 1;if(id.includes(q)||n.includes(q))return 2;
      if((r.description||'').toLowerCase().includes(q))return 3;return -1;};
    return usable.map(r=>({r,k:rank(r)})).filter(x=>x.k>=0).sort((a,b)=>a.k-b.k||b.r.createdAt-a.r.createdAt).slice(0,limit).map(x=>x.r);
  }
  /* Projects data (Armada useGitProjects): every ticket of the active repos, NOT interval-gated. */
  function projectItems(events,repos){
    const dels=collectDeletions(events),annBy=new Map();
    for(const r of repos)annBy.set(r.address.coordinate,r);
    const statuses=(events||[]).filter(e=>STATUS_KINDS.includes(e.kind)),comments=new Map(),latest=new Map();
    for(const ev of events||[]){const c=parseComment(ev);if(!c||isDeleted(dels,c.id,c.author))continue;comments.set(c.ticketId,(comments.get(c.ticketId)||0)+1);latest.set(c.ticketId,Math.max(latest.get(c.ticketId)||0,c.createdAt));}
    for(const ev of statuses){const s=parseStatus(ev);if(s)latest.set(s.ticketId,Math.max(latest.get(s.ticketId)||0,s.createdAt));}
    const items=[];
    for(const ev of events||[]){const t=parseTicket(ev);if(!t||isDeleted(dels,t.id,t.author))continue;
      const repo=t.repositoryAddresses.map(a=>annBy.get(a.coordinate)).find(Boolean);if(!repo)continue;
      const st=resolveStatus(t,repo,statuses);
      items.push({ticket:t,repo,status:statusFromKind(st&&st.kind,t.kind),commentCount:comments.get(t.id)||0,updatedAt:Math.max(t.createdAt,latest.get(t.id)||0)});}
    const seen=new Set();return items.filter(x=>!seen.has(x.ticket.id)&&seen.add(x.ticket.id)).sort((a,b)=>b.updatedAt-a.updatedAt);
  }
  function filterItems(items,{type,state,label,query,sort}={}){
    const terms=String(query||'').toLowerCase().split(/\s+/).filter(Boolean);
    const out=items.filter(x=>{
      if(type&&x.ticket.type!==type)return false;
      const open=x.status==='open'||x.status==='draft';
      if(state==='open'&&!open)return false;if(state==='closed'&&open)return false;
      if(label&&!x.ticket.labels.includes(label))return false;
      const hay=(x.ticket.subject+' '+x.ticket.content+' '+x.repo.name+' '+x.ticket.labels.join(' ')).toLowerCase();
      return terms.every(t=>hay.includes(t));});
    return sort==='name'?out.sort((a,b)=>a.ticket.subject.localeCompare(b.ticket.subject)):out;
  }
  function suggestLabels(items,coordinate){
    const counts=new Map();for(const x of items)if(!coordinate||x.repo.address.coordinate===coordinate)for(const l of x.ticket.labels)counts.set(l,(counts.get(l)||0)+1);
    const used=[...counts].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(x=>x[0]);
    return unique([...used,'bug','enhancement','documentation','question']);
  }
  /* Plain-text preview for a row: markdown and media stripped (Armada gitSummary). */
  function previewText(s,max=120){
    const t=String(s||'').replace(/```[\s\S]*?```/g,' ').replace(/!\[[^\]]*\]\([^)]*\)/g,' ').replace(/\[([^\]]*)\]\([^)]*\)/g,'$1')
      .replace(/https?:\/\/\S+\.(png|jpe?g|gif|webp|mp4|webm|mov)(\?\S*)?/gi,' ').replace(/[#>*_`~]+/g,' ').replace(/\s+/g,' ').trim();
    return t.length>max?t.slice(0,max-1)+'…':t;
  }

  const core={ANN,PR,ISSUE,OPEN,APPLIED,CLOSED,DRAFT,COMMENT,DELETION,STATUS_KINDS,TICKET_KINDS,CI_KINDS,DISCOVERY_RELAY,
    normRelay,normRelays,parseAddress,parseAnnouncement,parseTicket,parseStatus,parseComment,statusFromKind,resolveStatus,
    attachedAt,attachmentsOf,isActive,normalizeLabels,collectDeletions,issueTemplate,commentTemplate,statusTemplate,deletionTemplate,
    canSetStatus,statusOptions,assembleCIRuns,ciWorkflowName,ciOutcome,ciGroups,ciSummary,timelineActivities,groupKey,mergeTimeline,
    gitworkshopUrl,resolveInput,searchDirectory,projectItems,filterItems,suggestLabels,previewText};

  if(typeof document==='undefined'){root.PCConcordGit={core};return;}

  /* ───────────────────────────── data ───────────────────────────── */
  const CACHE_PREFIX='pc.ccgit.v1.',CACHE_MAX_EVENTS=600,CACHE_MAX_BYTES=400000,REFRESH_MS=60000;
  const repoState=new Map();   // coordinate -> {events:Map, ann, loadedAt, loading, relays:Set, first}
  let host=null;               // {repaint, notify, viewer, roomChannels} handed in by concord.js
  const P=()=>window.__PCConcordP?window.__PCConcordP():window.__PC;
  const relayDoor=()=>window.PCConcordRelay;
  const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const me=()=>{try{const v=P().viewer();return v&&v.pubkey||'';}catch(_){return '';}};

  function stateOf(coord){
    let s=repoState.get(coord);
    if(!s){s={events:new Map(),ann:null,loadedAt:0,loading:null,relays:new Set(),first:true};repoState.set(coord,s);loadCache(coord,s);}
    return s;
  }
  function loadCache(coord,s){
    try{const raw=localStorage.getItem(CACHE_PREFIX+coord);if(!raw)return;const evs=JSON.parse(raw);
      for(const ev of Array.isArray(evs)?evs:[])if(ev&&isId(ev.id))s.events.set(ev.id,ev);
      s.first=false;const a=[...s.events.values()].filter(e=>e.kind===ANN).map(parseAnnouncement).filter(Boolean).sort((x,y)=>y.createdAt-x.createdAt)[0];if(a)s.ann=a;}catch(_){}
  }
  function saveCache(coord,s){
    try{const evs=[...s.events.values()].sort((a,b)=>b.created_at-a.created_at).slice(0,CACHE_MAX_EVENTS);
      let raw=JSON.stringify(evs);while(raw.length>CACHE_MAX_BYTES&&evs.length>20){evs.splice(Math.floor(evs.length*0.8));raw=JSON.stringify(evs);}
      localStorage.setItem(CACHE_PREFIX+coord,raw);}catch(_){}
  }
  function verify(ev){try{const NT=window.NostrTools;return !NT||!NT.verifyEvent||NT.verifyEvent(ev);}catch(_){return false;}}
  /* Add events; answer how many were new. Each one is signature-checked: anyone can answer a REQ. */
  function absorb(coord,s,evs){
    let added=0;
    for(const ev of evs||[]){if(!ev||!isId(ev.id)||s.events.has(ev.id)||!verify(ev))continue;s.events.set(ev.id,ev);added++;
      if(ev.kind===ANN){const a=parseAnnouncement(ev);if(a&&a.address.coordinate===coord&&(!s.ann||a.createdAt>s.ann.createdAt))s.ann=a;}}
    return added;
  }
  function relaysFor(coord,hints){
    const s=stateOf(coord);for(const h of hints||[])s.relays.add(h);
    const ann=s.ann?s.ann.relays:[];
    return unique([...s.relays,...ann].map(normRelay).filter(Boolean)).filter(u=>u!==DISCOVERY_RELAY).slice(0,8);
  }
  async function query(relays,filters,timeout=8000){
    const door=relayDoor();if(!door||!relays.length)return [];
    try{const r=await door.queryFrom(relays,filters,{timeout,max:8,exact:true,purpose:'concord git'});return Array.isArray(r)?r:[];}catch(_){return [];}
  }
  /* One bounded refresh of a repository: its announcement, its tickets + CI, then every child of
   * those tickets and the deletions that name them. Returns the number of new events. */
  async function refreshRepo(coord,hints,{deep=false}={}){
    const s=stateOf(coord);if(s.loading)return s.loading;
    const address=parseAddress(coord);if(!address)return 0;
    s.loading=(async()=>{
      let added=0;
      /* THE REPOSITORY'S OWN RELAYS FIRST; THE INDEX ALONGSIDE, NEVER IN FRONT. index.ngit.dev is an outside relay,
       * i.e. a tunnelled dial (Tor), and this used to ask it for the announcement -- waiting up to 8s -- before asking
       * the relays that actually hold the issues: "the projects page ... all 0's", then "finally loaded -- took
       * forever" (2026-10-22). Only a repository whose relays are not known yet has to wait for it. Each step that
       * brings something repaints (`progress`), so the issues are on screen before their comments arrive. */
      const annF=[{kinds:[ANN],authors:[address.owner],'#d':[address.identifier],limit:4}];
      const fromIndex=query([DISCOVERY_RELAY],annF);
      let relays=relaysFor(coord,hints);
      if(relays.length)added+=absorb(coord,s,await query(relays,annF));
      else{added+=absorb(coord,s,await fromIndex);relays=relaysFor(coord,hints);}
      if(!relays.length){s.loadedAt=Date.now();return added;}
      relays=relaysFor(coord,hints);
      const pages=deep?5:1;let until=0;
      for(let i=0;i<pages;i++){
        const f={kinds:TICKET_KINDS,'#a':[coord],limit:deep?100:300};if(until)f.until=until;
        const got=await query(relays,[f,...(i===0?[{kinds:CI_KINDS,'#a':[coord],limit:200}]:[])]);const n=absorb(coord,s,got);added+=n;if(n)progress();
        const roots=got.filter(e=>TICKET_KINDS.includes(e.kind));if(roots.length<f.limit)break;until=Math.min(...roots.map(e=>e.created_at))-1;
      }
      const rootIds=[...s.events.values()].filter(e=>TICKET_KINDS.includes(e.kind)).map(e=>e.id);
      for(let i=0;i<rootIds.length;i+=100){const ids=rootIds.slice(i,i+100);
        added+=absorb(coord,s,await query(relays,[{kinds:[COMMENT],'#E':ids,limit:2000},{kinds:STATUS_KINDS,'#e':ids,limit:2000}]));}
      const all=[...s.events.values()].filter(e=>e.kind!==ANN&&e.kind!==DELETION).map(e=>e.id);
      for(let i=0;i<all.length;i+=200)added+=absorb(coord,s,await query(relays,[{kinds:[DELETION],'#e':all.slice(i,i+200),limit:2000}]));
      added+=absorb(coord,s,await fromIndex);   // a newer announcement the repository's relays did not have
      s.loadedAt=Date.now();
      if(added)saveCache(coord,s);
      return added;
    })().finally(()=>{s.loading=null;});
    return s.loading;
  }
  /* A step of a refresh brought events: show them now, not when the whole refresh is done. */
  function progress(){if(projectsOpen)renderProjects();if(host&&host.repaint)host.repaint();}
  function eventsOf(coords){const out=[];for(const c of coords){const s=repoState.get(c);if(s)out.push(...s.events.values());}return out;}
  function annsOf(coords){return coords.map(c=>repoState.get(c)&&repoState.get(c).ann).filter(Boolean);}

  /* ── what a room has attached ── */
  function roomAttachments(room){
    const out=[];
    for(const ch of (room&&room.channels)||[])for(const a of attachmentsOf(ch))out.push({...a,channel:ch});
    return out;
  }
  /* Active repositories of a room, one per coordinate, hints unioned, channel names collected. */
  function activeRepos(room){
    const by=new Map();
    for(const a of roomAttachments(room).filter(isActive)){
      const c=a.address.coordinate,cur=by.get(c)||{address:a.address,relayHints:[],channels:[]};
      cur.relayHints=unique([...cur.relayHints,...a.relayHints]);if(!cur.channels.includes(a.channel.name))cur.channels.push(a.channel.name);by.set(c,cur);
    }
    return [...by.values()];
  }
  const hasRepos=room=>activeRepos(room).length>0;
  function channelActivities(room,channel){
    const atts=attachmentsOf(channel);if(!atts.length)return [];
    const coords=unique(atts.map(a=>a.address.coordinate));
    for(const a of atts)relaysFor(a.address.coordinate,a.relayHints);
    const muted=mutedSet();
    return timelineActivities(eventsOf(coords),atts,annsOf(coords)).filter(a=>!muted.has(a.author));
  }
  function mutedSet(){try{const m=P().mutedPubkeys;return new Set(typeof m==='function'?m():[]);}catch(_){return new Set();}}

  /* ── refresh scheduling: only what is on screen, at most once a minute per repo ── */
  let ticking=null,watched={room:null,channel:null,projects:false};
  function watch(room,channel,projects){
    watched={room,channel,projects:!!projects};
    if(!ticking)ticking=setInterval(()=>{if(document.hidden||!document.body.classList.contains('concord-view'))return;void refreshWatched(false);},REFRESH_MS/2);
    void refreshWatched(false);
  }
  async function refreshWatched(force){
    const room=watched.room;if(!room)return;
    const targets=new Map();
    // The whole room's repos, so another channel's unread dot moves too (Armada useCommunityGitActivity).
    for(const r of activeRepos(room))targets.set(r.address.coordinate,r.relayHints);
    for(const a of attachmentsOf(watched.channel))if(!targets.has(a.address.coordinate))targets.set(a.address.coordinate,a.relayHints);
    let changed=0;const fresh=[];
    await Promise.all([...targets].map(async([coord,hints])=>{
      const s=stateOf(coord);if(!force&&Date.now()-s.loadedAt<REFRESH_MS)return;
      const before=new Set(s.events.keys()),wasFirst=s.first;
      const n=await refreshRepo(coord,hints,{deep:watched.projects&&s.first});s.first=false;
      if(n){changed+=n;if(!wasFirst)for(const [id,ev] of s.events)if(!before.has(id))fresh.push(ev);}
    }));
    if(fresh.length)notifyFresh(room,fresh);
    if(changed&&host&&host.repaint)host.repaint();
    if(changed&&projectsOpen)renderProjects();
    if(changed&&panel.ticket)renderPanel();
  }
  /* New activity from someone else, in a channel this account is not looking at, raises an OS
   * notification — the same three triggers as Armada (ticket opened, comment, status by the ticket
   * author or the repo owner). The first fetch of a repo is silent: history is not news. */
  function notifyFresh(room,fresh){
    const p=P(),mine=me();if(!p||!p.osNotify)return;
    if(window.PCConcord&&PCConcord.notificationsOn&&!PCConcord.notificationsOn())return;   // the bell is off
    for(const ch of room.channels||[]){
      const acts=channelActivities(room,ch),ids=new Set(fresh.map(e=>e.id));
      for(const a of acts){
        if(!ids.has(a.id)||a.author===mine||a.type==='ci-run')continue;
        if(host&&host.viewing&&host.viewing(room,ch.name))continue;
        if(a.type==='status-change'){const ann=stateOf(a.repository.coordinate).ann;if(a.author!==a.ticket.author&&a.author!==(ann?ann.owner:a.repository.owner))continue;}
        const who=nameOf(a.author),what=a.type==='ticket-opened'?(a.ticket.type==='issue'?'opened an issue':'opened a pull request'):a.type==='comment'?'commented on a ticket':'changed a ticket status';
        try{p.osNotify(who+' '+what+' in '+a.repository.identifier,a.ticket.subject||'New activity in #'+ch.name,
          {tag:'concord-git-'+a.id,route:host&&host.route?host.route(room,ch.name,a.ticket.id):''});}catch(_){}
      }
    }
  }
  /* The newest git activity in a channel by someone else, in ms — concord.js ORs it into "unread". */
  function latestAt(room,channel){
    const mine=me();let max=0;
    for(const a of channelActivities(room,channel))if(a.author!==mine&&a.createdAt*1000>max)max=a.createdAt*1000;
    return max;
  }

  /* ───────────────────────────── UI helpers ───────────────────────────── */
  function nameOf(pk){try{const pr=P().profOf(pk)||{};return pr.display_name||pr.name||npubShort(pk);}catch(_){return npubShort(pk);}}
  function avatarOf(pk){try{const pr=P().profOf(pk)||{};return pr.picture||P().LOGO||'';}catch(_){return '';}}
  function npubShort(pk){try{const n=window.NostrTools.nip19.npubEncode(pk);return n.slice(0,12)+'…'+n.slice(-4);}catch(_){return String(pk||'').slice(0,10)+'…';}}
  function want(pks){try{const p=P();for(const pk of unique(pks))if(pk&&p.needProfile)p.needProfile(pk);}catch(_){}}
  function ago(sec){const d=Math.max(0,Date.now()/1000-sec);if(d<60)return 'just now';if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';if(d<2592000)return Math.floor(d/86400)+'d ago';return new Date(sec*1000).toLocaleDateString();}
  const repoName=coord=>{const s=repoState.get(coord);return s&&s.ann?s.ann.name:(parseAddress(coord)||{}).identifier||'repository';};
  const STATUS_LABEL={open:'Open',draft:'Draft',closed:'Closed',merged:'Merged',resolved:'Resolved'};
  const ICON={issue:'🐞','pull-request':'⑂',comment:'💬',status:'●',ci:'⚙'};
  function statusChip(st){return `<span class="ccg-status ccg-st-${enc(st)}">${enc(STATUS_LABEL[st]||st)}</span>`;}
  function who(pk){return `<button type="button" class="ccg-who" data-ccg-profile="${enc(pk)}"><img src="${enc(avatarOf(pk))}" alt="" loading="lazy"><b>${enc(nameOf(pk))}</b></button>`;}

  /* Markdown, document mode (Armada renders tickets this way): headings, emphasis, code, links,
   * lists, quotes — everything ESCAPED first, links https/http only, bare image URLs inline. */
  function md(src,tags){
    /* A bare media URL is the picture (Armada/gitworkshop attach that way, with an imeta `m`). */
    const types=new Map();for(const t of tags||[])if(t[0]==='imeta'){const u=(t.find(v=>/^url /.test(v))||'').slice(4).trim();if(u)types.set(u,(t.find(v=>/^m /.test(v))||'').slice(2).trim());}
    const media=u=>{const m=types.get(u.replace(/&amp;/g,'&'))||'';if(/^image\//.test(m)||(!m&&/\.(png|jpe?g|gif|webp|avif)(\?\S*)?$/i.test(u)))return 'img';if(/^video\//.test(m)||(!m&&/\.(mp4|webm|mov|m4v)(\?\S*)?$/i.test(u)))return 'video';return '';};
    const blocks=[],text=String(src||'').replace(/```([^\n]*)\n([\s\S]*?)```/g,(_,l,code)=>{blocks.push(code);return '\u0000'+(blocks.length-1)+'\u0000';});
    const inline=s=>enc(s)
      .replace(/`([^`]+)`/g,'<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/(^|[^*])\*([^*\s][^*]*)\*/g,'$1<i>$2</i>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,(_,t,u)=>`<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`)
      .replace(/(^|[\s(])(https?:\/\/[^\s<]+)/g,(_,pre,u)=>media(u)==='img'?`${pre}<img class="ccg-img" src="${u}" alt="" loading="lazy">`:media(u)==='video'?`${pre}<video class="ccg-img" src="${u}" controls preload="metadata" playsinline></video>`:`${pre}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
    const out=[];let list=null;
    for(const line of text.split('\n')){
      const h=line.match(/^(#{1,6})\s+(.*)$/),li=line.match(/^\s*([-*]|\d+\.)\s+(.*)$/),q=line.match(/^>\s?(.*)$/),cb=line.match(/^\u0000(\d+)\u0000$/);
      if(li){const tag=/\d/.test(li[1])?'ol':'ul';if(!list||list!==tag){if(list)out.push(`</${list}>`);out.push(`<${tag}>`);list=tag;}out.push(`<li>${inline(li[2])}</li>`);continue;}
      if(list){out.push(`</${list}>`);list=null;}
      if(cb)out.push(`<pre><code>${enc(blocks[+cb[1]])}</code></pre>`);
      else if(h)out.push(`<h${Math.max(4,h[1].length+3)>6?6:Math.max(4,h[1].length+3)}>${inline(h[2])}</h${Math.max(4,h[1].length+3)>6?6:Math.max(4,h[1].length+3)}>`);
      else if(q)out.push(`<blockquote>${inline(q[1])}</blockquote>`);
      else if(line.trim())out.push(`<p>${inline(line)}</p>`);
    }
    if(list)out.push(`</${list}>`);
    return out.join('').replace(/\u0000(\d+)\u0000/g,(_,i)=>`<code>${enc(blocks[+i])}</code>`);
  }

  /* ── timeline rows ── */
  function rowHtml(group){
    const e=group.entries,head=e[e.length-1],repo=head.repository.identifier;
    want(e.map(x=>x.author));
    if(head.type==='ticket-opened'){
      const kind=head.ticket.type==='issue'?'issue':'pull request';
      const verb=e.length>1?`opened ${e.length} ${kind==='issue'?'issues':'pull requests'}`:`opened ${kind==='issue'?'an issue':'a pull request'}`;
      return `<div class="ccg-row ccg-ticket" data-message-id="${enc(head.ticket.id)}"><span class="ccg-ico" aria-hidden="true">${head.ticket.type==='issue'?ICON.issue:ICON['pull-request']}</span><div class="ccg-main"><div class="ccg-line">${who(head.author)} <span>${enc(verb)} in <span class="ccg-repo">${enc(repo)}</span></span><time>${enc(ago(head.createdAt))}</time></div>${e.map(x=>`<button type="button" class="ccg-subject" data-ccg-ticket="${enc(x.ticket.id)}">${enc(x.ticket.subject)}</button>`).join('')}${e.length===1&&previewText(head.ticket.content)?`<div class="ccg-preview" data-ccg-ticket="${enc(head.ticket.id)}">${enc(previewText(head.ticket.content))}</div>`:''}</div></div>`;
    }
    if(head.type==='comment'){
      let prev='';
      const lines=e.map(x=>{const cont=x.author===prev;prev=x.author;return `<div class="ccg-comment${cont?' ccg-cont':''}">${cont?'':who(x.author)}<button type="button" class="ccg-preview" data-ccg-ticket="${enc(x.ticket.id)}">${enc(previewText(x.comment.content,160)||'(attachment)')}</button></div>`;}).join('');
      return `<div class="ccg-row ccg-comments" data-message-id="${enc(head.comment.id)}"><span class="ccg-ico" aria-hidden="true">${ICON.comment}</span><div class="ccg-main"><div class="ccg-line"><span>on</span> <button type="button" class="ccg-subject ccg-inline" data-ccg-ticket="${enc(head.ticket.id)}">${enc(head.ticket.subject)}</button> <span class="ccg-repo">${enc(repo)}</span><time>${enc(ago(head.createdAt))}</time></div>${lines}</div></div>`;
    }
    if(head.type==='status-change'){
      const st=statusFromKind(head.status.kind,head.ticket.kind),verb={closed:'closed',merged:'merged',resolved:'resolved',draft:'marked as draft',open:'reopened'}[st];
      return `<div class="ccg-row ccg-notice" data-message-id="${enc(head.status.event.id)}"><span class="ccg-ico ccg-st-${enc(st)}" aria-hidden="true">${ICON.status}</span><div class="ccg-main"><div class="ccg-line">${who(head.author)} <span>${enc(verb)}</span> <button type="button" class="ccg-subject ccg-inline" data-ccg-ticket="${enc(head.ticket.id)}">${enc(head.ticket.subject)}</button>${e.length>1?`<span class="ccg-muted"> · ${e.length} status changes</span>`:''}<time>${enc(ago(head.createdAt))}</time></div></div></div>`;
    }
    const runs=e.map(x=>x.run),groups=ciGroups(runs),outcome=groups.some(g=>ciFailing(ciOutcome(g.latest)))?'failure':groups.some(g=>/queued|in_progress/.test(ciOutcome(g.latest)))?'in_progress':'success';
    return `<div class="ccg-row ccg-ci" data-message-id="${enc(head.run.id)}"><span class="ccg-ico ccg-ci-${enc(outcome)}" aria-hidden="true">${ICON.ci}</span><div class="ccg-main"><div class="ccg-line"><b>CI</b> <span class="ccg-muted">· ${runs.length} ${runs.length===1?'run':'runs'} · ${enc(ciSummary(groups))}</span> <span class="ccg-repo">${enc(repo)}</span><time>${enc(ago(head.createdAt))}</time></div>${groups.map(g=>ciGroupHtml(g)).join('')}</div></div>`;
  }
  function ciGroupHtml(g){
    const r=g.latest,o=ciOutcome(r),word={success:'succeeded',failure:'failed',cancelled:'was cancelled',skipped:'was skipped',timed_out:'timed out',startup_failure:'failed to start',neutral:'finished',queued:'is queued',in_progress:'is running'}[o]||o;
    const before=g.runs.slice(1,13).map(x=>`<span class="ccg-dot ccg-ci-${enc(ciOutcome(x))}" title="${enc(ciOutcome(x)+' · '+(x.commit||'').slice(0,7))}"></span>`).join('');
    const jobs=r.jobs.map(j=>{const res=j.result,jo=res&&res.conclusion||'pending';return `<li><span class="ccg-dot ccg-ci-${enc(jo)}"></span> ${enc(res&&res.name||j.job)} <span class="ccg-muted">${enc(jo)}</span>${res&&res.logs?` <a href="${enc(res.logs)}" target="_blank" rel="noopener noreferrer">logs</a>`:''}${res&&res.event.content?`<pre class="ccg-log">${enc(String(res.event.content).slice(-4000))}</pre>`:''}</li>`;}).join('');
    return `<details class="ccg-ci-run"><summary><span class="ccg-dot ccg-ci-${enc(o)}"></span> <b>${enc(g.name)}</b> ${enc(word)}${r.commit?` on <code>${enc(r.commit.slice(0,7))}</code>`:''} <span class="ccg-muted">· reported by ${enc(nameOf(r.author))}</span>${before?` <span class="ccg-before" title="before that">${before}</span>`:''}</summary>${jobs?`<ul class="ccg-jobs">${jobs}</ul>`:'<p class="ccg-muted">No job results yet.</p>'}</details>`;
  }
  /* What concord.js's message pane calls: chat list in, chat + git rows out. */
  function paneRows(room,channel,list){
    if(!room||!channel)return list;
    const acts=channelActivities(room,channel);
    return mergeTimeline(list,acts).map(x=>x&&x.ccGit?{ccGit:true,at:x.at,html:rowHtml(x)}:x);
  }
  function channelHasGit(channel){return attachmentsOf(channel).length>0;}
  /* WHAT A REPOSITORY CHANNEL IS, AT ITS TOP. The channel shows only activity from INSIDE its attachment interval
   * (Armada's rule, kept so an Armada member sees the same channel) -- so one connected a minute ago is EMPTY however
   * many issues the repository has, and with no messages it did not even draw the channel's welcome: "the Git channel
   * is empty in concord after connecting the repo" (2026-10-22, 93 issues on its relays). This names the repository,
   * says what will appear here and since when, and opens the existing issues. One card per ACTIVE attachment. */
  function channelIntroHtml(room,channel){
    const atts=attachmentsOf(channel).filter(isActive);if(!atts.length)return '';
    return atts.map(a=>{const coord=a.address.coordinate;relaysFor(coord,a.relayHints);want([a.address.owner]);
      const s=stateOf(coord),items=projectItems(eventsOf([coord]),repoList([{address:a.address}])),
        issues=items.filter(x=>x.ticket.type!=='pull-request'),open=issues.filter(x=>x.status==='open').length,prs=items.length-issues.length;
      const since=new Date(a.attachedAt*1000).toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'});
      const counts=items.length?`${open} open of ${issues.length} issue${issues.length===1?'':'s'}${prs?` · ${prs} pull request${prs===1?'':'s'}`:''}`
        :(s.loading||!s.loadedAt?'Loading repository activity…':'No issues yet');
      return `<div class="ccg-intro" data-ccg-intro="${enc(coord)}"><img src="${enc(avatarOf(a.address.owner))}" alt=""><div><b>${enc(nameOf(a.address.owner))} / ${enc(repoName(coord))}</b>`
        +`<small>${enc(counts)}</small><small>New issues, comments and status changes appear in this channel from ${enc(since)}, when the repository was connected.</small></div>`
        +`<button type="button" class="btn btn-ghost small" data-ccg-intro-open="${enc(coord)}">Issues</button></div>`;}).join('');
  }

  /* ───────────────────────────── ticket panel ───────────────────────────── */
  const panel={ticket:null,room:null,editing:'',sending:false};
  function findTicket(id){
    for(const s of repoState.values()){const ev=s.events.get(id);if(ev){const t=parseTicket(ev);if(t)return t;}}
    return null;
  }
  function repoOfTicket(t){
    const room=panel.room||(host&&host.room&&host.room());
    const held=new Set([...(room?roomAttachments(room):[]).map(a=>a.address.coordinate),...repoState.keys()]);
    const addr=t.repositoryAddresses.find(a=>held.has(a.coordinate))||t.repositoryAddress;if(!addr)return null;
    const s=stateOf(addr.coordinate);return {address:addr,owner:s.ann?s.ann.owner:addr.owner,maintainers:s.ann?s.ann.maintainers:[],ann:s.ann,coordinate:addr.coordinate};
  }
  function ticketData(t){
    const repo=repoOfTicket(t),evs=repo?[...stateOf(repo.coordinate).events.values()]:[],dels=collectDeletions(evs);
    const st=repo?resolveStatus(t,repo,evs):undefined;
    const comments=evs.map(parseComment).filter(c=>c&&c.ticketId===t.id&&!isDeleted(dels,c.id,c.author)).sort((a,b)=>a.createdAt-b.createdAt||a.id.localeCompare(b.id));
    return {repo,status:statusFromKind(st&&st.kind,t.kind),comments};
  }
  function openTicket(id,room){
    const t=findTicket(id);if(!t)return false;
    panel.ticket=t;panel.room=room||(host&&host.room&&host.room())||panel.room;panel.editing='';
    renderPanel();
    const repo=repoOfTicket(t);
    if(repo){const s=stateOf(repo.coordinate);s.loadedAt=0;void refreshRepo(repo.coordinate,relaysFor(repo.coordinate,[])).then(n=>{if(n&&panel.ticket&&panel.ticket.id===id){renderPanel();if(host&&host.repaint)host.repaint();}});}
    return true;
  }
  function closePanel(){panel.ticket=null;const el=document.getElementById('ccg-panel');if(el)el.remove();document.body.classList.remove('ccg-panel-open');}
  function memberSet(){try{return new Set(host&&host.members?host.members():[]);}catch(_){return new Set();}}
  function renderPanel(){
    const t=panel.ticket;if(!t)return;
    let el=document.getElementById('ccg-panel');
    const keep=el?{draft:(el.querySelector('#ccg-reply')||{}).value||'',edit:(el.querySelector('.ccg-edit-box')||{}).value,scroll:(el.querySelector('.ccg-panel-body')||{}).scrollTop||0}:null;
    if(!el){el=document.createElement('aside');el.id='ccg-panel';el.className='ccg-panel';el.setAttribute('role','dialog');el.setAttribute('aria-label','Conversation');document.body.appendChild(el);document.body.classList.add('ccg-panel-open');}
    const {repo,status,comments}=ticketData(t),viewer=me(),members=memberSet();
    want([t.author,...comments.map(c=>c.author)]);
    const opts=repo&&canSetStatus(t,repo,viewer)?statusOptions(t,status):[];
    const guest=pk=>members.size&&!members.has(pk)?'<span class="ccg-guest" title="Not a member of this community">Guest</span>':'';
    const wsUrl=gitworkshopUrl(t,window.NostrTools&&window.NostrTools.nip19);
    el.innerHTML=`<header class="ccg-panel-head"><div><small>${t.type==='issue'?'Issue':'Pull request'}</small><h3>${enc(t.subject)}</h3><p>${enc(repo?(repo.ann?repo.ann.name:repo.address.identifier):'')} · ${statusChip(status)}${t.labels.length?' '+t.labels.map(l=>`<span class="ccg-label">${enc(l)}</span>`).join(' '):''}</p></div><button type="button" class="ccg-x" data-ccg-close aria-label="Close conversation">×</button></header>
      ${opts.length?`<div class="ccg-status-actions">${opts.map(o=>`<button type="button" class="btn btn-ghost small" data-ccg-status="${o.kind}">${enc(o.label)}</button>`).join('')}</div>`:''}
      <div class="ccg-panel-body">
        <article class="ccg-post">${who(t.author)}${guest(t.author)}<span class="ccg-muted"> opened · ${enc(ago(t.createdAt))}</span>${t.branches?`<p class="ccg-muted">${enc([t.branches.name,t.branches.base&&('base '+t.branches.base),t.branches.head&&('head '+t.branches.head.slice(0,10))].filter(Boolean).join(' · '))}</p>`:''}<div class="ccg-md">${md(t.content,t.event.tags)||'<p class="ccg-muted">No description.</p>'}</div></article>
        ${comments.length?comments.map(c=>`<article class="ccg-post" data-ccg-comment="${enc(c.id)}">${who(c.author)}${guest(c.author)}<span class="ccg-muted"> commented · ${enc(ago(c.createdAt))}</span>${c.author===viewer?`<span class="ccg-own"><button type="button" data-ccg-edit="${enc(c.id)}" title="Edit comment">✎</button><button type="button" data-ccg-delete="${enc(c.id)}" title="Delete comment">⌫</button></span>`:''}${panel.editing===c.id?`<textarea class="input ccg-edit-box" rows="4">${enc(c.content)}</textarea><div class="ccg-edit-actions"><button type="button" class="btn btn-neon small" data-ccg-edit-save="${enc(c.id)}">Save</button><button type="button" class="btn btn-ghost small" data-ccg-edit-cancel>Cancel</button></div>`:`<div class="ccg-md">${md(c.content,c.event.tags)}</div>`}</article>`).join(''):'<p class="ccg-muted ccg-empty">No comments yet.</p>'}
        <p class="ccg-links">${wsUrl?`<a href="${enc(wsUrl)}" target="_blank" rel="noopener noreferrer">Open on gitworkshop</a> · `:''}<button type="button" class="ccg-linkbtn" data-ccg-json>View event JSON</button></p>
      </div>
      <footer class="ccg-panel-foot">${viewer?`<div class="ccg-compose"><button type="button" class="ccg-attach" data-ccg-attach title="Attach file" aria-label="Attach file"><svg class="ic" aria-hidden="true"><use href="#i-paperclip"></use></svg></button><input type="file" id="ccg-file" multiple hidden><textarea id="ccg-reply" rows="2" placeholder="Comment on this ${t.type==='issue'?'issue':'pull request'}…"></textarea><button type="button" class="btn btn-neon small" data-ccg-send>Comment</button></div><small class="ccg-muted" id="ccg-note">Public: repository discussion is visible outside this community.</small>`:'<small class="ccg-muted">Sign in to comment.</small>'}</footer>`;
    if(keep){const r=el.querySelector('#ccg-reply');if(r)r.value=keep.draft;const e=el.querySelector('.ccg-edit-box');if(e&&keep.edit!=null)e.value=keep.edit;const b=el.querySelector('.ccg-panel-body');if(b)b.scrollTop=keep.scroll;}
    bindPanel(el,t,repo);
  }
  function bindPanel(el,t,repo){
    const p=P(),note=el.querySelector('#ccg-note');
    el.querySelector('[data-ccg-close]').onclick=closePanel;
    el.querySelectorAll('[data-ccg-status]').forEach(b=>b.onclick=async()=>{
      if(b.disabled||!repo)return;b.disabled=true;
      const r=await publishTo(repo,statusTemplate(t,repo,+b.dataset.ccgStatus,relayHint(repo)));
      p.toast(r.ok?'status updated':r.queued?'Not confirmed yet — it will be re-sent':'status was not changed: '+r.msg);renderPanel();if(host&&host.repaint)host.repaint();});
    const ta=el.querySelector('#ccg-reply');
    if(ta){
      if(p.attachUserAutocomplete)try{p.attachUserAutocomplete(ta);}catch(_){}
      ta.onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();send();}};
      el.querySelector('[data-ccg-attach]').onclick=()=>el.querySelector('#ccg-file').click();
      el.querySelector('#ccg-file').onchange=async e=>{await attachFiles(e.target.files,ta,note);e.target.value='';};
      el.querySelector('[data-ccg-send]').onclick=send;
    }
    async function send(){
      const text=ta.value.trim();if(!text||panel.sending||!repo)return;panel.sending=true;
      const media=await mediaTags(text);
      const r=await publishTo(repo,commentTemplate(t,text,relayHint(repo),media));panel.sending=false;
      if(r.ok||r.queued){ta.value='';if(r.queued)p.toast('Comment not confirmed yet — it will be re-sent');renderPanel();if(host&&host.repaint)host.repaint();}
      else p.toast('comment was not sent: '+r.msg);
    }
    el.querySelectorAll('[data-ccg-profile]').forEach(b=>b.onclick=()=>{try{p.openProfile(b.dataset.ccgProfile);}catch(_){}});
    el.querySelectorAll('[data-ccg-edit]').forEach(b=>b.onclick=()=>{panel.editing=b.dataset.ccgEdit;renderPanel();});
    const cancel=el.querySelector('[data-ccg-edit-cancel]');if(cancel)cancel.onclick=()=>{panel.editing='';renderPanel();};
    const save=el.querySelector('[data-ccg-edit-save]');
    if(save)save.onclick=async()=>{
      const old=findEvent(save.dataset.ccgEditSave),box=el.querySelector('.ccg-edit-box');if(!old||!box||!repo)return;
      const text=box.value.trim();if(!text||text===old.content){panel.editing='';renderPanel();return;}
      save.disabled=true;
      /* Armada's edit: retract the old comment first, then republish under the ORIGINAL created_at,
       * keeping the imeta tags whose URL is still in the text. */
      const del=await publishTo(repo,deletionTemplate(old));
      if(!del.ok&&!del.queued){save.disabled=false;p.toast('comment was not edited: '+del.msg);return;}
      const keepMedia=(old.tags||[]).filter(x=>x[0]==='imeta'&&x.some(v=>/^url /.test(v)&&text.split(/\s+/).includes(v.slice(4))));
      const tpl=commentTemplate(t,text,relayHint(repo),unique([...keepMedia,...(await mediaTags(text))].map(x=>JSON.stringify(x))).map(x=>JSON.parse(x)));
      tpl.created_at=old.created_at;
      const r=await publishTo(repo,tpl);panel.editing='';
      p.toast(r.ok?'comment edited':r.queued?'Edit not confirmed yet':'edit was not saved: '+r.msg);renderPanel();if(host&&host.repaint)host.repaint();};
    el.querySelectorAll('[data-ccg-delete]').forEach(b=>b.onclick=async()=>{
      const ev=findEvent(b.dataset.ccgDelete);if(!ev||!repo)return;
      const ok=p.uiConfirm?await p.uiConfirm('Delete this comment? A deletion is a request (NIP-09): relays and clients that already have it may keep showing it.',{ok:'Delete',danger:true}):true;
      if(!ok)return;const r=await publishTo(repo,deletionTemplate(ev));
      p.toast(r.ok?'deletion requested':r.queued?'Deletion not confirmed yet':'comment was not deleted: '+r.msg);renderPanel();if(host&&host.repaint)host.repaint();});
    const json=el.querySelector('[data-ccg-json]');
    if(json)json.onclick=()=>{if(p.modal)p.modal(`<h3>Event JSON</h3><pre class="ccg-json">${enc(JSON.stringify(t.event,null,2))}</pre><div class="set-actions"><button class="btn btn-ghost small" id="ccg-json-copy">Copy</button></div>`,root=>{root.querySelector('#ccg-json-copy').onclick=()=>p.copyValue&&p.copyValue(JSON.stringify(t.event));});};
  }
  function findEvent(id){for(const s of repoState.values()){const e=s.events.get(id);if(e)return e;}return null;}
  const relayHint=repo=>relaysFor(repo.coordinate||repo.address.coordinate,[])[0]||'';

  /* PUBLISH: sign once, keep it locally at once (it shows immediately), send to the repo's activity
   * relays. Three outcomes, never two: stored, refused, or not yet confirmed (`queued`) — a lost OK
   * is not a failure, and the same signed event is re-sent on the next refresh until one relay
   * accepts it. Re-sending an event is harmless (one id); signing a second one is a duplicate. */
  const outbox=new Map();   // id -> {ev, coord}
  async function publishTo(repo,template){
    const p=P(),coord=repo.coordinate||repo.address.coordinate;
    if(!me())return {ok:false,msg:'sign in to take part'};
    const relays=relaysFor(coord,[]);if(!relays.length)return {ok:false,msg:'this repository has no reachable activity relays'};
    let ev;try{ev=await p.signTemplate({kind:template.kind,created_at:template.created_at||Math.floor(Date.now()/1000),tags:template.tags,content:template.content});}catch(e){return {ok:false,msg:(e&&e.message)||'not signed'};}
    if(!ev||!ev.id)return {ok:false,msg:'not signed'};
    const s=stateOf(coord);s.events.set(ev.id,ev);saveCache(coord,s);
    return sendHeld(ev,coord,relays);
  }
  async function sendHeld(ev,coord,relays){
    const door=relayDoor();let r=null;
    try{r=await door.publishTo(relays,ev,{timeout:8000,max:8,detailed:true});}catch(e){r={ok:false,msg:(e&&e.message)||'failed'};}
    if(r&&r.ok){outbox.delete(ev.id);return {ok:true};}
    if(!r||r.uncertain||/timeout|closed|connect|socket/i.test(String(r.msg||''))){outbox.set(ev.id,{ev,coord});armOutbox();return {ok:false,queued:true,msg:'not confirmed'};}
    const s=repoState.get(coord);if(s){s.events.delete(ev.id);saveCache(coord,s);}
    return {ok:false,msg:String(r.msg||'rejected')};
  }
  /* Armed on first use (an event whose delivery is not yet confirmed), never at load: a module that starts a long
   * timer by being loaded costs every page that loads it (tests/client/test_client_timers_at_load.py). Stops itself
   * once nothing is waiting. */
  let outboxTimer=null;
  function armOutbox(){
    if(outboxTimer)return;
    outboxTimer=setInterval(()=>{if(!outbox.size){clearInterval(outboxTimer);outboxTimer=null;return;}if(document.hidden)return;
      for(const {ev,coord} of [...outbox.values()])void sendHeld(ev,coord,relaysFor(coord,[]));},30000);
  }

  async function mediaTags(text){
    try{const tags=await P().imetaTagsFor(text);return (tags||[]).filter(t=>t[0]==='imeta');}catch(_){return [];}
  }
  async function attachFiles(files,ta,note){
    const p=P();files=[...(files||[])];
    for(let i=0;i<files.length;i++){
      if(note)note.textContent=`uploading ${i+1}/${files.length}…`;
      try{const f=files[i],url=await p.uploadBlob(f,{folder:'Posts'});const label=/^(image|video)\//.test(f.type||'')?'':(f.name||'').replace(/\s+/g,' ').trim()+' ';
        ta.value+=(ta.value&&!ta.value.endsWith('\n')?'\n':'')+label+url;}
      catch(e){if(note)note.textContent='upload failed: '+((e&&e.message)||e);return;}
    }
    if(note)note.textContent='Public: repository discussion is visible outside this community.';ta.focus();
  }

  /* ───────────────────────────── repository picker ───────────────────────────── */
  let directory={at:0,repos:[],loading:null};
  async function loadDirectory(){
    if(directory.loading)return directory.loading;
    if(Date.now()-directory.at<300000&&directory.repos.length)return directory.repos;
    directory.loading=(async()=>{
      const home=(()=>{try{return P().homeRelay&&P().homeRelay();}catch(_){return '';}})();
      const relays=unique([DISCOVERY_RELAY,normRelay(home||'')].filter(Boolean));
      /* "PosterChan not even found in Connect a git repository" (2026-10-22): every relay HAS its announcement, but
       * `limit:1000` is the NEWEST thousand (index.ngit.dev answers 500) and an older repository is simply not in the
       * list the search box filters. So your own repositories are asked for by author, always, and typing runs a
       * real search on the index (searchAnnouncements). */
      const mine=me(),evs=await query(relays,[{kinds:[ANN],limit:1000},...(mine?[{kinds:[ANN],authors:[mine],limit:200}]:[])],10000);
      directory={at:Date.now(),repos:foldAnnouncements(evs),loading:null};return directory.repos;
    })().catch(()=>{directory.loading=null;return directory.repos;});
    return directory.loading;
  }
  function foldAnnouncements(evs,base=[]){
    const by=new Map(base.map(a=>[a.address.coordinate,a]));
    for(const ev of evs||[]){if(!verify(ev))continue;const a=parseAnnouncement(ev);if(!a)continue;const c=by.get(a.address.coordinate);if(!c||a.createdAt>c.createdAt)by.set(a.address.coordinate,a);}
    return [...by.values()];
  }
  /* NIP-50 on the index (index.ngit.dev answers it; a relay that does not simply returns nothing). */
  const searched=new Map();
  async function searchAnnouncements(text){
    const k=String(text||'').trim().toLowerCase();if(k.length<2)return [];
    if(!searched.has(k))searched.set(k,query([DISCOVERY_RELAY],[{kinds:[ANN],search:k,limit:50}],8000).then(evs=>foldAnnouncements(evs)).catch(()=>[]));
    return searched.get(k);
  }
  async function resolveNip05(name){
    try{const NT=window.NostrTools;if(NT&&NT.nip05&&NT.nip05.queryProfile){const r=await NT.nip05.queryProfile(name);return r&&r.pubkey;}}catch(_){}
    return null;
  }
  /* Fetch the newest announcement for a resolved address (input hints first, then discovery);
   * Armada refuses a repository whose announcement names no relays — there is nowhere to read it. */
  async function fetchAnnouncement(resolved){
    /* This node's relay too: a repository hosted HERE (a /r/ share link carries no relay) is announced on it,
     * and may be on no public index at all. */
    let home='';try{home=normRelay(P()&&P().homeRelay&&P().homeRelay()||'');}catch(_){home='';}
    const relays=unique([...normRelays(resolved.relayHints),...(home?[home]:[]),DISCOVERY_RELAY]).slice(0,10);
    const evs=await query(relays,[{kinds:[ANN],authors:[resolved.address.owner],'#d':[resolved.address.identifier]}]);
    const ann=evs.filter(verify).map(parseAnnouncement).filter(a=>a&&a.address.coordinate===resolved.address.coordinate).sort((a,b)=>b.createdAt-a.createdAt)[0];
    if(!ann)throw new Error('Repository announcement not found.');
    if(!ann.relays.length)throw new Error('Repository announcement has no usable activity relays.');
    const s=stateOf(ann.address.coordinate);absorb(ann.address.coordinate,s,[ann.event]);
    return {address:ann.address,relayHints:normRelays([...ann.relays,...resolved.relayHints]),announcement:ann};
  }
  /* Opens a picker in a modal; resolves with {address, relayHints, announcement} or null. */
  function pickRepository({connected=new Set(),title='Connect a git repository'}={}){
    const p=P();
    return new Promise(resolve=>{
      let done=false;const finish=v=>{if(done)return;done=true;try{p.closeModal();}catch(_){}resolve(v);};
      p.modal(`<h3>${enc(title)}</h3><p class="muted small">Search repositories announced on Nostr, or paste an address — <code>naddr1…</code>, <code>nostr://npub…/repo</code>, a gitworkshop link, a clone address or a <code>/r/</code> share link.</p>
        <input class="input" id="ccg-pick-q" placeholder="Search, or paste an address" autocomplete="off" autocapitalize="none" spellcheck="false">
        <div class="ccg-pick-list" id="ccg-pick-list" role="listbox"><p class="ccg-muted">Loading repositories…</p></div>
        <div class="set-actions"><button class="btn btn-ghost small" id="ccg-pick-cancel">Cancel</button></div>`,rootEl=>{
        const q=rootEl.querySelector('#ccg-pick-q'),list=rootEl.querySelector('#ccg-pick-list');
        rootEl.querySelector('#ccg-pick-cancel').onclick=()=>finish(null);
        const row=a=>{const used=connected.has(a.address.coordinate);want([a.owner]);return `<button type="button" class="ccg-pick-row" data-ccg-pick="${enc(a.address.coordinate)}"${used?' disabled title="Already connected to this community"':''}><img src="${enc(avatarOf(a.owner))}" alt=""><span><b>${enc(nameOf(a.owner))} / ${enc(a.name)}</b>${a.description?`<small>${enc(previewText(a.description,90))}</small>`:''}${used?'<small>Already connected to this community</small>':''}</span></button>`;};
        const paint=async()=>{
          const v=q.value.trim();
          if(/^(naddr1|nostr:|30617:|https?:\/\/)/i.test(v)){list.innerHTML=`<button type="button" class="ccg-pick-row ccg-pick-paste" data-ccg-paste><span><b>Use this repository address</b><small>${enc(v.slice(0,90))}</small></span></button>`;
            list.querySelector('[data-ccg-paste]').onclick=async()=>{list.innerHTML='<p class="ccg-muted">Resolving…</p>';
              try{const r=await resolveInput(v,window.NostrTools.nip19,resolveNip05);if(connected.has(r.address.coordinate))throw new Error('This repository is already connected.');finish(await fetchAnnouncement(r));}
              catch(e){list.innerHTML=`<p class="ccg-error">${enc((e&&e.message)||e)}</p>`;}};return;}
          const [listed,found]=await Promise.all([loadDirectory(),searchAnnouncements(v)]);if(done||q.value.trim()!==v)return;
          const repos=found.length?foldAnnouncements([],[...listed,...found]):listed,hits=searchDirectory(repos,v);
          list.innerHTML=(v?'':'<p class="ccg-muted">Recently announced</p>')+(hits.length?hits.map(row).join(''):`<p class="ccg-muted">${repos.length?'No repository matches.':'No repositories could be loaded — paste an address instead.'}</p>`);
          list.querySelectorAll('[data-ccg-pick]').forEach(b=>b.onclick=()=>{const a=hits.find(x=>x.address.coordinate===b.dataset.ccgPick);if(!a)return;
            const s=stateOf(a.address.coordinate);absorb(a.address.coordinate,s,[a.event]);finish({address:a.address,relayHints:a.relays,announcement:a});});
        };
        let tm=null;q.oninput=()=>{clearTimeout(tm);tm=setTimeout(paint,150);};q.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();const b=list.querySelector('button:not([disabled])');if(b)b.click();}};
        setTimeout(()=>q.focus(),30);void paint();
      });
    });
  }

  /* ───────────────────────────── new issue ───────────────────────────── */
  function newIssue(room,preferred){
    const p=P();if(!me()){p.toast('sign in to open an issue');return;}
    const repos=activeRepos(room);if(!repos.length){p.toast('no repository is connected to this community');return;}
    for(const r of repos)relaysFor(r.address.coordinate,r.relayHints);
    const items=projectItems(eventsOf(repos.map(r=>r.address.coordinate)),repoList(repos));
    let labels=[];
    p.modal(`<h3>New issue</h3>
      ${repos.length>1?`<label class="fld">Repository<select class="input" id="ccg-ni-repo">${repos.map(r=>`<option value="${enc(r.address.coordinate)}"${r.address.coordinate===preferred?' selected':''}>${enc(repoName(r.address.coordinate))}</option>`).join('')}</select></label>`:`<p class="muted small">In <b>${enc(repoName(repos[0].address.coordinate))}</b></p>`}
      <label class="fld">Title<input class="input" id="ccg-ni-title" maxlength="300" placeholder="Short summary"></label>
      <label class="fld">Description<textarea class="input" id="ccg-ni-body" rows="6" placeholder="Describe the issue (optional)"></textarea></label>
      <div class="ccg-labels" id="ccg-ni-labels"></div>
      <div class="row cmp-tools"><button type="button" class="btn btn-ghost small" id="ccg-ni-attach"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg>Attach</button><input type="file" id="ccg-ni-file" multiple hidden><span class="spacer"></span></div>
      <p class="muted small" id="ccg-ni-note">Public: issues are visible outside this community.</p>
      <div class="set-actions"><button class="btn btn-neon small" id="ccg-ni-send">Open issue</button><button class="btn btn-ghost small" id="ccg-ni-cancel">Cancel</button></div>`,rootEl=>{
      const sel=rootEl.querySelector('#ccg-ni-repo'),title=rootEl.querySelector('#ccg-ni-title'),body=rootEl.querySelector('#ccg-ni-body'),note=rootEl.querySelector('#ccg-ni-note'),box=rootEl.querySelector('#ccg-ni-labels');
      const coord=()=>sel?sel.value:repos[0].address.coordinate;
      const paintLabels=()=>{
        const sugg=suggestLabels(items,coord()).filter(l=>!labels.includes(l)).slice(0,8);
        box.innerHTML=`<span class="ccg-muted">Labels</span>${labels.map(l=>`<button type="button" class="ccg-label on" data-ccg-unlabel="${enc(l)}" title="Remove label">${enc(l)} ×</button>`).join('')}${labels.length<MAX_LABELS?sugg.map(l=>`<button type="button" class="ccg-label" data-ccg-addlabel="${enc(l)}">+ ${enc(l)}</button>`).join('')+`<input class="ccg-label-input" id="ccg-ni-label" maxlength="${MAX_LABEL_LENGTH}" placeholder="+ label">`:''}`;
        box.querySelectorAll('[data-ccg-unlabel]').forEach(b=>b.onclick=()=>{labels=labels.filter(x=>x!==b.dataset.ccgUnlabel);paintLabels();});
        box.querySelectorAll('[data-ccg-addlabel]').forEach(b=>b.onclick=()=>{labels=normalizeLabels([...labels,b.dataset.ccgAddlabel]);paintLabels();});
        const inp=box.querySelector('#ccg-ni-label');
        if(inp)inp.onkeydown=e=>{if(e.key==='Enter'||e.key===','){e.preventDefault();if(inp.value.trim()){labels=normalizeLabels([...labels,inp.value]);paintLabels();box.querySelector('#ccg-ni-label')&&box.querySelector('#ccg-ni-label').focus();}}};
      };
      paintLabels();if(sel)sel.onchange=paintLabels;
      if(p.attachUserAutocomplete)try{p.attachUserAutocomplete(body);}catch(_){}
      rootEl.querySelector('#ccg-ni-cancel').onclick=()=>p.closeModal();
      rootEl.querySelector('#ccg-ni-attach').onclick=()=>rootEl.querySelector('#ccg-ni-file').click();
      rootEl.querySelector('#ccg-ni-file').onchange=async e=>{await attachFiles(e.target.files,body,note);e.target.value='';};
      setTimeout(()=>title.focus(),30);
      const send=rootEl.querySelector('#ccg-ni-send');
      send.onclick=async()=>{
        const subject=title.value.trim();if(!subject){title.focus();return;}
        const draft=box.querySelector('#ccg-ni-label');if(draft&&draft.value.trim())labels=normalizeLabels([...labels,draft.value]);   // an uncommitted label still counts (Armada)
        send.disabled=true;const c=coord(),r0=repos.find(r=>r.address.coordinate===c),s=stateOf(c);
        const repo={address:r0.address,coordinate:c,owner:s.ann?s.ann.owner:r0.address.owner,maintainers:s.ann?s.ann.maintainers:[]};
        const text=body.value.trim();
        const r=await publishTo(repo,issueTemplate(repo,subject,text,relayHint(repo),await mediaTags(text),labels));
        if(r.ok||r.queued){p.closeModal();p.toast(r.ok?'Issue opened':'Issue not confirmed yet — it will be re-sent');if(host&&host.repaint)host.repaint();if(projectsOpen)renderProjects();}
        else{send.disabled=false;note.textContent='The issue was not opened: '+r.msg;}
      };
    });
  }
  function repoList(repos){return repos.map(r=>{const s=stateOf(r.address.coordinate);return s.ann||{address:r.address,owner:r.address.owner,maintainers:[],name:r.address.identifier,identifier:r.address.identifier,relays:[],cloneUrls:[],webUrls:[],createdAt:0};});}

  /* ───────────────────────────── Projects ───────────────────────────── */
  let projectsOpen=false;
  const pv={tab:'overview',repo:'',state:'open',label:'',q:'',sort:'recent',layout:'grid',rq:'',rsort:'recent'};
  /* `opts.channel` scopes the view to that channel's repository (the header's Issues button). */
  function openProjects(room,opts={}){
    projectsOpen=true;panel.room=room;
    if(opts.tab)pv.tab=opts.tab;
    if(opts.channel){const a=attachmentsOf(opts.channel).find(isActive);pv.repo=a?a.address.coordinate:'';}
    watch(room,watched.channel,true);
    void refreshWatched(true);
    renderProjects();
  }
  function closeProjects(){projectsOpen=false;const el=document.getElementById('ccg-projects');if(el)el.remove();document.body.classList.remove('ccg-projects-open');watched.projects=false;}
  function renderProjects(){
    if(!projectsOpen)return;
    const room=panel.room,repos=activeRepos(room),coords=repos.map(r=>r.address.coordinate),anns=repoList(repos);
    let el=document.getElementById('ccg-projects');const scroll=el&&el.querySelector('.ccg-proj-body')?el.querySelector('.ccg-proj-body').scrollTop:0;
    const focused=el&&document.activeElement&&el.contains(document.activeElement)?{id:document.activeElement.id,pos:document.activeElement.selectionStart}:null;
    if(!el){el=document.createElement('section');el.id='ccg-projects';el.className='ccg-projects';el.setAttribute('role','dialog');el.setAttribute('aria-label','Projects');document.body.appendChild(el);document.body.classList.add('ccg-projects-open');}
    const all=projectItems(eventsOf(coords),anns),scoped=pv.repo?all.filter(x=>x.repo.address.coordinate===pv.repo):all;
    const loading=coords.some(c=>stateOf(c).loading)&&!all.length;
    const people=unique(anns.flatMap(a=>[a.owner,...(a.maintainers||[])]).concat(all.map(x=>x.ticket.author)));want(people.slice(0,40));
    const tabs=[['overview','Overview'],['repos','Repositories'],['prs','Pull Requests'],['issues','Issues']];
    const chip=pv.repo?`<span class="ccg-chip">${enc(repoName(pv.repo))} <button type="button" data-ccg-unscope aria-label="Show all repositories">×</button></span>`:'';
    el.innerHTML=`<header class="ccg-proj-head"><div><h2>Projects</h2><p class="ccg-muted">Git repositories connected to this community's channels.</p></div><div class="ccg-proj-tools"><button type="button" class="btn btn-ghost small" data-ccg-refresh>Refresh</button>${me()?'<button type="button" class="btn btn-neon small" data-ccg-new-issue>New issue</button>':''}<button type="button" class="ccg-x" data-ccg-proj-close aria-label="Close Projects">×</button></div></header>
      <nav class="ccg-tabs" role="tablist">${tabs.map(([k,l])=>`<button type="button" role="tab" aria-selected="${pv.tab===k}" class="${pv.tab===k?'on':''}" data-ccg-tab="${k}">${l}</button>`).join('')}${chip}</nav>
      <div class="ccg-proj-body">${loading?'<p class="ccg-muted">Loading repository activity…</p>':pv.tab==='overview'?overviewHtml(anns,scoped,people):pv.tab==='repos'?reposHtml(repos,anns,all):itemsHtml(scoped,pv.tab==='prs'?'pull-request':'issue')}</div>`;
    const body=el.querySelector('.ccg-proj-body');if(body)body.scrollTop=scroll;
    if(focused&&focused.id){const f=el.querySelector('#'+focused.id);if(f){f.focus();try{f.setSelectionRange(focused.pos,focused.pos);}catch(_){}}}
    bindProjects(el,room);
  }
  function overviewHtml(anns,items,people){
    const prs=items.filter(x=>x.ticket.type==='pull-request').length,iss=items.length-prs;
    const weeks=26,now=new Date();now.setHours(0,0,0,0);const start=now.getTime()-((weeks*7-1)-((6-now.getDay())))*86400000;const counts=new Map();
    for(const a of anns)if(a.createdAt){const d=new Date(a.createdAt*1000);d.setHours(0,0,0,0);counts.set(d.getTime(),(counts.get(d.getTime())||0)+1);}
    for(const x of items){const d=new Date(x.ticket.createdAt*1000);d.setHours(0,0,0,0);counts.set(d.getTime(),(counts.get(d.getTime())||0)+1);}
    const max=Math.max(1,...counts.values());let cells='';
    for(let i=0;i<weeks*7;i++){const t=start+i*86400000,n=counts.get(new Date(t).setHours(0,0,0,0))||0,lv=n?Math.min(4,Math.ceil(n/max*4)):0;cells+=`<i class="ccg-g${lv}" title="${enc(new Date(t).toLocaleDateString()+': '+n)}"></i>`;}
    return `<div class="ccg-stats"><button type="button" data-ccg-tab="repos"><b>${anns.length}</b><span>Repositories</span></button><button type="button" data-ccg-tab="prs"><b>${prs}</b><span>Pull requests</span></button><button type="button" data-ccg-tab="issues"><b>${iss}</b><span>Issues</span></button><div><b>${people.length}</b><span>People</span></div></div>
      <h4>Recent activity</h4>${items.length?`<div class="ccg-items">${items.slice(0,20).map(itemRow).join('')}</div>`:'<p class="ccg-muted">No issues or pull requests yet.</p>'}
      <h4>People</h4><div class="ccg-people">${people.slice(0,18).map(pk=>`<button type="button" data-ccg-profile="${enc(pk)}" title="${enc(nameOf(pk))}"><img src="${enc(avatarOf(pk))}" alt="${enc(nameOf(pk))}"></button>`).join('')}</div>
      <h4>Contributions <small class="ccg-muted">· last 26 weeks</small></h4><div class="ccg-graph" aria-label="Contribution graph">${cells}</div>`;
  }
  function reposHtml(repos,anns,items){
    const q=pv.rq.toLowerCase();
    let list=repos.map((r,i)=>({r,a:anns[i]})).filter(({r,a})=>!q||[a.name,a.identifier,a.description||'',r.channels.join(' ')].join(' ').toLowerCase().includes(q));
    list=pv.rsort==='name'?list.sort((x,y)=>x.a.name.localeCompare(y.a.name)):list.sort((x,y)=>(y.a.createdAt||0)-(x.a.createdAt||0));
    return `<div class="ccg-filters"><input class="input" id="ccg-rq" placeholder="Search repositories" value="${enc(pv.rq)}"><select class="input" id="ccg-rsort"><option value="recent"${pv.rsort==='recent'?' selected':''}>Recent</option><option value="name"${pv.rsort==='name'?' selected':''}>Name</option></select><button type="button" class="btn btn-ghost small" data-ccg-layout>${pv.layout==='grid'?'List':'Grid'}</button></div>
      <div class="ccg-repos ${pv.layout==='grid'?'grid':'list'}">${list.map(({r,a})=>{const its=items.filter(x=>x.repo.address.coordinate===r.address.coordinate),prs=its.filter(x=>x.ticket.type==='pull-request').length,iss=its.length-prs,ppl=unique([a.owner,...(a.maintainers||[]),...its.map(x=>x.ticket.author)]).slice(0,6);
        return `<article class="ccg-repo-card" data-ccg-scope="${enc(r.address.coordinate)}" tabindex="0"><h4>${enc(a.name)}</h4><small class="ccg-muted">${r.channels.map(c=>'#'+enc(c)).join(' · ')}</small><p>${enc(a.description||'A shared space for git work.')}</p><div class="ccg-repo-meta"><span class="ccg-muted">${a.createdAt?enc(ago(a.createdAt)):''}</span>${(a.webUrls||[])[0]?`<a href="${enc(a.webUrls[0])}" target="_blank" rel="noopener noreferrer" data-ccg-stop>View on web</a>`:''}${(a.cloneUrls||[])[0]?`<button type="button" class="ccg-linkbtn" data-ccg-clone="${enc(a.cloneUrls[0])}" title="${enc(a.cloneUrls[0])}">Copy clone URL</button>`:''}</div><div class="ccg-repo-foot"><span class="ccg-stack">${ppl.map(pk=>`<img src="${enc(avatarOf(pk))}" alt="" title="${enc(nameOf(pk))}">`).join('')}</span><span class="ccg-muted">${prs} PRs · ${iss} issues</span></div>${its.length?`<div class="ccg-bar"><i style="width:${Math.round(prs/its.length*100)}%"></i></div>`:''}</article>`;}).join('')||'<p class="ccg-muted">No repository matches.</p>'}</div>`;
  }
  function itemsHtml(items,type){
    const typed=items.filter(x=>x.ticket.type===type),open=typed.filter(x=>x.status==='open'||x.status==='draft').length;
    const shown=filterItems(items,{type,state:pv.state,label:pv.label,query:pv.q,sort:pv.sort});
    return `<div class="ccg-filters"><div class="ccg-seg" role="group">${[['open','Open '+open],['closed','Closed '+(typed.length-open)],['all','All']].map(([k,l])=>`<button type="button" class="${pv.state===k?'on':''}" data-ccg-state="${k}">${l}</button>`).join('')}</div><input class="input" id="ccg-q" placeholder="Search  ( / )" value="${enc(pv.q)}"><select class="input" id="ccg-sort"><option value="recent"${pv.sort==='recent'?' selected':''}>Recent</option><option value="name"${pv.sort==='name'?' selected':''}>Name</option></select>${pv.label?`<span class="ccg-chip">label: ${enc(pv.label)} <button type="button" data-ccg-unlabelf aria-label="Clear label filter">×</button></span>`:''}</div>
      ${shown.length?`<div class="ccg-items">${shown.map(itemRow).join('')}</div>`:`<p class="ccg-muted">${typed.length?'Nothing matches.':type==='issue'?'No issues yet.':'No pull requests yet.'}</p>`}`;
  }
  function itemRow(x){
    const t=x.ticket;
    return `<div class="ccg-item" data-ccg-ticket="${enc(t.id)}" tabindex="0" role="button"><span class="ccg-ico" aria-hidden="true">${t.type==='issue'?ICON.issue:ICON['pull-request']}</span><div class="ccg-item-main"><b>${enc(t.subject)}</b><small class="ccg-muted">${enc(x.repo.name)} · <span title="opened ${enc(new Date(t.createdAt*1000).toLocaleString())}">updated ${enc(ago(x.updatedAt))}</span> · ${enc(nameOf(t.author))}</small>${t.labels.length?`<span class="ccg-item-labels">${t.labels.slice(0,3).map(l=>`<button type="button" class="ccg-label" data-ccg-labelf="${enc(l)}">${enc(l)}</button>`).join('')}${t.labels.length>3?`<span class="ccg-muted">+${t.labels.length-3}</span>`:''}</span>`:''}</div><span class="ccg-item-side">${x.commentCount?`<span class="ccg-muted">💬 ${x.commentCount}</span>`:''}${statusChip(x.status)}</span></div>`;
  }
  function bindProjects(el,room){
    const p=P();
    el.querySelector('[data-ccg-proj-close]').onclick=closeProjects;
    el.querySelector('[data-ccg-refresh]').onclick=()=>{for(const r of activeRepos(room)){const s=stateOf(r.address.coordinate);s.loadedAt=0;s.first=true;}void refreshWatched(true);renderProjects();};
    const ni=el.querySelector('[data-ccg-new-issue]');if(ni)ni.onclick=()=>newIssue(room,pv.repo);
    el.querySelectorAll('[data-ccg-tab]').forEach(b=>b.onclick=()=>{pv.tab=b.dataset.ccgTab;renderProjects();});
    const un=el.querySelector('[data-ccg-unscope]');if(un)un.onclick=()=>{pv.repo='';renderProjects();};
    el.querySelectorAll('[data-ccg-scope]').forEach(c=>{const go=e=>{if(e.target.closest('a,button'))return;pv.repo=c.dataset.ccgScope;pv.tab='overview';renderProjects();};c.onclick=go;c.onkeydown=e=>{if(e.key==='Enter')go(e);};});
    el.querySelectorAll('[data-ccg-clone]').forEach(b=>b.onclick=e=>{e.stopPropagation();if(p.copyValue)p.copyValue(b.dataset.ccgClone);p.toast('clone URL copied');});
    el.querySelectorAll('[data-ccg-state]').forEach(b=>b.onclick=()=>{pv.state=b.dataset.ccgState;renderProjects();});
    el.querySelectorAll('[data-ccg-labelf]').forEach(b=>b.onclick=e=>{e.stopPropagation();pv.label=b.dataset.ccgLabelf;renderProjects();});
    const ul=el.querySelector('[data-ccg-unlabelf]');if(ul)ul.onclick=()=>{pv.label='';renderProjects();};
    const lay=el.querySelector('[data-ccg-layout]');if(lay)lay.onclick=()=>{pv.layout=pv.layout==='grid'?'list':'grid';renderProjects();};
    const bindInput=(id,key)=>{const f=el.querySelector('#'+id);if(f)f.oninput=()=>{pv[key]=f.value;clearTimeout(bindInput.t);bindInput.t=setTimeout(renderProjects,120);};};
    bindInput('ccg-q','q');bindInput('ccg-rq','rq');
    const s1=el.querySelector('#ccg-sort');if(s1)s1.onchange=()=>{pv.sort=s1.value;renderProjects();};
    const s2=el.querySelector('#ccg-rsort');if(s2)s2.onchange=()=>{pv.rsort=s2.value;renderProjects();};
    el.querySelectorAll('.ccg-item[data-ccg-ticket]').forEach(r=>{const go=e=>{if(e.target.closest('[data-ccg-labelf]'))return;openTicket(r.dataset.ccgTicket,room);};r.onclick=go;r.onkeydown=e=>{if(e.key==='Enter')go(e);};});
    el.querySelectorAll('[data-ccg-profile]').forEach(b=>b.onclick=()=>{try{p.openProfile(b.dataset.ccgProfile);}catch(_){}});
    el.onkeydown=e=>{if(e.key==='/'&&!/input|textarea|select/i.test((e.target.tagName||''))){const f=el.querySelector('#ccg-q,#ccg-rq');if(f){e.preventDefault();f.focus();}}if(e.key==='Escape'&&!panel.ticket)closeProjects();};
  }

  /* ───────────────────────────── settings: Integrations ───────────────────────────── */
  /* Connected repositories for the community settings dialog. `manage` = MANAGE_CHANNELS. */
  function settingsHtml(room,manage){
    const atts=roomAttachments(room).filter(isActive);
    if(!atts.length&&!manage)return '';
    for(const a of atts)relaysFor(a.address.coordinate,a.relayHints);
    return `<section class="ccg-settings"><h4>Connected repositories</h4><p class="muted small">Issues, comments and status changes from a connected repository appear in its channel. Anyone can file an issue from Concord, Armada or any NIP-34 client.</p>
      ${atts.length?`<div class="ccg-settings-list">${atts.map(a=>{want([a.address.owner]);return `<div class="ccg-settings-row"><img src="${enc(avatarOf(a.address.owner))}" alt=""><span><b>${enc(nameOf(a.address.owner))} / ${enc(repoName(a.address.coordinate))}</b><small>#${enc(a.channel.name)}</small></span>${manage?`<button type="button" class="btn btn-ghost small danger" data-ccg-detach="${enc(a.address.coordinate)}" data-ccg-channel="${enc(a.channel.id)}" aria-label="Disconnect ${enc(repoName(a.address.coordinate))}">Disconnect</button>`:''}</div>`;}).join('')}</div>`:'<p class="ccg-muted small">No repository is connected.</p>'}
      ${manage?'<button type="button" class="btn btn-ghost small" data-ccg-connect>+ Connect a repository</button>':''}</section>`;
  }
  /* `edit(channelId, changes)` is concord.js's channel-edition publisher. */
  function bindSettings(rootEl,room,edit){
    const p=P();
    rootEl.querySelectorAll('[data-ccg-detach]').forEach(b=>b.onclick=async()=>{
      const ch=(room.channels||[]).find(c=>c.id===b.dataset.ccgChannel),name=repoName(b.dataset.ccgDetach);
      const ok=p.uiConfirm?await p.uiConfirm(`Disconnect ${name} from #${ch?ch.name:'channel'}? Historical activity remains attached to its original interval.`,{ok:'Disconnect',danger:true}):true;
      if(!ok)return;b.disabled=true;
      try{await edit(b.dataset.ccgChannel,{detachRepository:b.dataset.ccgDetach});p.toast(name+' disconnected');}catch(e){b.disabled=false;p.toast('the repository was not disconnected: '+((e&&e.message)||e));}
    });
    const c=rootEl.querySelector('[data-ccg-connect]');
    if(c)c.onclick=()=>connectFlow(room,edit);
  }
  /* Pick a repository, then a channel (one repository per channel — Armada's UI rule). */
  async function connectFlow(room,edit,channelId){
    const p=P(),connected=new Set(roomAttachments(room).filter(isActive).map(a=>a.address.coordinate));
    const picked=await pickRepository({connected});if(!picked)return;
    let target=channelId;
    if(!target){
      const free=(room.channels||[]).filter(ch=>ch.id&&!attachmentsOf(ch).some(isActive));
      target=await new Promise(resolve=>{
        p.modal(`<h3>Choose a channel</h3><p class="muted small">Activity from <b>${enc(picked.announcement?picked.announcement.name:picked.address.identifier)}</b> will appear in this channel.</p><div class="ccg-pick-list">${(room.channels||[]).filter(ch=>ch.id).map(ch=>{const has=attachmentsOf(ch).find(isActive);return `<button type="button" class="ccg-pick-row" data-ccg-ch="${enc(ch.id)}"${has?' disabled':''}><span><b>#${enc(ch.name)}</b>${has?`<small>Already connected to ${enc(repoName(has.address.coordinate))}</small>`:''}</span></button>`;}).join('')}</div>${free.length?'':'<p class="muted small">Every channel already has a repository. Create a new channel for this one.</p>'}<div class="set-actions"><button class="btn btn-ghost small" id="ccg-ch-cancel">Cancel</button></div>`,rootEl=>{
          rootEl.querySelector('#ccg-ch-cancel').onclick=()=>{p.closeModal();resolve(null);};
          rootEl.querySelectorAll('[data-ccg-ch]').forEach(b=>b.onclick=()=>{p.closeModal();resolve(b.dataset.ccgCh);});
        });
      });
    }
    if(!target)return;
    try{await edit(target,{attachRepository:{address:picked.address.coordinate,relayHints:picked.relayHints}});p.toast('Repository connected');
      void refreshRepo(picked.address.coordinate,picked.relayHints).then(n=>{if(n&&host&&host.repaint)host.repaint();});}
    catch(e){p.toast('the repository was not connected: '+((e&&e.message)||e));}
  }

  /* ───────────────────────────── wiring ───────────────────────────── */
  document.addEventListener('click',e=>{
    const io=e.target&&e.target.closest&&e.target.closest('[data-ccg-intro-open]');
    if(io){e.preventDefault();const room=host&&host.room&&host.room();if(!room)return;
      const ch=(room.channels||[]).find(c=>attachmentsOf(c).some(a=>isActive(a)&&a.address.coordinate===io.dataset.ccgIntroOpen));
      openProjects(room,{channel:ch,tab:'issues'});return;}
    const t=e.target&&e.target.closest&&e.target.closest('.ccg-row [data-ccg-ticket]');
    if(t){e.preventDefault();openTicket(t.dataset.ccgTicket,host&&host.room&&host.room());return;}
    const pr=e.target&&e.target.closest&&e.target.closest('.ccg-row [data-ccg-profile]');
    if(pr){e.preventDefault();try{P().openProfile(pr.dataset.ccgProfile);}catch(_){}}
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&panel.ticket&&!document.querySelector('.modal:not(.hidden),#modal:not(.hidden)'))closePanel();});

  root.PCConcordGit={core,
    /* concord.js hands in what only it knows. */
    bindHost(h){host=h;},
    paneRows,channelHasGit,channelIntroHtml,hasRepos,activeRepos,latestAt,watch,openTicket,closePanel,openProjects,closeProjects,
    newIssue,pickRepository,connectFlow,settingsHtml,bindSettings,attachmentsOf,
    /* For tests and the harness. */
    _state:repoState,_absorb:(coord,evs)=>absorb(coord,stateOf(coord),evs),_md:md,_rowHtml:rowHtml};
})(typeof window!=='undefined'?window:globalThis);
