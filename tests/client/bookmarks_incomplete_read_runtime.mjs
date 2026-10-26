/* A BOOKMARK READ NOBODY FINISHED IS NOT A BOOKMARK THAT IS GONE.
 *
 * renderBookmarks paints what the Store holds, then asks the relay for the rest. Relay.query resolves [] with
 * complete:false when no relay answered in time, and that used to be final: two bookmarks showed as one, with no
 * retry and nothing on screen to say a post was missing -- the gate's "flaky under load" bookmark test, failing in
 * deploy after deploy (only one of two bookmarks listed after a reload).
 *
 * This runs the SHIPPED renderBookmarks against relays that (1) miss the first read and answer the second, (2) never
 * answer -- then the person presses Try again and they do, (3) answer completely that the post does not exist.
 */
import { clientSource } from './client_source.mjs';
const app = clientSource();
const start = app.indexOf('  function _bookmarksFromStore(){');
const end = app.indexOf('  // ---------- minimal, SAFE markdown renderer', start);
if (start < 0 || end < 0) throw new Error('renderBookmarks moved');
const shipped = app.slice(start, end);

function world(answers, opts = {}){
  const store = new Map([['a1', {id:'a1', pubkey:'p1', created_at:2}]]);
  const remote = {id:'a2', pubkey:'p2', created_at:1};
  const feed = {
    innerHTML: '',
    querySelector(sel){ return sel === '.bm-missing' && this.innerHTML.includes('bm-missing')
      ? { remove: () => { this.innerHTML = this.innerHTML.replace(/<div class="empty bm-missing">[\s\S]*$/, ''); } } : null; },
    insertAdjacentHTML(_, html){ this.innerHTML += html; },
  };
  const retry = { disabled: false, onclick: null };
  const calls = [];
  const g = {
    BOOKMARKS: new Set(opts.unread ? [] : ['a1', 'a2']), VIEW: 'bookmarks', ME: { pubkey: 'me' }, GUEST: false,
    _bmReadFor: opts.unread ? '' : 'me', _bmFetch: null, fetchBookmarks(){ calls.push('fetchBookmarks'); },
    Store: { get: id => store.get(id) || null, saveEvent: e => store.set(e.id, e), query: () => [] },
    needProfile(){}, hydrate(){},
    noteHtml: ev => `<article data-id="${ev.id}"></article>`,
    $: sel => sel === '#feed' ? feed : ((sel === '#bm-missing-retry' || sel === '#bm-retry') && feed.innerHTML.includes(sel.slice(1)) ? retry : null),
    Relay: {
      async ready(){ return true; },
      async query(f){ calls.push(f[0].ids.slice()); const a = answers.shift() || 'silent';
        const out = a === 'answer' ? [remote] : [];
        out.complete = a !== 'silent'; return out; },
    },
  };
  const names = Object.keys(g);
  const render = new Function(...names, shipped + '\n;return renderBookmarks;')(...names.map(k => g[k]));
  return { render, feed, retry, calls, answers };
}
const shown = f => (f.innerHTML.match(/<article/g) || []).length;
const out = {};

{ // 1: the first read is missed (no relay answered), the second one lands
  const w = world(['silent', 'answer']); await w.render();
  out.missedThenAnswered = { shown: shown(w.feed), asked: w.calls.length, notice: w.feed.innerHTML.includes('bm-missing') };
}
{ // 2: nobody answers at all -- say so; Try again once the relay is back
  const w = world(['silent', 'silent']); await w.render();
  const before = { shown: shown(w.feed), notice: w.feed.innerHTML.includes('couldn’t be loaded'), button: w.feed.innerHTML.includes('bm-missing-retry') };
  w.answers.push('answer');
  if (w.retry.onclick) { w.retry.onclick(); await new Promise(r => setTimeout(r, 20)); }
  out.neverAnswered = { ...before, pressed: !!w.retry.onclick, afterRetry: shown(w.feed), noticeAfter: w.feed.innerHTML.includes('bm-missing') };
}
{ // 3: the relays answered completely: the post is not there -- asked once, and said
  const w = world(['empty']); await w.render();
  out.answeredEmpty = { shown: shown(w.feed), asked: w.calls.length, notice: w.feed.innerHTML.includes('1 bookmarked post') };
}
{ // 4: no relay has answered for the list and nothing is held: say so, and Try again asks again
  const w = world([], { unread: true }); await w.render();
  const shown = { unreachable: w.feed.innerHTML.includes('bm-unreachable'), button: w.feed.innerHTML.includes('id="bm-retry"') };
  const before = w.calls.filter(c => c === 'fetchBookmarks').length;
  if (w.retry.onclick) w.retry.onclick();
  await new Promise(r => setTimeout(r, 20));
  out.listNeverRead = { ...shown, pressed: !!w.retry.onclick, asked: w.calls.filter(c => c === 'fetchBookmarks').length - before };
}
process.stdout.write(JSON.stringify(out));
