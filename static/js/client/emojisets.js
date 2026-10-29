/* "Use this instance's emoji in other Nostr clients": adds every pack the node publishes as an emoji set
 * (NIP-51 kind 30030, app/services/emoji_sets.py) to the account's kind-10030 emoji list -- the list Amethyst,
 * Nostrudel, Coracle and friends read to show a person's packs.
 *
 * A 10030 is REPLACEABLE: writing it over a read that never arrived wipes every pack the person added elsewhere
 * (the follows-wipe shape this client keeps re-learning). So the list is read first, written only once this node's
 * relay has answered (Relay.homeAnswered, or every relay EOSE'd), and MERGED -- tags it holds are kept as they are,
 * only missing set coordinates are added. Loaded on demand from Settings -> Social. */
(function(){
  'use strict';
  async function addAll(){
    const PC = window.__PC, R = window.Relay, me = PC && PC.me && PC.me();
    if(!me || !me.pubkey) throw new Error('Sign in first');
    const r = await fetch('/client/emoji-sets');
    if(!r.ok) throw new Error('This server could not list its emoji packs');
    const j = await r.json();
    const sets = (j && j.sets) || [];
    if(!sets.length || !j.pubkey) throw new Error('This server publishes no emoji packs yet');
    if(R.ready) await R.ready(8000);
    const got = await R.query([{ kinds:[10030], authors:[me.pubkey], limit:5 }], 8000);
    if(!(got && (got.complete === true || (R.homeAnswered && R.homeAnswered(got)))))
      throw new Error('Your relays did not answer, so nothing was changed. Try again in a moment.');
    const cur = (got || []).filter(e => e && e.pubkey === me.pubkey && e.kind === 10030)
      .sort((a, b) => b.created_at - a.created_at || (a.id < b.id ? -1 : 1))[0];
    const tags = cur ? cur.tags.map(t => t.slice()) : [];
    const have = new Set(tags.filter(t => t[0] === 'a').map(t => t[1]));
    let added = 0;
    for(const s of sets){
      const a = '30030:' + j.pubkey + ':' + s.d;
      if(have.has(a)) continue;
      tags.push(j.relay ? ['a', a, j.relay] : ['a', a]);
      added++;
    }
    if(!added) return { added: 0, total: sets.length };
    const res = await PC.publish(10030, cur ? (cur.content || '') : '', tags);
    if(!res || res.ok === false) throw new Error((res && res.msg) || 'Your relays did not accept the list');
    return { added, total: sets.length };
  }
  window.PCEmojiSets = { addAll };
})();
