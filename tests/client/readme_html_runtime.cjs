'use strict';
/* A README's HTML renders — safely. Runs the SHIPPED mdToHtml (app.js) under node.
 * Reported: "on the ngit posterchan repo, it's showing HTML instead of rendering it". */
const fs=require('fs'),path=require('path');
const acorn=require('/opt/flood/node_modules/acorn');
const src=fs.readFileSync(path.join(__dirname,'..','..','static','js','client','app.js'),'utf8');
const ast=acorn.parse(src,{ecmaVersion:'latest',sourceType:'script',allowReturnOutsideFunction:true});
const fns={},consts={};
(function walk(n){ if(!n||typeof n.type!=='string')return;
  if(n.type==='FunctionDeclaration'&&n.id&&!fns[n.id.name])fns[n.id.name]=src.slice(n.start,n.end);
  if(n.type==='VariableDeclarator'&&n.id&&n.id.name&&/^_md/.test(n.id.name)&&!consts[n.id.name])consts[n.id.name]='const '+src.slice(n.start,n.end)+';';
  for(const k in n){const v=n[k];if(Array.isArray(v))v.forEach(walk);else if(v&&typeof v.type==='string')walk(v);} })(ast);
const need=['mdToHtml','mdInline','_mdUrl','_mdRelPath','_mdCells'];
for(const n of need) if(!fns[n]) throw new Error('app.js has no '+n);
const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const body=Object.values(consts).join('\n')+'\n'+need.map(n=>fns[n]).join('\n')+'\nreturn mdToHtml;';
const mdToHtml=new Function('enc',body)(enc);
function ok(n,v){ if(!v) throw new Error(n); console.log('  ok   '+n); }
const readme=fs.readFileSync(path.join(__dirname,'..','..','README.md'),'utf8');
const top=readme.split('\n').slice(0,20).join('\n');
const out=mdToHtml(top);
ok('no <a> or <img> tag is shown as text', !/&lt;\/?(a|img)\b/i.test(out));
ok('the links render', /<a href="https:\/\/poster\.place" target="_blank" rel="noopener">/.test(out));
ok('relative images are kept for the repo to resolve', /<img class="md-rel" data-rel="static\/mascot\/mascot-happy-front-05\.png"/.test(out));
ok('a width survives as a size', /data-rel="static\/mascot[^>]*style="width:200px"/.test(out));
// The whole opening block, laid out the way GitHub shows it ("not displaying images like github, the
// alignment … still showing html in readme: <b>Desktop</b>").
const head=mdToHtml(readme.split('\n').slice(0,45).join('\n'));
ok('no layout tag is shown as text', !/&lt;\/?(b|sub|div|table|tr|td|center|p)\b/i.test(head));
ok('the block is centered', /<div style="text-align:center">/.test(head));
ok('the screenshots sit in a two-column table', (head.match(/<td style="text-align:center;width:50%">/g)||[]).length===2 && /<table class="md-html-table">/.test(head));
ok('the captions keep their bold', /<sub><b>Desktop<\/b> — PosterChanOS/.test(head));
ok('a table is never wrapped in a paragraph', !/<p>\s*<table/.test(head) && !/<table[^>]*>(?:(?!<\/table>)[\s\S])*<br>\s*<tr/.test(head));
ok('the heading inside the centered block still renders', /<h2>Poster-chan AI<\/h2>/.test(head));
const whole=mdToHtml(readme);
ok('nothing in the whole README shows an allowed tag as text', !/&lt;\/?(b|sub|sup|div|table|tr|td|th|center|details|summary|kbd)\b/i.test(whole));
const evil=mdToHtml([
  '<a href="javascript:alert(1)">x</a>',
  '<img src="https://e.example/a.png" onerror="alert(1)" style="position:fixed">',
  '<img src="x.png" onload="alert(2)">',
  '<img src="//evil.example/x.png">','<img src="../../etc/passwd">',
  '<script>alert(3)</script>','<iframe src="https://e.example"></iframe>',
  '<img src="https://e.example/b.png" alt="&quot;><script>alert(4)</script>">',
  '<div align="center" onclick="alert(5)" style="position:fixed" class="x">c</div>',
  '<td width="expression(alert(6))" align="javascript:x" colspan="999" onmouseover="alert(7)">t</td>',
  '<details open ontoggle="alert(8)"><summary>s</summary>d</details>',
  '<center><b onclick="alert(9)">z</b></center>',
].join('\n\n'));
ok('a rebuilt tag keeps only align/width/span/open', /<div style="text-align:center">c<\/div>/.test(evil) && /<td>t<\/td>/.test(evil)
   && /<details open>/.test(evil) && /<div style="text-align:center"><b>z<\/b><\/div>/.test(evil));
ok('no javascript: link', !/href="javascript:/i.test(evil));
ok('no event handler survives', !/\son[a-z]+=/i.test(evil));
ok('no style from the source survives', !/position:fixed/.test(evil));
ok('no script or iframe element', !/<(script|iframe)\b/i.test(evil));
ok('a protocol-relative or climbing path is not an image', !/data-rel="(\/\/|\.\.)/.test(evil));
ok('an alt cannot break out of its attribute', !/<script>alert\(4\)/.test(evil));
console.log('OK readme html');
