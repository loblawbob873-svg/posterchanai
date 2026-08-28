import fs from 'node:fs';
import vm from 'node:vm';

const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const noop=()=>{};
const input={value:'hello relay',dataset:{ccDraftKey:'room:general'},selectionStart:11,selectionEnd:11,
  selectionDirection:'none',setSelectionRange(start,end,direction){this.selectionStart=start;this.selectionEnd=end;this.selectionDirection=direction;}};
let composerVisible=true;
const document={activeElement:input,body:{},documentElement:{appendChild:noop},head:{appendChild:noop},
  querySelector(selector){if(selector==='link[data-concord-css]')return {};if(selector==='#cc-input'&&composerVisible)return input;return null;},
  createElement:()=>({dataset:{}}),addEventListener:noop};
const window={document,addEventListener:noop,__PC:{$:selector=>selector==='#cc-input'&&composerVisible?input:null}};
vm.runInNewContext(src,{window,document,console,setTimeout:()=>0,clearTimeout:noop,URL,atob,crypto:{},
  localStorage:{getItem:()=>null,setItem:noop,removeItem:noop},sessionStorage:{getItem:()=>null,setItem:noop}});

const api=window.PCConcord;
const submitted=api.beginComposerSend('room:general');
if(submitted.value!=='hello relay')throw new Error('send rollback snapshot lost the submitted text');
if(input.value!=='')throw new Error('submitted text remained visible in the Concord composer');

// A repaint while relay I/O is pending must see the cleared DOM, never the submitted snapshot.
composerVisible=false;
composerVisible=true;
if(input.value!=='')throw new Error('a repaint restored submitted text while send was pending');

if(!api.restoreFailedComposer('room:general',submitted)||input.value!=='hello relay')
  throw new Error('relay failure did not restore the submitted draft');

// New typing wins over rollback if the earlier send fails later.
input.value='new thought';input.selectionStart=input.selectionEnd=input.value.length;
const newerSend=api.beginComposerSend('room:general');
input.value='do not overwrite me';input.selectionStart=input.selectionEnd=input.value.length;
if(api.restoreFailedComposer('room:general',newerSend)||input.value!=='do not overwrite me')
  throw new Error('failed send overwrote text entered while it was in flight');

console.log('Concord composer send transaction ok');
