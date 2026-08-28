'use strict';

/* Pure multi-output policy for PosterChanOS. Electron owns one renderer per active Sway output;
 * each renderer owns one workspace and must see only the native windows on that workspace. Keeping
 * this policy outside main.js makes hotplug/unplug and workspace selection testable without Sway. */
const quote = s => '"' + String(s).replace(/(["\\])/g, '\\$1') + '"';

function plan(outputs, workspaces){
  const active=(outputs||[]).filter(o=>o&&o.active&&o.name);
  if(!active.length) return [];
  const ws1=(workspaces||[]).find(w=>String(w&&w.name)==='1');
  const focused=active.find(o=>o.focused);
  const primary=active.find(o=>ws1&&o.name===ws1.output)||focused||active[0];
  const ordered=[primary].concat(active.filter(o=>o!==primary)
    .sort((a,b)=>((a.rect&&a.rect.x)||0)-((b.rect&&b.rect.x)||0)
               ||((a.rect&&a.rect.y)||0)-((b.rect&&b.rect.y)||0)));
  return ordered.map((o,i)=>({output:String(o.name),workspace:String(i+1),primary:i===0,
    rect:Object.assign({x:0,y:0,width:0,height:0},o.rect||{})}));
}

function windowsFor(rows, workspace){
  return (rows||[]).filter(w=>w && (w.stashed || String(w.workspace||'')===String(workspace)));
}

function placement(id, assignment){
  const n=Number(id);
  if(!Number.isFinite(n)||n<=0) throw new Error('invalid shell window');
  if(!assignment||!assignment.output||!assignment.workspace) throw new Error('invalid display assignment');
  return [
    'workspace number '+assignment.workspace,
    'move workspace to output '+quote(assignment.output),
    '[con_id='+n+'] move container to workspace number '+assignment.workspace,
    '[con_id='+n+'] fullscreen disable',
    '[con_id='+n+'] floating disable',
    '[con_id='+n+'] border none',
  ];
}

function needsPlacement(row, assignment){
  if(!row||!assignment) return true;
  const actual=row.rect||{}, wanted=assignment.rect||{};
  return String(row.workspace||'')!==String(assignment.workspace||'') || row.visible===false ||
    Number(actual.x)!==Number(wanted.x)||Number(actual.y)!==Number(wanted.y)||
    Number(actual.width)!==Number(wanted.width)||Number(actual.height)!==Number(wanted.height);
}

module.exports={plan,windowsFor,placement,needsPlacement};
