import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const body=source.split('function retainCommunityRail',2)[1].split('/* THREADS.',1)[0];
const fn=vm.runInNewContext('(function retainCommunityRail'+body+')');
const server=(id,html,cls='cc-server')=>({dataset:{ccServer:id},innerHTML:html,className:cls,title:id});
const oldServers=[server('0','<img src="blob:kept">'),server('1','<span>B</span>')];
const newServers=[server('0','<img src="blob:kept">','cc-server active'),server('1','<span>B</span>')];
const oldRail={querySelectorAll:()=>oldServers};
let replacement=null;
const newRail={querySelectorAll:()=>newServers,replaceWith:value=>{replacement=value;}};
fn(oldRail,newRail);
if(replacement!==oldRail)throw new Error('the existing rail was not retained');
if(oldServers[0].innerHTML!=='<img src="blob:kept">')throw new Error('the decoded avatar node was rewritten');
if(oldServers[0].className!=='cc-server active')throw new Error('active navigation state was not patched');

// Metadata really changing must still replace the image, otherwise retaining the rail would pin
// an obsolete avatar forever after another client updates the community profile.
replacement=null;
const changedServers=[server('0','<img src="blob:new">','cc-server active'),server('1','<span>B</span>')];
fn(oldRail,{querySelectorAll:()=>changedServers,replaceWith:value=>{replacement=value;}});
if(replacement!==oldRail||oldServers[0].innerHTML!=='<img src="blob:new">')
  throw new Error('a genuine community avatar change was not painted');

// Never retain nodes by array position if membership sync inserted/reordered a community. That
// would briefly show one community's private icon and title on another community's button.
replacement=null;
const reordered=[server('1','<span>B</span>'),server('0','<img src="blob:new">')];
const replacementRail={querySelectorAll:()=>reordered,replaceWith:value=>{replacement=value;}};
fn(oldRail,replacementRail);
if(replacement!==null)throw new Error('a reordered community rail reused avatars by array position');
