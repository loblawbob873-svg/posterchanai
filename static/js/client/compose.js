/* The composer — the post/reply/quote sheet with its attachments, mentions, emoji, scheduling and
 * drafts, plus the picture-post renderer (the backgrounds, the frames and the card preview it
 * draws). Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_composeDeps`) and
 * builds this factory the first time somebody opens the composer. The code below is
 * BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let` bindings, which
 * the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets.
 *
 * Stayed in app.js: the background catalogue and the bits the TIMELINE HEADER draws with it
 * (CMP_BGS, _bgCss, _BG_WORDS, _cardHook, makeCardPreview) — the header is painted before anybody
 * has opened a composer.
 */
window.PCComposeFactory = function(dep){
  const _S = dep.state;   // live app.js bindings: _S.CFG, _S.LOGO, _S.VIEW
  const {
    $, $$, CMP_BGS, Drafts, InstEmoji, Scheduled, _BG_WORDS, _aiEmojiSuggest, _aiFramedCard,
    _appendQuoteNevent, _autoCleanOnPost, _bgCss, _blossomDenied, _captureCamera, _cardHook,
    _cleanLinksCmd, _dtLocal, _insertAt, _qDraftSet, _stickyNudge, _syncSendLabel,
    articleCommentTags, attachEmojiAutocomplete, attachMentionAutocomplete, blossomPicker,
    closeModal, composeTranslate, emojiName, enc, gifPicker, imetaTagsFor, linkify,
    makeCardPreview, mediaParts, mentionTags, modal, needProfile, openArticle, openEmojiPopover,
    openMenuPopover, openThread, profOf, publish, renderThread, renderView, replyKindFor,
    replyTags, requestBlossomAccess, timeAgo, toast, updateMentionHint, uploadBlob,
  } = dep;
  function _bgFill(ctx,W,H,bg){ if(bg.colors.length>1){ const g=ctx.createLinearGradient(0,0,W,H); bg.colors.forEach((c,i)=>g.addColorStop(i/(bg.colors.length-1),c)); ctx.fillStyle=g; } else ctx.fillStyle=bg.colors[0]; ctx.fillRect(0,0,W,H); }
  function _bgWrap(ctx, text, maxW){ const out=[]; for(const para of String(text).split('\n')){ if(!para){ out.push(''); continue; } let line=''; for(const word of para.split(/\s+/)){ const t=line?line+' '+word:word; if(ctx.measureText(t).width>maxW && line){ out.push(line); line=word; } else line=t; } if(line) out.push(line); } return out; }
  function _bgDeco(ctx, W, H, deco, framed){   // festive emoji framed along the top + bottom edges
    ctx.save(); ctx.globalAlpha=0.92; const S=Math.min(W,H), size=Math.round(S*0.072);
    ctx.font=`${size}px system-ui,-apple-system,'Segoe UI',sans-serif`; ctx.textAlign='center'; ctx.textBaseline='middle';
    // With a frame, push the emoji rows INSIDE it — at the default inset the glyphs straddle the inner
    // hairline, which reads as a mistake rather than a decoration.
    const n=6, m=framed ? Math.round(S*0.108) : size*1.05;
    // Horizontally too: spread across the FRAMED area, not the full canvas, or the first and last glyph
    // of each row sit on top of the side rules.
    const x0=framed?Math.round(S*0.108):0, xw=W-x0*2;
    for(let i=0;i<n;i++){ const x=x0 + xw*(i+0.5)/n;
      ctx.fillText(deco[i%deco.length], x, m);
      ctx.fillText(deco[(i+1)%deco.length], x, H-m); }
    ctx.restore();
  }
  // Rounded rect as an explicit path. NOT ctx.roundRect(): that is recent enough (Firefox 112, Safari 16)
  // that the APK's WebView on an older device would throw mid-render and lose the whole card.
  function _rrect(ctx,x,y,w,h,r){
    r=Math.min(r, w/2, h/2);
    ctx.beginPath();
    ctx.moveTo(x+r,y); ctx.lineTo(x+w-r,y); ctx.quadraticCurveTo(x+w,y,x+w,y+r);
    ctx.lineTo(x+w,y+h-r); ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);
    ctx.lineTo(x+r,y+h); ctx.quadraticCurveTo(x,y+h,x,y+h-r);
    ctx.lineTo(x,y+r); ctx.quadraticCurveTo(x,y,x+r,y); ctx.closePath();
  }
  // The 🖼️ framed-card treatment: a heavy rounded rule just inside the edge with a lighter hairline
  // tucked behind it. Drawn in the background's OWN foreground colour so it reads as part of the design
  // on every swatch (the light `gold` background has a dark fg — a hardcoded white frame would vanish).
  function _bgFrame(ctx,W,H,bg){
    const col=bg.fg||'#fff';
    // Proportional to the SHORT edge. Keyed on W, a 16:9 card got an inset and a rule nearly twice as
    // heavy as a square one — the same border has to read the same on every shape.
    const S=Math.min(W,H), fi=S*0.042, fw=Math.max(2,S*0.009);
    ctx.save();
    ctx.strokeStyle=col; ctx.lineJoin='round';
    ctx.globalAlpha=0.88; ctx.lineWidth=fw;
    _rrect(ctx, fi, fi, W-fi*2, H-fi*2, S*0.022); ctx.stroke();
    const gi=fi+fw*1.9;
    ctx.globalAlpha=0.45; ctx.lineWidth=Math.max(1,W*0.0032);
    _rrect(ctx, gi, gi, W-gi*2, H-gi*2, S*0.016); ctx.stroke();
    ctx.restore();
  }
  const _MONO="ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono',monospace";
  // Deterministic PRNG. The card preview re-renders on EVERY keystroke, so a Math.random() code rain
  // would reshuffle itself while you type — the field has to be stable to read as a background.
  function _seedRand(seed){ let s=(seed>>>0)||1; return ()=>{ s^=s<<13; s^=s>>>17; s^=s<<5; s>>>=0; return s/4294967296; }; }
  const _CONSOLE_TOKENS=['const','let','fn','=>','0x1f','!=','&&','||','return','void','#!/bin/sh','sudo',
    'git push','SELECT *','while(1)','::','[ok]','[warn]','0b1011','</>','~$','404','200 OK','null','async',
    'await','0xdeadbeef','if(','}else{','#define','printf(','/dev/null','chmod +x','ssh','curl -s','| grep','exit 0'];
  // Cyberpunk console: dim monospace source raining behind the words, CRT scanlines across it, and a
  // vignette that keeps the middle dark so the type stays the brightest thing on the card. Drawn right
  // after the fill, i.e. UNDER the text and under any frame.
  function _bgConsole(ctx,W,H,bg){
    const rnd=_seedRand(0x5eed), S=Math.min(W,H), fs=Math.round(S*0.022), lh=fs*1.5;
    ctx.save();
    ctx.font=`${fs}px ${_MONO}`; ctx.textAlign='left'; ctx.textBaseline='alphabetic'; ctx.fillStyle=bg.fg||'#5cff9d';
    for(let y=lh; y<H; y+=lh){
      let x=S*0.03+rnd()*S*0.06;
      ctx.globalAlpha=0.05+rnd()*0.07;      // each line a different dimness — a flat field looks printed, not lit
      while(x<W-S*0.05){
        const tok=_CONSOLE_TOKENS[(rnd()*_CONSOLE_TOKENS.length)|0];
        ctx.fillText(tok,x,y); x+=ctx.measureText(tok).width+fs*(0.8+rnd()*1.6);
      }
    }
    ctx.globalAlpha=0.14; ctx.fillStyle='#000';
    for(let y=0;y<H;y+=3) ctx.fillRect(0,y,W,1);   // scanlines
    const g=ctx.createRadialGradient(W/2,H/2,S*0.18,W/2,H/2,S*0.72);
    g.addColorStop(0,'rgba(0,0,0,0)'); g.addColorStop(1,'rgba(0,0,0,.55)');
    ctx.globalAlpha=1; ctx.fillStyle=g; ctx.fillRect(0,0,W,H);
    ctx.restore();
  }
  async function renderBgPost(text, bg, framed){
    const t=String(text||'');
    // Grow the canvas SIDEWAYS, never downwards. The feed caps an image at 300px TALL (.media-row img), so
    // a taller card is a SMALLER card: 4:5 shows at 240px wide and 2:3 at 200px, with the type scaled to
    // roughly 8px — unreadable. Holding the height at 1080 and widening keeps the display scale fixed while
    // giving each line far more room, so the card fills the column instead of shrinking away from it.
    const H = 1080;
    // SQUARE, always. Width is bound by the column and height by the 720px lone-image cap, so 1:1 is the
    // largest shape that exists here — every other ratio gives up one dimension for nothing. Rendered at
    // 1080² rather than 720² so it stays crisp on a hidpi screen at the same displayed size.
    const W = 1080;
    const LONG = t.length>320;
    const pad=Math.min(W,H)*(framed?0.115:0.095), maxW=W-pad*2,   // the frame eats into the text box
          maxH=(H-pad*2)*(bg.deco?(framed?0.78:0.82):1);   // leave room for deco rows
    const cv=document.createElement('canvas'); cv.width=W; cv.height=H; const ctx=cv.getContext('2d');
    _bgFill(ctx,W,H,bg);
    if(bg.fx==='console') _bgConsole(ctx,W,H,bg);
    if(bg.deco) _bgDeco(ctx,W,H,bg.deco,framed);
    // Before the text, so a descender can never be crossed by the rule.
    if(framed) _bgFrame(ctx,W,H,bg);
    // Centred type is right for a headline and wrong for paragraphs — a long summary set centred reads as
    // a poem. Long cards go left-aligned, which is what makes them look like a designed article card.
    ctx.fillStyle=bg.fg||'#fff'; ctx.textAlign=LONG?'left':'center'; ctx.textBaseline='middle';
    // Monospace is not a font swap, it's the whole point of the console swatch — proportional type on a
    // terminal reads as a poster of a terminal. 700 rather than 800: mono faces go muddy at 800.
    const font=s=>bg.mono ? `700 ${s}px ${_MONO}` : `800 ${s}px system-ui,-apple-system,'Segoe UI',Roboto,sans-serif`;
    // `textScale` shrinks BOTH the starting size and the floor. Scaling only the start would let the
    // fit loop walk right back down to the unscaled floor, so a long draft would come out the same size
    // as every other swatch — the setting would appear to work and then quietly not.
    const _ts=bg.textScale||1;
    const floor=Math.round((LONG?26:30)*_ts);
    let fs=Math.round((LONG?64:112)*_ts), lines=[];
    for(; fs>=floor; fs-=2){ ctx.font=font(fs); lines=_bgWrap(ctx,t,maxW); const lh=fs*(LONG?1.34:1.22);
      if(lines.length*lh<=maxH && lines.every(l=>ctx.measureText(l).width<=maxW)) break; }
    const lh=fs*(LONG?1.34:1.22), y0=H/2 - (lines.length*lh)/2 + lh/2, x=LONG?pad:W/2;
    // Every other swatch drops the type onto a light background, so it needs a shadow to separate. A
    // console is the inverse — light type on near-black — where a drop shadow does nothing and a bloom
    // in the text's OWN colour is what makes it look emitted rather than printed.
    if(bg.glow){ ctx.shadowColor=bg.fg||'#fff'; ctx.shadowBlur=fs*0.38; ctx.shadowOffsetY=0; }
    else { ctx.shadowColor='rgba(0,0,0,.25)'; ctx.shadowBlur=fs*0.12; ctx.shadowOffsetY=Math.max(1,fs*0.03); }
    lines.forEach((l,i)=>ctx.fillText(l, x, y0+i*lh));
    // A block cursor parked after the last word — the one detail that reads as a LIVE console instead of
    // green text on black. Placed off the measured last line so it lands right whether centred or left.
    if(bg.fx==='console' && lines.length){
      const lw=ctx.measureText(lines[lines.length-1]).width;
      ctx.fillRect(x+(LONG?lw:lw/2)+fs*0.28, y0+(lines.length-1)*lh-fs*0.42, fs*0.5, fs*0.84);
    }
    // JPEG, not PNG. The card is ALWAYS opaque (_bgFill paints the whole canvas), so PNG's reason to
    // exist here — preserving alpha — never applies, while a gradient-and-noise field is the worst case
    // for lossless: measured 1504 KB as PNG vs 170 KB at jpeg 0.9 for the console swatch, and 1224 vs 85
    // for a plain gradient. Worse, compressImage treats PNG as "lossless, no quality knob" and only
    // enforces its 800 KB cap for JPEG, so a card was the one upload in the app that skipped the cap
    // entirely. (The 0.92 here was already a no-op — toBlob ignores quality for image/png.)
    // Not WebP, which is smaller still: toBlob('image/webp') silently falls back to PNG on older
    // Safari/WebViews, which would reintroduce this bug invisibly on exactly the devices that can least
    // afford it. JPEG is what compressImage already emits for every other upload.
    return await new Promise(r=>cv.toBlob(r,'image/jpeg',0.9));
  }
  // Render + upload the card and return the note content: the image, then the links, LAST.
  // The words are not repeated underneath — the card is a picture OF them, so posting them again is the
  // same thing twice. The link goes at the end because that is the one part a reader has to be able to
  // tap, and it is the only part a card cannot carry.
  async function buildBgPost(text, bg, framed){
    const urls=(String(text||'').match(/https?:\/\/\S+/g)||[]).map(u=>u.replace(/[)\].,>'"]+$/,''));
    const words=_BG_WORDS(text);
    const card=_cardHook(words);
    const blob=await renderBgPost(card||' ', bg, framed);
    const url=await uploadBlob(new File([blob],'post.jpg',{type:'image/jpeg'}), {folder:'Posts'});
    // `trimmed` = the card could not hold every word. Callers surface it, so a long draft losing its tail
    // is never silent — for a link summary that is fine (the article link is right there), but it must
    // still be said out loud rather than discovered after posting.
    return { url, content: url + (urls.length ? '\n\n'+urls.join(' ') : ''),
             trimmed: !!(words && words!==card) };
  }
  // `open` ('poll' | 'ai' | 'react') auto-opens one of the composer's tools after the modal renders. The
  // timeline's inline composer uses it to surface Poll/AI as first-class buttons without reimplementing
  // their UI — the poll builder and the AI handlers live in this closure, so duplicating them inline would
  // mean two copies of each drifting apart.
  /* WHICH VIEW A SUCCESSFUL POST HAS TO REPAINT.
   *
   * Pure, and separate from the click handler, because the bug it encodes was a MISSING ENTRY in a
   * list of view names — the kind of thing that is invisible in a 40-line handler and obvious in a
   * three-line function with a test beside it. `thread` was absent: it is the one view not reached
   * through `renderView` (the route calls `renderThread(id)` instead), and it holds no live
   * subscription, so a reply posted from inside a conversation had nothing at all to bring it on
   * screen until the user navigated away and back.
   *
   * Run under node by tests/client/test_reply_appears_in_its_thread.py. */
  function _repaintAfterPost(view){
    if(view==='home' || view==='global' || view==='drafts') return 'view';
    if(view==='thread') return 'thread';
    return null;
  }
  function compose({reply=null, replyPk=null, quote=null, draftId=null, text='', articleComment=null, articleParent=null, cw=false, cwReason='', files=null, open=null}={}){
    /* A COMPOSER THAT OPENS UNDER ANOTHER WINDOW IS NOT A COMPOSER — "the new post, reply, modal
     * gets stuck behind windows". On PosterChanOS the desktop shell is a TILED sway window and
     * floating applications paint above it unconditionally, so this modal cannot be raised from
     * inside the page at any z-index. os.js installs a host that opens it as its own floating
     * window instead; everywhere else there is no host and nothing changes.
     *
     * The host takes only what survives a URL — an id, a pubkey, some text. A composer carrying
     * FILES or an article-parent OBJECT is not serialisable, so the host declines it and it opens right
     * here, exactly as it always has. */
    if(window.__PC_COMPOSE_HOST){
      try{
        if(window.__PC_COMPOSE_HOST({reply, replyPk, quote, draftId, text,
                                     articleComment, articleParent, cw, cwReason, files, open})) return;
      }catch(_){ }
    }
    const title = articleComment?(articleParent?'Reply to comment':'Comment on article'):reply?'Reply':quote?'Quote post':'New post';
    // Show the post being replied to / quoted, ditto-style — replying used to give you an empty box with
    // no reminder of what you were answering.
    const _cmpCtx=(id,label)=>{
      const o=Store.get(id); if(!o) return '';
      const p=profOf(o.pubkey); needProfile(o.pubkey);
      const nm=p.name||p.display_name||'anon';
      // mediaParts() PULLS media URLs out of the text, so rendering only .text silently dropped the
      // original's images — replying to a photo showed an empty body. Render its media too (bare items,
      // not the carousel: a paging widget inside a 180px preview is noise).
      const mp=mediaParts(o.content);
      const body=(mp.text||'').trim();
      const media=(mp.items&&mp.items.length)?`<div class="media-row cmp-ctx-media">${mp.items.join('')}</div>`:'';
      return `<div class="cmp-ctx"><div class="cmp-ctx-lbl">${label}</div>
        <div class="quoted cmp-parent" data-open="${enc(o.id)}" role="button" tabindex="0" title="Open original post"><div class="hd"><img class="qav" src="${enc(p.picture||_S.LOGO)}" onerror="this.src='${_S.LOGO}'">`
        +`<span class="name" data-prof="${o.pubkey}">${emojiName(o.pubkey,nm)}</span><span class="time">${timeAgo(o.created_at)}</span></div>`
        +`${body?`<div class="txt">${linkify(body)}</div>`:(media?'':'<div class="txt"><span class="muted small">(no text)</span></div>')}${media}</div></div>`;
    };
    let qhtml = quote ? _cmpCtx(quote,'Quoting') : (reply ? _cmpCtx(reply,'Replying to') : '');
    modal(`<h3 class="cmp-hd">${title}<button class="modal-x" id="cmp-close" title="Close"
              aria-label="Close">&#215;</button></h3>${qhtml}
      <div class="cmp-tabs"><button class="cmp-tab active" data-t="write">Write</button><button class="cmp-tab" data-t="preview"><svg class="ic b-ic" aria-hidden="true"><use href="#i-eye"></use></svg>Preview</button></div>
      <textarea id="cmp" placeholder="what's happening on the net?"></textarea>
      <div class="muted small mention-hint hidden" id="cmp-mentions"></div>
      <div id="cmp-preview" class="note-preview hidden"></div>
      <div class="cmp-tools"><button class="cmp-ico needs-net" id="cmp-attach" title="Attach an image or file" aria-label="Attach an image or file"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg></button><button class="cmp-ico" id="cmp-react" title="Emoji or GIF" aria-label="Emoji or GIF"><svg class="ic b-ic" aria-hidden="true"><use href="#i-smile"></use></svg></button>${(reply||quote||articleComment)?'':'<button class="cmp-ico" id="cmp-poll" title="Poll" aria-label="Poll"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chart"></use></svg></button>'}<button class="cmp-ico needs-net" id="cmp-ai" title="AI tools" aria-label="AI tools"><svg class="ic b-ic" aria-hidden="true"><use href="#i-ai"></use></svg></button><button class="cmp-ico" id="cmp-more" title="More — attach from Files, background, sensitive" aria-label="More — attach from Files, background, sensitive">⋯</button><span class="cmp-of"><button class="cmp-ico" id="cmp-clean" title="Clean links — remove tracking from every link" aria-label="Clean links — remove tracking from every link"><svg class="ic b-ic" aria-hidden="true"><use href="#i-broom"></use></svg></button><button class="cmp-ico" id="cmp-cw-btn" title="Mark sensitive / NSFW" aria-label="Mark sensitive / NSFW"><svg class="ic b-ic" aria-hidden="true"><use href="#i-nsfw"></use></svg></button>${(quote||articleComment)?'':`<button class="cmp-ico needs-net" id="cmp-bg-btn" title="Background — post short text as an image" aria-label="Background — post short text as an image"><svg class="ic b-ic" aria-hidden="true"><use href="#i-palette"></use></svg></button>`}<button class="cmp-ico" id="cmp-draft" title="Save to drafts" aria-label="Save to drafts"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg></button></span><input type="file" id="cmp-file" multiple hidden></div>
      ${(quote||articleComment)?'':`<div id="cmp-bg-strip" class="cmp-bg-strip hidden" aria-label="post background"></div>
      <div id="cmp-cardprev" class="cmp-cardprev hidden" aria-label="card preview"></div>`}
      <div id="cmp-cw-row" class="cmp-cw-row hidden"><input class="input" id="cmp-cw-reason" maxlength="120" placeholder="🔞 sensitive — reason (optional, e.g. nudity)"></div>
      <div class="cmp-actions">${(reply||quote||articleComment)?'':'<button class="btn btn-ghost small needs-net" id="cmp-sched-btn"><svg class="ic b-ic" aria-hidden="true"><use href="#i-clock"></use></svg>Schedule</button>'}<button class="btn btn-neon small" id="cmp-send"><svg class="ic b-ic" aria-hidden="true"><use href="#i-send"></use></svg><span id="cmp-send-label">Post</span></button></div>
      ${(reply||quote||articleComment)?'':`<div id="cmp-sched-row" class="cmp-sched-row hidden"><span class="muted small">Publish at</span><input type="datetime-local" id="cmp-sched-at" class="input"><span class="sched-chips"><button type="button" class="sched-chip" data-min="10">+10m</button><button type="button" class="sched-chip" data-min="60">+1h</button><button type="button" class="sched-chip" data-min="1440">+1d</button></span><button class="btn btn-neon small" id="cmp-sched-go"><svg class="ic b-ic" aria-hidden="true"><use href="#i-clock"></use></svg>Schedule</button><div id="cmp-sched-when" class="muted small sched-when"></div></div>`}
      <div id="cmp-pollbox" class="poll-build hidden">
        <div class="muted small">Poll options</div>
        <div id="cmp-poll-opts"><input class="input poll-opt-in" placeholder="Option 1"><input class="input poll-opt-in" placeholder="Option 2"></div>
        <div class="row"><button class="btn btn-ghost small" id="cmp-poll-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Add option</button>
          <label class="muted small" style="margin-left:auto"><input type="checkbox" id="cmp-poll-multi"> Allow multiple</label></div>
      </div>
      <div class="muted small" id="cmp-status"></div>`, root=>{
      // The composer is the one modal whose contents can outgrow the viewport (a quote/reply card on
      // top of a 220px textarea and two button rows), so it lays out as a flex COLUMN instead of a
      // single scroll box — otherwise the overflow comes off the bottom, i.e. off Post. See the
      // `.modal.cmp-modal` block in client.css; modal() has no class hook, so it goes on here.
      /* STICKY: a stray click on the backdrop must not throw away a half-written post — see
       * modal(). The ✕ added to the title row is what keeps that honest, because a composer with no
       * visible way out is a trap on any phone without a hardware Back button. */
      root.classList.add('cmp-modal', 'modal-sticky');
      { const x=$('#cmp-close',root); if(x) x.onclick=()=>closeModal(); }
      // Armada-style reply context: one readable parent preview in the composer, not a permanent
      // parent card above every message. Clicking (or pressing Enter) opens the original thread.
      { const p=$('.cmp-parent',root); if(p){ const go=()=>{ const id=p.dataset.open; closeModal(); openThread(id); };
        p.onclick=e=>{ if(!e.target.closest('[data-prof]')) go(); };
        p.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); go(); } }; } }
      const ta=$('#cmp',root); attachMentionAutocomplete(ta); if(text) ta.value=text;
      // Files shared IN from another app (OS share sheet → _consumeSharedFiles): upload each to Blossom
      // and append its URL, exactly like paste/attach. Runs async so the composer paints immediately.
      if(files && files.length){ (async()=>{ const st=$('#cmp-status',root);
        for(let i=0;i<files.length;i++){ if(st) st.textContent=`uploading shared ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i], {folder:'Posts'}); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ if(_blossomDenied&&_blossomDenied(err)){ requestBlossomAccess(); if(st) st.textContent='🔒 No upload access — requested it from the admin.'; }
            else if(st) st.textContent='upload failed: '+((err&&err.message)||err); return; } }
        if(st) st.textContent=''; try{ ta.dispatchEvent(new Event('input')); }catch(_){} })(); }
      // Auto-save as a Draft if the composer is dismissed by ACCIDENT (click-outside / Escape) with
      // unsaved text — leaving the New Post / reply window shouldn't lose what you typed. Posting or the
      // 💾 Draft button set `committed` so they don't double-save; an empty composer saves nothing.
      let committed=false;
      // Autosave WHILE TYPING, into one draft that keeps being updated (autoId), and drop it once the post
      // actually lands. The close/Escape/pagehide hooks below only fire on a graceful teardown — a crash, an
      // OOM-killed tab or a phone reaping the app in the background took the text with it. Silent (no toast):
      // this runs constantly.
      let autoId = draftId || null;
      const _draftFields=()=>({ id:autoId, text:ta.value.trim(), reply, replyPk, quote, ..._cwState() });
      const _saveDraftNow=()=>{ try{ autoId = Drafts.save(_draftFields()) || autoId; }catch(_){} };
      let _autoT=null;
      const _autosaveTick=()=>{ clearTimeout(_autoT); _autoT=setTimeout(()=>{
        if(committed || !(ta.value||'').trim()) return;
        _saveDraftNow();
      }, 1200); };
      ta.addEventListener('input', _autosaveTick);
      // The post landed — remove the draft it came from (whether the user opened one, or autosave made one).
      const _dropDraft=()=>{ clearTimeout(_autoT); const id=autoId||draftId; if(id) try{ Drafts.remove(id); }catch(_){} };
      const _autoSaveDraft=()=>{ if(committed || !(ta.value||'').trim()) return; committed=true;
        try{ _saveDraftNow(); toast('saved to drafts 💾'); if(_S.VIEW==='drafts') renderView(true); }catch(_){} };
      // Also persist on ANY page teardown — reload (incl. a service-worker update), navigation, or tab
      // close — not just Escape/click-outside, and regardless of focus. pagehide is the reliable signal
      // (beforeunload is flaky on mobile); the isConnected check makes a stale listener a no-op. This is
      // what lets the SW updater reload freely without a fragile focus-based composer guard.
      const _pageHideSave=()=>{ if(ta.isConnected) _autoSaveDraft(); };
      const _closeCmp=()=>{ document.removeEventListener('keydown',_escSave); window.removeEventListener('pagehide',_pageHideSave); closeModal(); };
      const _escSave=e=>{ if(e.key==='Escape'){ e.preventDefault(); _autoSaveDraft(); _closeCmp(); } };
      document.addEventListener('keydown', _escSave);
      window.addEventListener('pagehide', _pageHideSave);
      /* THIS HANDLER REPLACED modal()'s, AND THAT IS WHY THE COMPOSER WAS NEVER STICKY.
       *
       * `root.classList.add('cmp-modal','modal-sticky')` a few lines up is read by modal()'s
       * backdrop guard — but this line then assigned OVER that guard with an unconditional
       * save-and-close, so the class, the CSS flinch rule and the ✕ were all in place around a
       * sheet that still threw itself away on a stray click. Reported after the sticky fix had
       * shipped: "new post in social exited when I click on desktop so that is broke still", while
       * email — whose composer does not override the handler — was fine.
       *
       * The draft is still saved on Escape and on pagehide, which is where saving belongs. It has
       * no business happening here any more, because this no longer closes anything. */
      { const _bg=root.parentElement; if(_bg) _bg.onclick=e=>{
          if(e.target!==_bg) return;
          e.preventDefault(); e.stopPropagation();
          if(root.classList.contains('modal-sticky')){ _stickyNudge(root); return; }
          _autoSaveDraft(); _closeCmp(); }; }
      const _mh=$('#cmp-mentions',root); ta.addEventListener('input', ()=>updateMentionHint(ta,_mh)); updateMentionHint(ta,_mh);
      ta.addEventListener('keydown', e=>{ if((e.ctrlKey||e.metaKey) && e.key==='Enter'){ e.preventDefault(); const sb=$('#cmp-send',root); if(sb) sb.click(); } });   // Ctrl/⌘+Enter to post
      $$('.cmp-tab',root).forEach(b=> b.onclick=()=>{
        $$('.cmp-tab',root).forEach(x=>x.classList.toggle('active',x===b));
        const pv=b.dataset.t==='preview', prev=$('#cmp-preview',root);
        ta.classList.toggle('hidden',pv); prev.classList.toggle('hidden',!pv);
        if(pv){
          const paint=()=>{ prev.innerHTML = ta.value.trim()
            ? `<div class="txt">${InstEmoji.render(linkify(ta.value))}</div>`
            : '<div class="muted small">Nothing to preview.</div>'; };
          paint();
          // A typed :shortcode: must show as a picture here even if the picker was never opened in
          // this session — that's the only reason the map might not be loaded yet.
          if(!InstEmoji.loaded && InstEmoji.SC_RE.test(ta.value||'')) InstEmoji.load().then(()=>{ if(prev.isConnected && !prev.classList.contains('hidden')) paint(); });
        }
      });
      // paste image (or any file) from clipboard -> upload + append URL
      ta.addEventListener('paste', async (e)=>{
        const files=[...(e.clipboardData&&e.clipboardData.items||[])].filter(it=>it.kind==='file').map(it=>it.getAsFile()).filter(Boolean);
        if(!files.length) return; e.preventDefault();
        for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading pasted ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i], {folder:'Posts'}); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; } else $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent='';
      });
      // 📎 Attach → pick Local (this device) or Blossom (your uploaded files)
      $('#cmp-attach',root).onclick=()=>{
        // Putting a popover between the trusted click and `input.click()` made Firefox lose the
        // transient user activation, so Attach did nothing and no /upload was ever emitted. The
        // answer was to open the chooser directly on web — which silently removed "📁 Files" from
        // this button, because the menu was where it lived. Reported as "you broke Reply modal? no
        // more attach from Files", and "it is missing if I can't see it and it defaults to local
        // files!" — a choice behind a ⋯ labelled More is not a choice anybody finds.
        //
        // Both, now: the device item is a <label> bound to the input, so the browser opens the
        // chooser as the label's OWN default action and there is no programmatic click to be
        // distrusted. See openMenuPopover.
        const opts = window.Capacitor ? [['camera','📷 Camera'],['local','🖼️ Photos / files'],['blossom','📁 Files']]
                                       : [['local','💻 Local','',{htmlFor:'cmp-file'}],['blossom','📁 Files']];
        // 🎮 A webxdc mini app — a game, a poll, a shared editor — attached as a playable card.
        // Its own entry rather than a file among files: a .xdc is a zip, so picking it from "Local"
        // would upload it as an anonymous archive with no way to know it can be played.
        if(window.PCWebxdc && PCWebxdc.attach) opts.push(['webxdc','🎮 Mini app (.xdc)']);
        openMenuPopover($('#cmp-attach',root), opts, a=>{
          if(a==='camera') _captureCamera(ta, root);
          else if(a==='local') $('#cmp-file',root).click();
          else if(a==='blossom') blossomPicker(ta);
          else if(a==='webxdc') PCWebxdc.attach(ta); });
      };
      attachEmojiAutocomplete(ta);   // `:shortcode` autocomplete, same as every other composer
      // 😀 React → insert an Emoji or a GIF
      $('#cmp-react',root).onclick=(e)=>{ e.stopPropagation();
        const items=[['emoji','😀 Emoji']]; if(_S.CFG.gif_enabled) items.push(['gif','🎬 GIF']);
        openMenuPopover($('#cmp-react',root), items, a=>{
          if(a==='emoji') openEmojiPopover($('#cmp-react',root), (emoji)=>{ _insertAt(ta, emoji); });
          else if(a==='gif') gifPicker(ta); }); };
      // 📊 Poll → toggle the poll-builder; ＋ Add option grows the list
      { const pb=$('#cmp-poll',root), box=$('#cmp-pollbox',root);
        if(pb) pb.onclick=()=>{ const on=box.classList.toggle('hidden')===false; pb.classList.toggle('active',on); };
        const add=$('#cmp-poll-add',root);
        if(add) add.onclick=()=>{ const wrap=$('#cmp-poll-opts',root); const n=wrap.children.length+1;
          const i=document.createElement('input'); i.className='input poll-opt-in'; i.placeholder='Option '+n; wrap.appendChild(i); i.focus(); };
      }
      // 🤖 AI → a small menu (✨ AI Enhancer = summarize a pasted link into a post; # Hashtags = suggest
      // + append). Uses the shared openMenuPopover so it's consistent with the other menus and becomes a
      // readable bottom-sheet on mobile (was a cramped hand-rolled dropdown).
      { const aiBtn=$('#cmp-ai',root);
        const firstUrl=()=>{ const m=(ta.value||'').match(/https?:\/\/[^\s]+/i); return m?m[0]:null; };
        const hasImage=()=>/(?:!\[|https?:\/\/\S+\.(?:png|jpe?g|gif|webp)\b|\/blossom\/|media\.)/i.test(ta.value||'');
        let lastTags='';   // the EXACT hashtag block we last appended — only strip THIS on re-run, never the user's own tags
        const doEnhance=async()=>{
          const url=firstUrl(); if(!url){ toast('paste a link into the post first'); return; }
          $('#cmp-status',root).textContent='summarizing link…';
          try{
            const r=await fetch('/client/compose-from-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(r=>r.json());
            if(r&&r.text){ ta.value=r.text; lastTags=''; $('#cmp-status',root).textContent=''; ta.dispatchEvent(new Event('input')); }
            else $('#cmp-status',root).textContent='couldn\'t summarize: '+((r&&r.error)||'no content');
          }catch(_){ $('#cmp-status',root).textContent='summarize failed'; }
        };
        const doTags=async()=>{
          const body=(ta.value||'').trim(); if(!body && !hasImage()){ toast('write something first'); return; }
          $('#cmp-status',root).textContent='finding hashtags…';
          try{
            const r=await fetch('/client/hashtags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:body, has_image:hasImage()})}).then(r=>r.json());
            if(r&&r.hashtags){
              // Re-run: strip ONLY the exact block WE appended last time (if it's still at the end), so a
              // user's own hand-typed trailing #tags are preserved — never blanket-strip trailing hashtags.
              let base=body;
              if(lastTags && base.endsWith(lastTags)) base=base.slice(0, base.length-lastTags.length).replace(/\s+$/,'');
              ta.value = base + (base?'\n\n':'') + r.hashtags; lastTags=r.hashtags;
              $('#cmp-status',root).textContent=''; ta.dispatchEvent(new Event('input'));
            } else $('#cmp-status',root).textContent='no hashtags: '+((r&&r.error)||'try again');
          }catch(_){ $('#cmp-status',root).textContent='hashtags failed'; }
        };
        // 🖼️ Framed card is offered only where a 🎨 background can be (the strip is hidden for replies
        // and quotes), so the menu never lists something that cannot apply here.
        const doCard=()=>_aiFramedCard(ta, m=>{ const s=$('#cmp-status',root); if(s) s.textContent=m; }, {
          framed: _bgFramed, hasBg: !!_bgChoice, set: v=>{ _bgFramed=v; _bgFramePreview(); },
          reveal: ()=>{ const strip=$('#cmp-bg-strip',root), b=$('#cmp-bg-btn',root);
            if(strip && strip.classList.contains('hidden')){ strip.classList.remove('hidden'); if(b) b.classList.add('active'); } } });
        if(aiBtn) aiBtn.onclick=(e)=>{ e.stopPropagation();
          const items=[['enhance','✨ AI Enhancer'],['tags','# Hashtags'],['emoji','😀 Suggest emoji'],['translate','🌐 Translate']];
          if($('#cmp-bg-strip',root)) items.push(['card', (_bgFramed?'🖼️ Framed card ✓':'🖼️ Framed card')]);
          openMenuPopover(aiBtn, items, a=>{ if(a==='enhance') doEnhance(); else if(a==='tags') doTags();
            else if(a==='emoji') _aiEmojiSuggest(ta, m=>{ const s=$('#cmp-status',root); if(s) s.textContent=m; });
            else if(a==='card') doCard(); else if(a==='translate') composeTranslate(ta, aiBtn); }); };
      }
      /* ⋯ overflow. The items CLICK the hidden buttons rather than re-implementing them: Sensitive
       * and Background are toggles whose `.on` class IS the composer's state (read by _cwState, by
       * the draft restore, and by the bg strip), so re-homing them into a menu would mean moving
       * that state too. This way there is exactly one handler and one source of truth per action,
       * and the menu only decides what is VISIBLE. The ✓ mirrors that state back, the same way the
       * AI menu marks Framed card — a menu that closes on pick otherwise says nothing about what is
       * already armed. */
      { const mb=$('#cmp-more',root);
        if(mb) mb.onclick=e=>{ e.stopPropagation();
          const has=id=>!!$(id,root), on=id=>{ const b=$(id,root); return !!b && b.classList.contains('on'); };
          const items=[];
          if(has('#cmp-clean'))  items.push(['clean','🧹 Clean links']);
          if(has('#cmp-cw-btn')) items.push(['cw', on('#cmp-cw-btn')?'🔞 Sensitive ✓':'🔞 Sensitive']);
          if(has('#cmp-bg-btn')) items.push(['bg', on('#cmp-bg-btn')?'🎨 Background ✓':'🎨 Background']);
          if(has('#cmp-draft'))  items.push(['draft','☁️ Save to drafts']);
          items.push(['files','📁 Attach from Files']);
          const sel={clean:'#cmp-clean', cw:'#cmp-cw-btn', bg:'#cmp-bg-btn', draft:'#cmp-draft'};
          openMenuPopover(mb, items, a=>{ if(a==='files'){ blossomPicker(ta); return; }
            const b=sel[a] && $(sel[a],root); if(b) b.click(); }); }; }
      /* 🧹 Clean links — its OWN button, not an item in the 🤖 AI menu. It never calls the model or
       * the network (see _cleanLinksCmd / urlclean.js), and filing it under AI both misdescribed it
       * and made it unreachable offline, since the AI button carries `needs-net`. This one does not. */
      { const cb=$('#cmp-clean',root);
        if(cb) cb.onclick=()=>_cleanLinksCmd(ta, m=>{ const s=$('#cmp-status',root); if(s) s.textContent=m; }); }
      $('#cmp-file',root).onchange=async e=>{ const files=[...e.target.files]; if(!files.length)return;
        for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i], {folder:'Posts'}); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; } else $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent=''; e.target.value=''; };
      // drag & drop files onto the composer → upload + append URLs (same as 📎 Attach / paste)
      { const _cmpDrop=async files=>{ files=files.filter(Boolean); if(!files.length)return;
          for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading ${i+1}/${files.length}…`;
            try{ const url=await uploadBlob(files[i], {folder:'Posts'}); ta.value+=(ta.value?'\n':'')+url; }
            catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; } else $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
          $('#cmp-status',root).textContent=''; };
        root.addEventListener('dragover',e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); root.classList.add('cmp-drop'); } });
        root.addEventListener('dragleave',e=>{ if(e.target===root) root.classList.remove('cmp-drop'); });
        root.addEventListener('drop',async e=>{ if(!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')))return; e.preventDefault(); root.classList.remove('cmp-drop'); await _cmpDrop([...(e.dataTransfer.files||[])]); });
      }
      $('#cmp-draft',root).onclick=()=>{
        const body=ta.value.trim(); if(!body){ toast('nothing to save'); return; }
        committed=true; Drafts.save({id:draftId, text:body, reply, replyPk, quote, ..._cwState()}); _closeCmp(); toast('saved to drafts');
        if(_S.VIEW==='drafts') renderView(true);
      };
      // 🔞 sensitive / NSFW (NIP-36): toggle a content-warning, optionally with a reason.
      { const cb=$('#cmp-cw-btn',root); if(cb) cb.onclick=()=>{ cb.classList.toggle('on'); const r=$('#cmp-cw-row',root); if(r) r.classList.toggle('hidden', !cb.classList.contains('on')); const ri=$('#cmp-cw-reason',root); if(ri && cb.classList.contains('on')) ri.focus(); }; }
      // restore the toggle when re-opening a draft that had it set (else a sensitive post posts unflagged)
      if(cw){ const cb=$('#cmp-cw-btn',root); if(cb) cb.classList.add('on'); const r=$('#cmp-cw-row',root); if(r) r.classList.remove('hidden'); const ri=$('#cmp-cw-reason',root); if(ri) ri.value=cwReason||''; }
      const _cwState=()=>{ const cb=$('#cmp-cw-btn',root); return cb && cb.classList.contains('on') ? { cw:true, cwReason:(($('#cmp-cw-reason',root)||{}).value||'').trim() } : { cw:false, cwReason:'' }; };
      const _applyCw=(tags)=>{ const s=_cwState(); if(s.cw) tags.push(['content-warning', s.cwReason]); };
      // 🎨 Background: swatch strip; picking one previews it in the composer and, on Post, renders the
      // text onto it as an IMAGE. Only for SHORT plain text — a link/media or long text drops it + warns.
      let _bgChoice=null;
      let _bgFramed=false;   // 🖼️ Framed card (AI menu) — a border on the 🎨 background post
      // The modal already previews the background by tinting the textarea, so preview the frame there too —
      // otherwise the only way to find out what "framed" looks like is to post it.
      const _cardPrev=makeCardPreview($('#cmp-cardprev',root)||document.createElement('div'));
      const _bgFramePreview=()=>_cardPrev(ta.value, _bgChoice, _bgFramed);
      { const bgBtn=$('#cmp-bg-btn',root), strip=$('#cmp-bg-strip',root);
        if(bgBtn && strip){
          const select=(bg,el)=>{ _bgChoice=bg; $$('.cmp-swatch',strip).forEach(s=>s.classList.toggle('on', s===el));
            // Clearing the background must clear the FRAME with it. _bgFramed survived a ✕, so the
            // AI menu still read "🖼️ Framed card ✓" with no card armed, and the next background you
            // picked came out framed without asking — which is what made ✕ look like it half-worked.
            if(!bg) _bgFramed=false;
            _bgFramePreview();   // dropping the background must drop its preview with it
          };
          // ✕, matching the strip at the top of Social. This said "Aa", so the same control was labelled
          // two different ways depending on which composer you opened.
          const none=document.createElement('button'); none.type='button'; none.className='cmp-swatch cmp-swatch-none on'; none.title='no background'; none.textContent='✕';
          none.onclick=()=>select(null,none); strip.appendChild(none);
          // Length and links no longer disqualify a background: buildBgPost puts the hook on the card and
          // the rest — remaining text, source link — underneath, where a URL is still clickable. Only a
          // draft with NO words left has nothing to draw.
          const _bgWhyNot=()=>_BG_WORDS(ta.value) ? ''
            : 'that is just a link — use 🤖 AI → 🖼️ Framed card to turn it into a card';
          CMP_BGS.forEach(bg=>{ const s=document.createElement('button'); s.type='button'; s.className='cmp-swatch'; s.title=bg.id; s.style.background=_bgCss(bg);
            if(bg.deco) s.textContent=bg.deco[0];   // show the holiday emoji so the swatch is recognisable
            // Refuse the pick and say why, as the Social strip does. Without this the modal let you arm a
            // background on a bare link and only complained at Post, after the card had already failed.
            s.onclick=()=>{ const why=_bgWhyNot(); if(why){ const st=$('#cmp-status',root); if(st) st.textContent=why; return; } select(bg,s); };
            strip.appendChild(s); });
          bgBtn.onclick=(e)=>{ e.stopPropagation(); const on=strip.classList.toggle('hidden')===false; bgBtn.classList.toggle('active',on);
            if(!on && _bgChoice) select(null,none);   // collapsing the row must not leave a hidden background armed
          };
          const _tooBig=()=>!!_bgWhyNot();
          ta.addEventListener('input', ()=>{ _bgFramePreview();
            if(_bgChoice && _tooBig()){ select(null,none);
            $('#cmp-status',root).textContent='background removed — nothing left to put on the card';
            setTimeout(()=>{ const st=$('#cmp-status',root); if(st && st.textContent.startsWith('background removed')) st.textContent=''; },2500); } });
        }
      }
      // Offline, the button says what it will actually do — the post gets signed and queued, not sent.
      // Set once here for a composer opened while already offline; _setOffline keeps it in sync after that,
      // so there is no per-composer timer or listener to leak.
      _syncSendLabel(root);
      $('#cmp-send',root).onclick=async()=>{
        // Auto-clean (Settings → 🧹 Remove link trackers) runs here, ahead of every branch below —
        // a reply, a poll and an article comment all read ta.value from this one
        // spot, and all of them derive imeta/mention tags from the URLs in it.
        { const n=_autoCleanOnPost(ta); if(n) toast(`🧹 cleaned ${n} link${n===1?'':'s'}`); }
        const text=ta.value.trim();
        if(!text && !quote){
          // Do not let an empty reply reach signers/NIP-44, and do not fail as an invisible no-op.
          const st=$('#cmp-status',root); if(st) st.textContent=reply?'Write a reply first.':'Write something first.';
          ta.focus(); return;   // a quote-repost may have no comment; everything else needs content
        }
        committed=true; document.removeEventListener('keydown',_escSave);   // posting → don't auto-save; drop the Escape hook
        // 📊 Poll (NIP-88 kind-1068) — only for top-level posts; question = text, options from the builder.
        { const pbox=$('#cmp-pollbox',root); if(pbox && !pbox.classList.contains('hidden')){
            const labels=[...$$('.poll-opt-in',root)].map(i=>i.value.trim()).filter(Boolean);
            if(labels.length<2){ $('#cmp-status',root).textContent='add at least 2 poll options'; return; }
            const multi=$('#cmp-poll-multi',root).checked;
            const tags=[['polltype', multi?'multiplechoice':'singlechoice']];
            labels.forEach((l,i)=>tags.push(['option','opt'+(i+1), l]));
            mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
            imetaTagsFor(text).forEach(t=>tags.push(t));
            _applyCw(tags);
            // A poll's options/mode can't round-trip through a plain-text draft, so DON'T close the modal
            // until the relay accepts it — that way a blip leaves the composer (with its options) intact to
            // retry, rather than losing them.
            try{ const r=await publish(1068, text, tags);
              if(r&&r.ok){ closeModal(); toast('poll posted'); if(_S.VIEW==='home'||_S.VIEW==='global') renderView(true); } }
            catch(e){ toast('poll failed: '+((e&&e.message)||e)); } return;
          } }
        // Article comment → NIP-22 comment (kind 1111) scoped to that root.
        if(articleComment){
          let tags = articleCommentTags(articleComment, articleParent);
          mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
          imetaTagsFor(text).forEach(t=>tags.push(t));
          _applyCw(tags);
          // An article comment carries scope tags a plain draft can't preserve, so keep the modal
          // open until the relay accepts it — a blip leaves the text in place to retry rather than orphaning
          // a scopeless draft that would post to the home feed.
          try{ const r=await publish(1111, text, tags);
            if(r && r.ok){ closeModal(); toast('comment posted'); if(_S.VIEW==='article') openArticle(articleComment); }
          }catch(e){ toast('post failed: '+((e&&e.message)||e)); } return;
        }
        // 🎨 Background post → render the text onto the chosen background + upload; post the IMAGE (the
        // styled text IS the post). Top-level posts and REPLIES; quotes/article-comments still
        // hide the 🎨 button — a quote has to carry its nevent in the content and an article comment is a
        // different kind (1111), so neither is a plain kind-1 image note.
        if(_bgChoice && !quote){
          // Last parity gap with the Social strip: it refuses at SEND when there are no words left to put
          // on the card, rather than rendering a blank one. Reaching here is now unlikely (picking is
          // guarded and typing the words away disarms it), but the two should fail the same way.
          // Send already set committed=true and dropped the Escape hook (it was about to publish), so a
          // bare return here would leave the composer unable to autosave the draft. Put both back — we are
          // not posting after all.
          if(!_BG_WORDS(text)){
            committed=false; document.addEventListener('keydown', _escSave);
            $('#cmp-status',root).textContent='nothing to put on the card — write something, or use 🤖 AI → 🖼️ Framed card to summarize the link';
            return;
          }
          const sb=$('#cmp-send',root); if(sb) sb.disabled=true; $('#cmp-status',root).textContent='rendering…';
          try{
            const built=await buildBgPost(text, _bgChoice, _bgFramed);
            $('#cmp-status',root).textContent='uploading…';
            if(built.trimmed) toast('card shows the opening — the rest did not fit on it');
            const url=built.content;   // the image, plus anything that did not fit on the card
            // A background REPLY is still a reply: without these it publishes as a top-level note and the
            // thread it was written into never sees it. Same replyTags() the plain path uses, and FIRST so
            // the e/p markers lead the tag list exactly as they do there.
            const btags=reply ? replyTags(Store.get(reply), reply, replyPk) : [];
            imetaTagsFor(url).forEach(t=>btags.push(t)); _applyCw(btags);
            mentionTags(url).forEach(t=>{ if(!btags.some(x=>x[0]==='p'&&x[1]===t[1])) btags.push(t); });
            // Save a draft before publishing so the text survives a failure (the styled image is re-rendered
            // from it on retry), and only report success / drop the draft when the relay actually stored it.
            _saveDraftNow(); closeModal();
            const r=await publish(1, url, btags);
            if(r && r.ok){ _dropDraft(); toast(_bgFramed?'posted 🖼️':'posted 🎨'); }   // failure toast + kept draft handled by publish()
            if(_S.VIEW==='home'||_S.VIEW==='global'||_S.VIEW==='drafts') renderView(true);
          }catch(err){ committed=false; const sb2=$('#cmp-send',root); if(sb2) sb2.disabled=false;
            if(typeof _blossomDenied==='function' && _blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; }
            else $('#cmp-status',root).textContent='background post failed: '+((err&&err.message)||err); }
          return;
        }
        let tags=[]; let content=text;
        if(reply){ const o=Store.get(reply); tags=replyTags(o, reply, replyPk); }
        if(quote){ const o=Store.get(quote); const qpk=(o&&o.pubkey)||''; tags.push(['q', quote, _S.CFG.relay_url||'', qpk]); if(qpk)tags.push(['p',qpk]); content=_appendQuoteNevent(content, quote, qpk); }
        mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
        imetaTagsFor(text).forEach(t=>tags.push(t));
        _applyCw(tags);
        // Save the draft BEFORE we close + publish, so the text is never lost if the post fails (the modal is
        // already gone by then). On success we remove it; on failure it stays in Drafts as the recovery path.
        _saveDraftNow();
        closeModal();
        { const r=await publish(replyKindFor(reply?Store.get(reply):null), content, tags);
          /* A QUEUED post KEEPS its draft, and registers which draft belongs to it.
           *
           * The old rule dropped it: "a queued one is not lost — it is in the timeline with a
           * Pending badge". That holds only while the event can eventually be accepted. A post the
           * relay will REFUSE for ever (a wrongly-signed one, say) is queued, badged, retried, and
           * finally given up on — by which time the only copy of what somebody wrote is gone. That
           * is exactly how a quote post was lost, and "Drafts is empty" is how it was reported.
           *
           * So the draft stays until the event actually SENDS: _flushOutbox clears it through this
           * mapping on success, and deliberately keeps it when an item is dropped — that copy is
           * then the only place the text still exists. */
          if(r && r.ok) _dropDraft();
          else if(r && r.queued && r.ev) _qDraftSet(r.ev.id, autoId||draftId);
          if(r && r.ok) toast('posted'); }   // failure toast + kept draft handled by publish()
        { const what=_repaintAfterPost(_S.VIEW);
          if(what==='view') renderView(true);
          else if(what==='thread' && renderThread._tok) renderThread(renderThread._tok); }
        /* A REPLY POSTED FROM INSIDE A THREAD HAD NOTHING TO REPAINT IT.
         *
         * The thread is the one view that is not reached through `renderView` — it is rendered by
         * `renderThread(id)` off the route — so it was simply absent from the list above, and
         * `renderThread` holds NO live subscription (it is a one-shot query). Between those two
         * facts there was no path at all by which a reply you had just sent could appear: it
         * published fine, it was in the Store, and the conversation on screen never changed until
         * you navigated away and came back, which is exactly how it was reported.
         *
         * `publish()` has already saved the event optimistically, so this costs nothing: the
         * cache-first paint puts the reply on screen immediately and the refresh behind it is the
         * ordinary one. Re-rendering the SAME id keeps the scroll position (`_same` in
         * renderThread), so the conversation does not jump under the user's thumb. */
      };
      // ⏰ Schedule (plain top-level posts): sign the note with a future created_at; the backend publishes it.
      { const sbtn=$('#cmp-sched-btn',root), srow=$('#cmp-sched-row',root), sat=$('#cmp-sched-at',root);
        if(sbtn && srow && sat){
          // Live "→ fires <local time> (in N min)" readout so what you're scheduling is unambiguous — the
          // silent 1-hour default used to surprise people who thought they'd picked a nearer time.
          const _when=$('#cmp-sched-when',root);
          const _updWhen=()=>{ if(!_when) return; const ts=Math.floor(new Date(sat.value).getTime()/1000);
            if(!sat.value || isNaN(ts)){ _when.textContent=''; return; }
            const mins=Math.round((ts-Date.now()/1000)/60);
            const rel = mins<1?'now' : mins<60?`in ${mins} min` : mins<1440?`in ${(mins/60).toFixed(mins%60?1:0)} h` : `in ${Math.round(mins/1440)} d`;
            _when.textContent=`→ publishes ${new Date(ts*1000).toLocaleString()} (${rel})`; };
          sat.addEventListener('input', _updWhen);
          $$('.sched-chip',srow).forEach(c=> c.onclick=()=>{ sat.value=_dtLocal(new Date(Date.now()+(+c.dataset.min)*60000)); _updWhen(); });
          sbtn.onclick=()=>{ const show=srow.classList.toggle('hidden')===false; sbtn.classList.toggle('on', show);
            if(show){ sat.min=_dtLocal(new Date(Date.now()+60*1000)); if(!sat.value) sat.value=_dtLocal(new Date(Date.now()+10*60*1000)); _updWhen(); sat.focus(); } };   // default: +10 min (short, and the readout shows it)
          $('#cmp-sched-go',root).onclick=async()=>{
            const st=$('#cmp-status',root), go=$('#cmp-sched-go',root);
            { const n=_autoCleanOnPost(ta); if(n) toast(`🧹 cleaned ${n} link${n===1?'':'s'}`); }
            const text=ta.value.trim();
            const whenTs=Math.floor(new Date(sat.value).getTime()/1000);
            if(!sat.value || isNaN(whenTs)){ st.textContent='pick a date & time'; return; }
            if(whenTs < Math.floor(Date.now()/1000)+30){ st.textContent='pick a time at least a minute from now'; return; }
            go.disabled=true; st.textContent='scheduling…';
            try{
              // Build the SAME event the immediate Post ▶ would — text/image note, poll, or background —
              // just signed with the future time and queued instead of published.
              let kind=1, content=text, tags=[];
              const _pb=$('#cmp-pollbox',root);
              if(_pb && !_pb.classList.contains('hidden')){                 // 📊 poll (kind 1068)
                if(!text){ st.textContent='write a poll question'; go.disabled=false; return; }
                const labels=[...$$('.poll-opt-in',root)].map(i=>i.value.trim()).filter(Boolean);
                if(labels.length<2){ st.textContent='add at least 2 poll options'; go.disabled=false; return; }
                kind=1068; tags=[['polltype', $('#cmp-poll-multi',root).checked?'multiplechoice':'singlechoice']];
                labels.forEach((l,i)=>tags.push(['option','opt'+(i+1), l]));
                mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
                imetaTagsFor(text).forEach(t=>tags.push(t)); _applyCw(tags);
              } else if(typeof _bgChoice!=='undefined' && _bgChoice){        // 🎨 background → render+upload now, post the image
                if(!text){ st.textContent='write something first'; go.disabled=false; return; }
                st.textContent='rendering…'; const _b=await buildBgPost(text, _bgChoice, _bgFramed);
                st.textContent='uploading…'; content=_b.content;
                if(_b.trimmed) toast('card shows the opening — the rest did not fit on it');
                imetaTagsFor(content).forEach(t=>tags.push(t)); _applyCw(tags);
              } else {                                                       // plain text (incl. attached media URLs in the text)
                if(!text){ st.textContent='write something to schedule'; go.disabled=false; return; }
                mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
                imetaTagsFor(text).forEach(t=>tags.push(t)); _applyCw(tags);
              }
              const r=await Scheduled.create(kind, content, tags, whenTs);
              if(r && r.ok){ committed=true; document.removeEventListener('keydown',_escSave); _dropDraft(); closeModal();
                toast('scheduled for '+new Date(whenTs*1000).toLocaleString()); if(_S.VIEW==='drafts') renderView(true); }
              else { go.disabled=false; st.textContent='schedule failed: '+((r&&r.error)||'try again'); }
            }catch(err){ go.disabled=false;
              if(typeof _blossomDenied==='function' && _blossomDenied(err)){ requestBlossomAccess(); st.textContent='🔒 No upload access — requested it from the admin.'; }
              else st.textContent='schedule failed: '+((err&&err.message)||err); }
          };
        } }
      ta.focus();
      // Auto-open a tool when the caller asked for one (the timeline composer's 📊 / 🤖 / 😀 buttons).
      // Clicking the real control reuses its existing handler, so there's no second implementation to
      // keep in sync — and no-op if that button isn't present for this composer variant (reply, quote…).
      if(open){ const b=$({poll:'#cmp-poll', ai:'#cmp-ai', react:'#cmp-react'}[open]||'', root); if(b) b.click(); }
    });
  }

  return {
    buildBgPost, compose, renderBgPost,
  };
};
