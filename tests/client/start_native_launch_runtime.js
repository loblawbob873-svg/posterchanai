'use strict';
/* Execute the scanned-desktop-entry launcher, not a reimplementation of it. */
const path=require('path');
const launched=[],focused=[];
const entries=[
  {id:'firefox-bin',name:'Firefox',match:'firefox',argv:['/usr/bin/firefox-bin']},
  {id:'steam',name:'Steam',match:'steam',argv:['/usr/bin/steam']},
  {id:'libreoffice-writer',name:'LibreOffice Writer',match:'libreoffice',argv:['/usr/bin/libreoffice','--writer']},
  {id:'org.example.Generic',name:'Generic App',match:'generic',argv:['/opt/generic/bin/app','--safe']},
];
global.pcApps={list:async()=>({apps:entries})};
let windows=[];
global.pcWM={
  windows:async()=>windows,
  launch:async(argv)=>{launched.push(argv);return {pid:100+launched.length,window:{id:200+launched.length}};},
  focus:async id=>{focused.push(id);return true;},
};
const S=require(path.resolve(__dirname,'../../static/js/client/osshell.js'));
function ok(name,value){if(!value)throw new Error(name);console.log('  ok   '+name);}
(async()=>{
  await S.detect();
  const listed=await S.machineApps(true);
  ok('all scanned entries are retained',listed.length===entries.length);
  for(const entry of entries){
    const before=launched.length;
    const result=await S.launch('app:'+entry.id);
    ok(entry.name+' launches exactly once',launched.length===before+1);
    ok(entry.name+' preserves desktop Exec argv',JSON.stringify(launched.at(-1))===JSON.stringify(entry.argv));
    ok(entry.name+' reports its mapped window',!!result.window);
  }
  windows=[{id:77,app:'firefox',title:'Firefox'}];
  const before=launched.length;
  const reused=await S.launch('app:firefox-bin');
  ok('existing Firefox is focused instead of duplicated',launched.length===before&&focused.at(-1)===77&&reused.focused===77);
  console.log('OK installed Start programs launch');
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
