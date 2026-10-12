/* Concord channel SPACES: forum channels, voice presence on the channel list, and the banner.
 *
 * Three things a member reported missing ("no banners", "no forum channels", "no voice channels"),
 * each of which the protocol already defines -- nothing here invents a kind or a field:
 *
 *  - BANNER: CORD-02 community metadata `banner`, an encrypted ImagePointer {url,key,nonce,hash}
 *    exactly like `icon` (AES-256-GCM, 16-byte nonce; Armada src/concord/lib/image.ts).
 *  - FORUM: CORD-03 §2 channel metadata `view: "forum"` (absent = "chat"; Armada channelView.ts).
 *    A post is a kind-9 message with a NIP-14 `["subject", <title>]` tag, capped at 256 UTF-8 bytes;
 *    its comments are ordinary kind-1111 thread replies (Armada forum.ts). A client without forum
 *    support reads the same events as messages with threads.
 *  - VOICE: CORD-07 "Every Channel is callable -- there is no separate 'voice Channel' type". A
 *    channel with a live call shows a speaker and the people in it (Armada ConcordPage.tsx
 *    `occupied`, ChannelGlyph.tsx); a forum channel offers no call. Presence is the signed ephemeral
 *    kind-23313 rumor folded by PCCordVoice.fold (cord-voice.js).
 *
 * Pure helpers and markup only. The transport (relay reads, presence subscriptions, publishing)
 * stays in concord.js, which owns the membership, plane auth and stores. Kept separate so those
 * features do not grow concord.js's render path and so this half can be read on its own. */
(function(root){
  'use strict';
  const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const utf8=new TextEncoder(),utf8d=new TextDecoder();
  const SUBJECT_TAG='subject',SUBJECT_MAX_BYTES=256,NAME_MAX_BYTES=64;
  const bytes=s=>utf8.encode(String(s||'')).length;
  function truncateUtf8(s,max){
    const b=utf8.encode(String(s||''));if(b.length<=max)return String(s||'');
    let end=max;while(end>0&&(b[end]&0xc0)===0x80)end--;return utf8d.decode(b.subarray(0,end));
  }
  const clean=s=>String(s==null?'':s).replace(/\s+/g,' ').trim();

  /* ---- forum (CORD-03 §2 `view`, NIP-14 subject) ---- */
  function channelView(channel){ return channel&&channel.view==='forum'?'forum':'chat'; }
  /* The post's title, or '' when the message is not a titled post. Kind 9 only: a 1111 is a comment.
   * Blank reads as none; an over-long title is truncated, never dropped (Armada subjectOf). */
  function subjectOf(m){
    if(!m||Number(m.kind||9)!==9)return '';
    const raw=((m.tags||[]).find(t=>Array.isArray(t)&&t[0]===SUBJECT_TAG)||[])[1];
    if(typeof raw!=='string')return '';
    const title=clean(raw);if(!title)return '';
    return bytes(title)>SUBJECT_MAX_BYTES?truncateUtf8(title,SUBJECT_MAX_BYTES).trimEnd():title;
  }
  /* Throws rather than publishing a clipped title (the dialog enforces the same limit). */
  function subjectTags(title){
    const t=clean(title);
    if(!t)throw new Error('A post needs a title.');
    if(bytes(t)>SUBJECT_MAX_BYTES)throw new Error('Titles are limited to '+SUBJECT_MAX_BYTES+' bytes.');
    return [[SUBJECT_TAG,t]];
  }
  const idOf=m=>String(m&&(m.id||m.rumorId)||'');
  /* The feed: every titled post among `rows` (the channel's messages, any order), newest activity
   * first; `repliesFor(id)` answers the post's thread. Times are this client's milliseconds. */
  function forumPosts(rows,repliesFor){
    const out=[];
    for(const root of rows||[]){
      const title=subjectOf(root);if(!title)continue;
      const replies=(repliesFor&&repliesFor(idOf(root)))||[];
      let newest=root;for(const r of replies)if(Number(r.at)>Number(newest.at))newest=r;
      const people=[];for(const r of [...replies].sort((a,b)=>Number(b.at)-Number(a.at)))if(r.pubkey&&!people.includes(r.pubkey))people.push(r.pubkey);
      out.push({root,id:idOf(root),title,replyCount:replies.length,lastActivityAt:Number(newest.at)||0,lastActivityBy:newest.pubkey||'',participants:people});
    }
    out.sort((a,b)=>(b.lastActivityAt-a.lastActivityAt)||(a.id<b.id?-1:a.id>b.id?1:0));
    return out;
  }
  function ago(ms,now=Date.now()){
    const s=Math.max(0,Math.round((now-ms)/1000));
    if(s<60)return 'just now';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';
    if(s<86400*30)return Math.floor(s/86400)+'d ago';return new Date(ms).toLocaleDateString();
  }
  /* `p` is the client surface (enc, profOf, LOGO). The card IS the open-thread control: concord.js
   * already binds every [data-cc-thread] to "open this thread", and a forum post is a thread root. */
  function feedHtml(p,posts,opts={}){
    const name=n=>{const pr=p&&p.profOf?p.profOf(n)||{}:{};return pr.display_name||pr.name||String(n||'').slice(0,10)||'member';};
    const head=`<div class="cc-forum-head"><div><b>${enc(opts.channel?'#'+opts.channel:'Forum')}</b><small>${posts.length} ${posts.length===1?'post':'posts'}</small></div>`
      +(opts.canPost?`<button type="button" class="btn btn-neon cc-forum-new" id="cc-forum-new"><svg class="ic" aria-hidden="true"><use href="#i-pen"></use></svg>New post</button>`:'')+'</div>';
    if(!posts.length)return `<section class="cc-forum" aria-label="Forum posts">${head}<div class="cc-forum-empty"><svg class="ic" aria-hidden="true"><use href="#i-chat"></use></svg><b>No posts yet</b><span>Start a discussion with a title, and replies stay together under it.</span></div></section>`;
    return `<section class="cc-forum" aria-label="Forum posts">${head}<div class="cc-forum-list">${posts.map(x=>{
      const first=clean(x.root.text||x.root.content||'').slice(0,220);
      return `<button type="button" class="cc-forum-post" data-cc-thread="${enc(x.id)}"><span class="cc-forum-title">${enc(x.title)}</span>`
        +(first?`<span class="cc-forum-excerpt">${enc(first)}</span>`:'')
        +`<span class="cc-forum-meta"><span class="cc-forum-by">${enc(name(x.root.pubkey)||x.root.by||'')}</span><span class="cc-forum-replies">${x.replyCount} ${x.replyCount===1?'reply':'replies'}</span><span class="cc-forum-when" title="${enc(new Date(x.lastActivityAt).toLocaleString())}">${enc(ago(x.lastActivityAt))}</span></span></button>`;
    }).join('')}</div></section>`;
  }
  /* The title above a titled post's body, wherever it is drawn (the forum thread, or a chat channel
   * that received a post from a forum-aware client). */
  function subjectHtml(m){ const t=subjectOf(m); return t?`<div class="cc-forum-subject" role="heading" aria-level="3">${enc(t)}</div>`:''; }

  /* ---- channel glyph + voice presence (CORD-07) ---- */
  function glyphHtml(view,occupied){
    const v=view==='forum'?'forum':'chat';
    const icon=occupied?'<svg class="ic"><use href="#i-volume"></use></svg>':v==='forum'?'<svg class="ic"><use href="#i-chat"></use></svg>':'#';
    return `<span class="cc-channel-kind${occupied?' cc-voice-live':''}" data-view="${v}" data-occupied="${occupied?'true':'false'}" aria-hidden="true">${icon}</span>`;
  }
  function voiceJoinHtml(channel,occupied,inCall){
    if(!channel||!channel.id||channelView(channel)==='forum')return '';
    if(inCall)return `<span class="cc-voice-here" title="You are in voice here" aria-label="You are in voice here"><svg class="ic"><use href="#i-headphones"></use></svg></span>`;
    const label=(occupied?'Join voice in #':'Start voice in #')+channel.name;
    return `<button type="button" class="cc-voice-join${occupied?' live':''}" data-cc-voice-join="${enc(channel.id)}" data-cc-voice-channel="${enc(channel.name)}" title="${enc(label)}" aria-label="${enc(label)}"><svg class="ic"><use href="#i-phone"></use></svg></button>`;
  }
  function voicePeopleHtml(p,channelId,people){
    if(!people||!people.length)return '';
    return `<div class="cc-voice-people" data-cc-voice-for="${enc(channelId)}" aria-label="In voice">${people.map(x=>{
      const pr=p&&p.profOf?p.profOf(x.pubkey)||{}:{},nm=pr.display_name||pr.name||String(x.pubkey).slice(0,10);
      return `<span class="cc-voice-person" data-pk="${enc(x.pubkey)}"${x.verified?'':' data-unverified="true"'}><img src="${enc(pr.picture||(p&&p.LOGO)||'')}" alt=""><span>${enc(nm)}</span></span>`;
    }).join('')}</div>`;
  }

  /* ---- dialogs (no native prompt/confirm: they wedge the Electron renderer) ----
   * The same overlay classes app.js's uiPrompt uses, so they inherit its z-index above the desktop
   * shell, its look, and the `.uiconfirm-msg` / `[data-uc]` hooks existing tests already drive. */
  function overlay(inner,{onOpen,collect}){
    return new Promise(resolve=>{
      const ov=document.createElement('div');ov.className='uiconfirm-bg pc-cord-spaces-dialog';
      ov.innerHTML=`<div class="uiconfirm glass neon-border" role="dialog" aria-modal="true">${inner}</div>`;
      const done=v=>{try{document.removeEventListener('keydown',onKey,true);}catch(_){}ov.remove();resolve(v);};
      const submit=()=>{const v=collect(ov);if(v&&v.error){const e=ov.querySelector('.cc-dialog-error');if(e)e.textContent=v.error;return;}done(v);};
      const onKey=e=>{if(e.key==='Escape'){e.preventDefault();done(null);}else if(e.key==='Enter'&&!(e.target&&e.target.tagName==='TEXTAREA')){e.preventDefault();submit();}};
      ov.addEventListener('click',e=>{if(e.target===ov){done(null);return;}const b=e.target.closest('[data-uc]');if(b){if(b.dataset.uc==='1')submit();else done(null);}});
      document.addEventListener('keydown',onKey,true);
      document.body.appendChild(ov);
      if(onOpen)onOpen(ov);
    });
  }
  const buttons=(ok)=>`<p class="cc-dialog-error" role="alert"></p><div class="uiconfirm-btns"><button class="btn btn-ghost small" data-uc="0">Cancel</button><button class="btn btn-neon small" data-uc="1">${enc(ok)}</button></div>`;
  /* The channel kinds CORD-03 defines. There is no voice TYPE to offer: CORD-07 makes every chat
   * channel callable, which the channel list shows. Resolves {name, view} or null. */
  function askNewChannel(opts={}){
    return overlay(`<div class="uiconfirm-msg">Name the new channel</div>
      <input class="input uiprompt-in" maxlength="64" autocomplete="off" placeholder="channel-name" value="${enc(opts.value||'')}" style="width:100%;margin-bottom:10px">
      <div class="cc-view-choice" role="radiogroup" aria-label="Channel type">
        <button type="button" role="radio" aria-checked="true" data-cc-view="chat"><b>#</b><span><strong>Text</strong><small>Messages, threads and calls</small></span></button>
        <button type="button" role="radio" aria-checked="false" data-cc-view="forum"><svg class="ic" aria-hidden="true"><use href="#i-chat"></use></svg><span><strong>Forum</strong><small>Titled posts with replies</small></span></button>
      </div>${buttons(opts.ok||'Create channel')}`,{
      onOpen(ov){
        const radios=[...ov.querySelectorAll('[data-cc-view]')];
        radios.forEach(r=>r.onclick=()=>radios.forEach(x=>x.setAttribute('aria-checked',x===r?'true':'false')));
        const inp=ov.querySelector('.uiprompt-in');setTimeout(()=>{try{inp.focus();}catch(_){}},20);
      },
      collect(ov){
        const name=clean(ov.querySelector('.uiprompt-in').value).replace(/^#/,'');
        const view=(ov.querySelector('[data-cc-view][aria-checked="true"]')||{}).dataset?.ccView==='forum'?'forum':'chat';
        if(!name)return {error:'Name the channel.'};
        if(bytes(name)>NAME_MAX_BYTES)return {error:'Channel names are limited to 64 bytes.'};
        return {name,view};
      }
    });
  }
  /* A forum post: a title and a first message. Resolves {title, body} or null. */
  function askNewPost(opts={}){
    return overlay(`<div class="uiconfirm-msg">New post</div>
      <label class="cc-label" for="cc-post-title">Title</label>
      <input class="input" id="cc-post-title" maxlength="300" autocomplete="off" placeholder="What is this about?" style="width:100%;margin-bottom:10px">
      <label class="cc-label" for="cc-post-body">Post</label>
      <textarea class="input" id="cc-post-body" rows="5" placeholder="Write the first post" style="width:100%;resize:vertical"></textarea>${buttons(opts.ok||'Post')}`,{
      onOpen(ov){setTimeout(()=>{try{ov.querySelector('#cc-post-title').focus();}catch(_){}},20);},
      collect(ov){
        const title=clean(ov.querySelector('#cc-post-title').value),body=String(ov.querySelector('#cc-post-body').value||'').trim();
        if(!title)return {error:'A post needs a title.'};
        if(bytes(title)>SUBJECT_MAX_BYTES)return {error:'Titles are limited to '+SUBJECT_MAX_BYTES+' bytes.'};
        if(!body)return {error:'Write the first post.'};
        return {title,body};
      }
    });
  }

  /* ---- banner (CORD-02 `banner`) ---- */
  function bannerSettingsHtml(url,canEdit){
    return `<label class="cc-label">Banner</label><div class="cc-banner-setting">`
      +`<div class="cc-banner-preview${url?'':' empty'}" id="cc-banner-preview">${url?`<img src="${enc(url)}" alt="Current banner">`:'<span>No banner</span>'}</div>`
      +(canEdit?`<div class="cc-banner-actions"><button type="button" class="btn btn-ghost" id="cc-banner-pick">${url?'Change banner…':'Upload banner…'}</button>`
        +(url?'<button type="button" class="btn btn-ghost danger" id="cc-banner-remove">Remove</button>':'')
        +`<input type="file" id="cc-banner-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden></div>`:'')
      +`<p class="cc-member-help" id="cc-banner-status" role="status" aria-live="polite">Shown above the channel list for every member. Encrypted like the icon.</p></div>`;
  }
  /* Encrypt image bytes for a CORD-02 ImagePointer: a fresh AES-256-GCM key and 16-byte nonce per
   * image, SHA-256 of the plaintext (Armada encryptImageBlob). Returns {cipher, key, nonce, hash}. */
  async function sealImageBytes(plain){
    const hex=b=>Array.from(new Uint8Array(b),x=>x.toString(16).padStart(2,'0')).join('');
    const key=crypto.getRandomValues(new Uint8Array(32)),nonce=crypto.getRandomValues(new Uint8Array(16));
    const k=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['encrypt']);
    const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},k,plain));
    return {cipher,key:hex(key),nonce:hex(nonce),hash:hex(await crypto.subtle.digest('SHA-256',plain))};
  }

  /* ---- styles, injected once: this module owns its own presentation ---- */
  const CSS=`
.cc-channel .cc-channel-kind{display:inline-grid!important;place-items:center!important;min-width:20px!important}
.cc-channel .cc-channel-kind .ic{width:17px!important;height:17px!important}
.cc-channel .cc-channel-kind.cc-voice-live{color:#3ba55d!important}
.cc-voice-join,.cc-voice-here{display:grid!important;place-items:center!important;flex:0 0 34px!important;width:34px!important;height:34px!important;padding:0!important;border:0!important;border-radius:7px!important;background:transparent!important;color:#777f90!important;cursor:pointer!important}
.cc-voice-join .ic,.cc-voice-here .ic{width:16px!important;height:16px!important}
.cc-voice-join:hover{background:var(--cc-hover,rgba(255,255,255,.06))!important;color:#fff!important}
.cc-voice-join.live,.cc-voice-here{color:#3ba55d!important}
.cc-channel-row:not(:hover) .cc-voice-join:not(.live){opacity:.45}
.cc-voice-people{display:flex;flex-direction:column;gap:2px;margin:0 0 6px 34px;min-width:0}
.cc-voice-person{display:flex;align-items:center;gap:7px;min-width:0;padding:3px 6px;border-radius:6px;color:#c9cdd6;font-size:13px}
.cc-voice-person img{width:20px;height:20px;border-radius:50%;object-fit:cover;flex:0 0 20px}
.cc-voice-person span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cc-voice-person[data-unverified]{opacity:.6}
.cc-conversation:has(.cc-forum) .cc-compose,.cc-conversation:has(.cc-forum) .cc-reply,.cc-conversation:has(.cc-forum) .cc-typing{display:none!important}
.cc-forum{display:flex;flex-direction:column;gap:12px;max-width:860px;margin:0 auto;padding:4px 0 18px;min-width:0}
.cc-forum-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.cc-forum-head b{display:block;font-size:17px}.cc-forum-head small{color:var(--muted,#9298a8);font-size:12px}
.cc-forum-new{display:inline-flex;align-items:center;gap:7px;min-height:40px}
.cc-forum-new .ic{width:16px;height:16px}
.cc-forum-list{display:flex;flex-direction:column;gap:8px}
.cc-forum-post{display:flex;flex-direction:column;align-items:stretch;gap:5px;width:100%;min-width:0;padding:12px 14px;border:1px solid rgba(255,255,255,.08);border-radius:10px;background:rgba(255,255,255,.035);color:inherit;font:inherit;text-align:start;cursor:pointer}
.cc-forum-post:hover,.cc-forum-post:focus-visible{background:rgba(255,255,255,.07);border-color:rgba(255,255,255,.16)}
.cc-forum-title{font-weight:700;font-size:15.5px;line-height:1.3;overflow-wrap:anywhere}
.cc-forum-excerpt{color:#b9bfcc;font-size:13.5px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.cc-forum-meta{display:flex;flex-wrap:wrap;gap:4px 12px;color:var(--muted,#9298a8);font-size:12px}
.cc-forum-replies{font-weight:700;color:#c9cdd6}
.cc-forum-empty{display:flex;flex-direction:column;align-items:center;gap:6px;padding:40px 16px;color:var(--muted,#9298a8);text-align:center}
.cc-forum-empty .ic{width:30px;height:30px}
.cc-forum-subject{font-weight:800;font-size:17px;line-height:1.3;margin:2px 0 4px;overflow-wrap:anywhere}
.cc-view-choice{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:6px}
.cc-view-choice button{display:flex;align-items:center;gap:10px;min-width:0;min-height:52px;padding:8px 10px;border:1px solid rgba(255,255,255,.14);border-radius:10px;background:transparent;color:inherit;font:inherit;text-align:start;cursor:pointer}
.cc-view-choice button[aria-checked="true"]{border-color:var(--neon,#7c5cff);background:rgba(124,92,255,.14)}
.cc-view-choice b{font-size:20px;width:20px;text-align:center}.cc-view-choice .ic{width:20px;height:20px;flex:0 0 20px}
.cc-view-choice span{display:flex;flex-direction:column;min-width:0}.cc-view-choice small{color:var(--muted,#9298a8);font-size:11.5px}
.cc-dialog-error{min-height:1em;margin:4px 0;color:#ff6b6b;font-size:12.5px}
.cc-banner-setting{display:flex;flex-direction:column;gap:8px;margin-bottom:8px}
.cc-banner-preview{height:84px;border-radius:10px;overflow:hidden;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04)}
.cc-banner-preview img{display:block;width:100%;height:100%;object-fit:cover}
.cc-banner-preview.empty{display:grid;place-items:center;color:var(--muted,#9298a8);font-size:12.5px}
.cc-banner-actions{display:flex;flex-wrap:wrap;gap:8px}
#cc-settings-dialog .cc-join-card>.cc-join-actions{position:sticky;bottom:-26px;z-index:1;margin:12px -4px -6px;padding:10px 4px 6px;background:var(--cct-card,#20242e);box-shadow:0 -10px 14px -10px rgba(0,0,0,.5)}
@media(max-width:820px){#cc-settings-dialog .cc-join-card>.cc-join-actions{bottom:-20px}}
.cc-invite-banner{height:72px;margin:-4px -4px 10px;border-radius:12px;overflow:hidden}.cc-invite-banner img{display:block;width:100%;height:100%;object-fit:cover}
@media(max-width:600px){.cc-view-choice{grid-template-columns:1fr}.cc-forum-post{padding:12px}}
`;
  function injectStyles(){
    try{ if(typeof document==='undefined'||document.getElementById('pc-cord-spaces-css'))return;
      const s=document.createElement('style');s.id='pc-cord-spaces-css';s.textContent=CSS;(document.head||document.documentElement).appendChild(s);}catch(_){}
  }
  injectStyles();

  root.PCCordSpaces=Object.freeze({SUBJECT_MAX_BYTES,channelView,subjectOf,subjectTags,forumPosts,feedHtml,subjectHtml,
    glyphHtml,voiceJoinHtml,voicePeopleHtml,askNewChannel,askNewPost,bannerSettingsHtml,sealImageBytes,injectStyles});
})(globalThis);
