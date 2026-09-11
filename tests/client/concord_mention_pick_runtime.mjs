/* RUN the Concord mention picker the way a THUMB runs it.
 *
 * Everything else about this picker can be read from the source; whether a tap tags anybody cannot.
 * The list is built from `mentionChoices`, its rows are rebuilt on every keystroke, and the choice
 * is committed from a delegated pointer pair — so the questions worth answering are "does a tap on
 * row N accept choice N" and "does a drag accept nothing", and both need events.
 *
 * The picker lives inside the composer's bind closure, so its two functions are lifted and given
 * the surroundings they close over.
 */
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');

function fn(head){
  const i = source.indexOf(head), begin = source.indexOf('{', i);
  if (i < 0 || begin < 0) throw Error('missing ' + head);
  let depth = 0;
  for (let p = begin; p < source.length; p++){
    if (source[p] === '{') depth++;
    else if (source[p] === '}' && --depth === 0) return source.slice(i, p + 1);
  }
  throw Error('unterminated ' + head);
}

/** The smallest DOM these two functions actually use, with real event dispatch. */
function el(tag){
  const node = {
    tag, className: '', children: [], parentElement: null, _html: '', _attrs: {}, _on: {},
    setAttribute(k, v){ node._attrs[k] = String(v); },
    getAttribute(k){ return node._attrs[k]; },
    get innerHTML(){ return node._html; },
    set innerHTML(v){ node._html = String(v); node.children = parseRows(node, String(v)); },
    addEventListener(type, fn){ (node._on[type] = node._on[type] || []).push(fn); },
    insertBefore(child, before){ child.parentElement = node;
      const at = node.children.indexOf(before);
      node.children.splice(at < 0 ? node.children.length : at, 0, child); return child; },
    remove(){ if(node.parentElement){ const a = node.parentElement.children.indexOf(node);
      if(a >= 0) node.parentElement.children.splice(a, 1); node.parentElement = null; } },
    querySelector(sel){ return node.children.find(c => matches(c, sel)) || null; },
    scrollIntoView(){ node._scrolled = true; },
    closest(sel){ let c = node; while(c){ if(matches(c, sel)) return c; c = c.parentElement; } return null; },
    dispatch(type, ev){ (node._on[type] || []).forEach(f => f(Object.assign({ target: node,
      preventDefault(){ ev.defaulted = true; } }, ev))); },
  };
  return node;
}

function matches(node, sel){
  if(sel.startsWith('[') && sel.endsWith(']')){
    const name = sel.slice(1, -1).replace(/=.*$/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return !!(node.dataset && node.dataset[name.replace(/^data/, '').replace(/^./, c => c.toLowerCase())]);
  }
  return sel.startsWith('.') ? String(node.className || '').split(/\s+/).includes(sel.slice(1)) : false;
}

/** Enough of an HTML parser for the rows this picker emits: one <button> per choice. */
function parseRows(parent, html){
  const rows = [];
  for(const m of html.matchAll(/<button[^>]*class="([^"]*)"[^>]*data-cc-mention="(\d+)"[^>]*>/g)){
    const row = el('button');
    row.className = m[1];
    row.dataset = { ccMention: m[2] };
    row.parentElement = parent;
    row._attrs['aria-selected'] = /aria-selected="true"/.test(m[0]) ? 'true' : 'false';
    rows.push(row);
  }
  return rows;
}

export function picker({ choices, index = 0 } = {}){
  const compose = el('div'); compose.className = 'cc-compose';
  const conversation = el('main'); conversation.className = 'cc-conversation';
  conversation.children.push(compose); compose.parentElement = conversation;

  const accepted = [];
  const ctx = {
    Number, Math, String, Object,
    document: {
      querySelector: (sel) => (sel === '.cc-compose' ? compose
                             : sel === '.cc-mentions' ? (conversation.querySelector('.cc-mentions') || null)
                             : null),
      createElement: el,
    },
    p: { enc: (v) => String(v == null ? '' : v).replace(/[&<>"]/g, (c) =>
           ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
         profOf: () => ({ picture: 'pic.png' }), LOGO: 'logo.png' },
    mentionChoices: choices,
    mentionIndex: index,
    acceptMention: (i) => { accepted.push(i); return true; },
    syncMentionState: () => {},
  };
  vm.createContext(ctx);
  vm.runInContext([
    'var mentionChoices = globalThis.mentionChoices, mentionIndex = globalThis.mentionIndex;',
    fn('function paintMentionPicker('),
    'globalThis.run = { paint: paintMentionPicker, set: (c, i) => { mentionChoices = c; mentionIndex = i; } };',
  ].join('\n'), ctx);

  const api = {
    paint(){ ctx.run.paint(); return conversation.querySelector('.cc-mentions'); },
    set(c, i){ ctx.run.set(c, i); },
    list: () => conversation.querySelector('.cc-mentions'),
    accepted,
    /* A thumb: down, (maybe) a slide, up. */
    tap(row, slide = 0){
      const list = conversation.querySelector('.cc-mentions');
      const held = { defaulted: false };
      list.dispatch('pointerdown', { target: row, clientX: 100, clientY: 200, ...held });
      if(slide) list.dispatch('pointermove', { target: row, clientX: 100, clientY: 200 + slide });
      list.dispatch('pointerup', { target: row, clientX: 100, clientY: 200 + slide });
    },
  };
  return api;
}
