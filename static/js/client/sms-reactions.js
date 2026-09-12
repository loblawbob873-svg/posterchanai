/* SMS text fallback reactions are a presentation inference, never carrier metadata.
 * Consumers supply a strict one-to-one thread and actual sender identity, namespaced
 * row/document ids, and eligible=true only for received or successfully sent rows.
 * Incomplete history and ambiguous targets remain ordinary text. Inputs are untouched. */
(function(root){
  'use strict';
  const forms = [
    ['heart','❤️','Loved','Removed a heart from'],
    ['like','👍','Liked','Removed a like from'],
    ['dislike','👎','Disliked','Removed a dislike from'],
    ['laugh','😂','Laughed at','Removed a laugh from'],
    ['emphasize','‼️','Emphasized','Removed an emphasis from'],
    ['question','❓','Questioned','Removed a question mark from']
  ];
  function parse(body){
    if(typeof body!=='string') return null;
    for(const [kind,emoji,add,remove] of forms){
      for(const [prefix,operation] of [[add,'add'],[remove,'remove']]){
        if(!body.startsWith(prefix+' ')) continue;
        const quoted=body.slice(prefix.length+1), first=quoted[0];
        const close=first==='“'?'”':first==='"'?'"':'';
        if(!close || quoted.length<3 || !quoted.endsWith(close)) continue;
        return {kind,emoji,operation,text:quoted.slice(1,-1)};
      }
    }
    return null;
  }
  function project(rows,historyComplete){
    const chipsByTarget=Object.create(null), consumedIds=[];
    if(historyComplete!==true || !Array.isArray(rows)) return {chipsByTarget,consumedIds};
    const unique=new Map(), conflicts=new Set();
    for(const row of rows){
      if(!row || typeof row.id!=='string' || !row.id) continue;
      const fields=[row.id,row.thread,row.actor,row.body,row.date,row.incoming,row.eligible,row.group];
      const prior=unique.get(row.id);
      if(prior && JSON.stringify(prior.fields)!==JSON.stringify(fields)) conflicts.add(row.id);
      else if(!prior) unique.set(row.id,{row,fields});
    }
    // Conflicting identities mean this snapshot cannot establish unique targets.
    if(conflicts.size) return {chipsByTarget,consumedIds};
    const messages=[...unique.values()].map(x=>x.row).filter(m=>!conflicts.has(m.id)
      && typeof m.thread==='string' && m.thread && typeof m.actor==='string' && m.actor
      && typeof m.body==='string' && Number.isSafeInteger(m.date) && m.date>=0
      && typeof m.incoming==='boolean' && m.eligible===true && m.group===false);
    const operations=[];
    for(const row of messages){
      const parsed=parse(row.body); if(!parsed) continue;
      const targets=messages.filter(m=>m.id!==row.id && m.thread===row.thread
        && m.incoming!==row.incoming && m.date<row.date && m.body===parsed.text && !parse(m.body));
      if(targets.length===1) operations.push({row,parsed,target:targets[0].id});
    }
    // Equal-time operations have no reliable ordering in provider/backup history.
    const ties=new Map();
    const key=o=>JSON.stringify([o.row.thread,o.target,o.row.actor]);
    for(const o of operations){const k=JSON.stringify([key(o),o.row.date]);ties.set(k,(ties.get(k)||0)+1);}
    operations.sort((a,b)=>a.row.date-b.row.date);
    const active=new Map();
    for(const o of operations){
      const k=key(o); if(ties.get(JSON.stringify([k,o.row.date]))>1) continue;
      const previous=active.get(k);
      if(o.parsed.operation==='remove'){
        if(!previous || previous.kind!==o.parsed.kind) continue;
        active.delete(k);
      }else active.set(k,{target:o.target,actor:o.row.actor,kind:o.parsed.kind,emoji:o.parsed.emoji,sourceId:o.row.id});
      consumedIds.push(o.row.id);
    }
    for(const chip of active.values()) (chipsByTarget[chip.target]||(chipsByTarget[chip.target]=[])).push(chip);
    return {chipsByTarget,consumedIds};
  }
  /* SENDING one. `SmsReactions.java` has had this since the native Texts app shipped; the web
   * half could only ever READ a tapback, which is why the screen showed other people's reactions
   * as chips and offered no way to answer one.
   *
   * The curly quotes are load-bearing and are NOT a typo for ASCII ones: this is the exact string
   * iOS and Google Messages emit on an SMS fallback, and it is what every other phone's parser
   * matches on. `parse` accepts either pair when reading (some senders use ASCII), but what we
   * SEND has to be the interoperable one or the tapback lands on an iPhone as a line of text.
   * Byte-identical to the Java, asserted across both languages in
   * tests/test_sms_reactions_projection.py. */
  function format(kind,remove,text){
    if(typeof text!=='string' || !text) throw new Error('Missing reaction text');
    for(const [k,,add,undo] of forms) if(k===kind) return (remove?undo:add)+' \u201c'+text+'\u201d';
    throw new Error('Unknown reaction kind');
  }
  const api={parse,project,format,forms:forms.map(([kind,emoji])=>({kind,emoji}))};
  if(typeof module==='object' && module.exports) module.exports=api;
  else root.PCSmsReactions=api;
})(typeof window==='object'?window:globalThis);
