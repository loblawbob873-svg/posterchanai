/* Offline outbox — the ONE safe slice of "write while offline".
 *
 * There used to be a general Outbox here and it was removed for good reasons: replaying signed events
 * behind the user's back is what produced the follows-list wipe and the duplicate posts. Nothing about
 * that has changed, so this deliberately does NOT bring it back. What it brings back is the subset where
 * replay is provably harmless:
 *
 *   - Only NON-REPLACEABLE kinds (see QUEUEABLE). A kind-1 note, a repost, a reaction, a comment and a
 *     highlight are each addressed by their own event id, so re-sending one is a no-op at any relay that
 *     already has it. There is no "last write wins" and therefore nothing to lose.
 *   - Everything replaceable or addressable is REFUSED: kind 0 (profile), 3 (follows), 10000-19999 and
 *     30000-39999 all overwrite a whole document, which is precisely the shape that got a list erased.
 *     Those keep failing loudly, exactly as they do today.
 *   - Kind 5 (delete) is refused too. It is destructive and irreversible; a delete the user believes
 *     failed must not fire an hour later from a queue they have forgotten about.
 *
 * The queue is VISIBLE. Every entry renders as a "Pending" badge on the post it belongs to, the offline
 * banner counts them, and each one can be sent or discarded by hand. Nothing here fires without the user
 * having been told it is waiting — that is the difference between a queue and the replay that broke things.
 *
 * Entries are the FULL SIGNED event: signing happens at compose time, while the signer is present and the
 * user is watching. Flushing never signs anything, so a queue draining in the background can never surface
 * a signer prompt for something the user no longer remembers asking for.
 */
(function(){
  const KEY = 'pc_outbox';
  const MAX = 200;                    // hard cap; a queue longer than this is a bug, not a use case
  const MAX_AGE = 7 * 86400;          // a week-old reaction arriving out of nowhere is noise — expire it
  const MAX_TRIES = 5;                // see flush(): past this an entry is poison, not merely unlucky

  // Non-replaceable, id-addressed kinds only. Adding to this set is a correctness decision, not a
  // preference: the kind must be one where re-sending the identical event is a no-op at the relay.
  //   1    text note          6    repost          7    reaction
  //   1111 NIP-22 comment     9802 highlight
  const QUEUEABLE = new Set([1, 6, 7, 1111, 9802]);

  function _load(){
    try{
      const a = JSON.parse(localStorage.getItem(KEY) || '[]');
      return Array.isArray(a) ? a.filter(x => x && x.ev && x.ev.id && x.ev.sig) : [];
    }catch(_){ return []; }
  }
  function _save(list){
    try{ localStorage.setItem(KEY, JSON.stringify(list)); }
    catch(_){ /* quota / private mode → the queue is in-memory for this session only */ }
  }
  // Notify the UI. A plain DOM event so app.js can re-render badges and the banner without this module
  // knowing anything about either.
  function _changed(){ try{ window.dispatchEvent(new CustomEvent('pc:outbox')); }catch(_){} }

  let items = _load();
  let _flushing = false;

  // Drop anything past MAX_AGE on load. Done here rather than at flush time so a stale entry never
  // survives long enough to be sent — and so the count the user sees is the count that will actually go.
  (function _expire(){
    const now = Math.floor(Date.now()/1000);
    const keep = items.filter(it => (now - (it.at || 0)) < MAX_AGE);
    if (keep.length !== items.length){ items = keep; _save(items); }
  })();

  const Outbox = {
    // Whether an event of this kind may be queued AT ALL. Callers use it to decide between queueing and
    // failing; it is the single place the safety rule lives.
    canQueue(kind){ return QUEUEABLE.has(Number(kind)); },

    count(){ return items.length; },
    list(){ return items.slice(); },
    has(id){ return items.some(it => it.ev.id === id); },

    // Queue a SIGNED event. Returns false (and queues nothing) for any kind that is not provably safe to
    // replay, so a caller that forgets to check canQueue still cannot create the dangerous case.
    add(ev){
      if (!ev || !ev.id || !ev.sig || !this.canQueue(ev.kind)) return false;
      if (this.has(ev.id)) return true;                       // already waiting — adding twice is a no-op
      if (items.length >= MAX) return false;                  // refuse rather than silently evict a post
      items.push({ ev, at: Math.floor(Date.now()/1000), tries: 0 });
      _save(items); _changed();
      return true;
    },

    remove(id){
      const n = items.length;
      items = items.filter(it => it.ev.id !== id);
      if (items.length !== n){ _save(items); _changed(); }
      return items.length !== n;
    },

    // Send everything waiting, oldest first, ONE at a time. Serialized because these are the user's posts
    // in the order they wrote them, and because a burst of publishes into a socket that has just come back
    // is the least likely moment for all of them to land. An entry leaves the queue only on a relay OK, or
    // when it has failed MAX_TRIES times (see below).
    //
    // Stopping at the FIRST failure is deliberate: the relay pool cannot tell "no answer because we went
    // offline again" from "this event was rejected" — a rejected OK is never settled, so both surface as
    // {ok:false, msg:'timeout'} after the publish timeout. Ploughing on would therefore burn that timeout
    // per remaining item on what is almost always simply "the network went away again".
    //
    // But that alone would let ONE permanently-rejected event (a blocked author, a relay policy) sit at the
    // head of the queue forever, silently blocking every good post behind it while the banner insists that
    // N items are "waiting to send". So an entry that has failed MAX_TRIES separate drains is treated as
    // poison and dropped. `dropped` comes back as the list of event IDS, not a count, and the caller is
    // expected to evict them from the local store as well: the event was saved optimistically at compose
    // time, so dropping it from the queue alone would leave it sitting in the timeline looking posted with
    // nothing left that will ever send it — the silent divergence this whole feature exists to avoid.
    async flush(){
      if (_flushing || !items.length) return { sent: 0, dropped: [] };
      if (!window.Relay || Relay.status !== 'ok') return { sent: 0, dropped: [] };
      _flushing = true;
      let sent = 0; const dropped = [];
      try{
        for (const it of items.slice()){
          if (!window.Relay || Relay.status !== 'ok') break;   // went away mid-drain → stop, keep the rest
          let r = null;
          try{ r = await Relay.publish(it.ev); }catch(_){ r = null; }
          if (r && r.ok){ this.remove(it.ev.id); sent++; continue; }
          it.tries = (it.tries||0) + 1;
          if (it.tries >= MAX_TRIES){ this.remove(it.ev.id); dropped.push(it.ev.id); }
          else _save(items);
          break;
        }
      } finally { _flushing = false; }
      return { sent, dropped };
    },
  };

  // Drain when connectivity plausibly returned. Both signals are needed: 'online' fires on a LAN that may
  // still not reach the relay, and the relay can recover without the browser ever reporting a transition.
  try{ window.addEventListener('online', ()=>{ setTimeout(()=>Outbox.flush(), 1200); }); }catch(_){}

  window.Outbox = Outbox;
})();
