/* Concord renders `inline code` and ``` fenced blocks ``` (2026-10-10: "concord is not rendering console text",
 * a member posting shell output in backticks saw the raw ticks). Drives the SHIPPED messageContentHtml. */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const noop=()=>{}, store=new Map();
const localStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
const document={querySelector:()=>null,createElement:()=>({dataset:{}}),querySelectorAll:()=>[],addEventListener:noop,
  body:{classList:{contains:()=>false}},head:{appendChild:noop}};
const window={document,addEventListener:noop};
vm.runInNewContext(src,{window,document,localStorage,sessionStorage:localStorage,setTimeout:()=>0,clearTimeout:noop,URL,atob});
const enc=s=>String(s).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;').replaceAll('>','&gt;');
// The real linkify escapes and turns URLs into links; this stand-in does the same two things.
const linkify=s=>enc(s).replace(/https?:\/\/[^\s<]+/g,u=>`<a href="${u}">${u}</a>`);
let carded='';
const p={enc,linkify,viewer:()=>({}),linkCardHtml:s=>{carded=s;return '';}};
const render=text=>window.PCConcord.messageContentHtml(p,{id:'m',text,tags:[]},{},'general');

// plain prose is byte-identical to before
assert.equal(render('hello https://a.test/x'),'<p>hello <a href="https://a.test/x">https://a.test/x</a></p>');

// inline
let h=render('run `ls -la /tmp` now');
assert.match(h,/<code class="cc-code">ls -la \/tmp<\/code>/,'inline code is not rendered: '+h);
assert(!h.includes('`'),'backticks still visible: '+h);

// fenced block, with a language, HTML and a URL inside
h=render('output:\n```bash\n$ curl https://x.test/a\n<b>bold?</b>\n  indented\n```\ndone');
assert.match(h,/<pre class="cc-codeblock"><code>\$ curl https:\/\/x\.test\/a\n&lt;b&gt;bold\?&lt;\/b&gt;\n  indented<\/code><\/pre>/,'fenced block wrong: '+h);
assert(!/<a href="https:\/\/x\.test/.test(h),'a URL inside code was linkified');
assert(!/<p>[^]*<pre/.test(h.replace(/<\/p>/g,'</p>\u0000').split('\u0000').find(x=>x.includes('<pre'))||''),'a <pre> must not sit inside a <p>');
assert(h.includes('output:')&&h.includes('done'),'prose around the block was lost');
assert(!carded.includes('x.test'),'a URL inside code produced a link preview');

// a fence with no language, and a one-line ```x``` (inline, per CommonMark)
h=render('```\nmake install\n```');
assert.match(h,/<pre class="cc-codeblock"><code>make install<\/code><\/pre>/,h);
h=render('try ```npm ci``` first');
assert.match(h,/<code class="cc-code">npm ci<\/code>/,h);

// an unclosed fence is shown as typed, never swallowing the rest
h=render('```\nhalf a block');
assert(h.includes('half a block')&&!h.includes('<pre'),h);

// code is escaped (no markup injection through a code span)
h=render('`<img src=x onerror=alert(1)>`');
assert(!h.includes('<img'),h);
console.log('ok concord code');
