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
