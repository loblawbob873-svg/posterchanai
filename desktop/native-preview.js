/* One-shot, memory-only native window preview for a stashed Wayland surface. */
'use strict';
const { execFile } = require('child_process');

const GRIM = process.env.PC_GRIM || 'grim';

function geometry(rect){
  const x=Math.round(Number(rect&&rect.x)),y=Math.round(Number(rect&&rect.y)),
    w=Math.round(Number(rect&&rect.width)),h=Math.round(Number(rect&&rect.height));
  if(![x,y,w,h].every(Number.isFinite)||w<1||h<1||w>8192||h>8192||w*h>24000000)return '';
  return `${x},${y} ${w}x${h}`;
}

function capture(rect, runner=execFile){
  const area=geometry(rect);if(!area)return Promise.resolve('');
  return new Promise(resolve=>{
    runner(GRIM,['-g',area,'-'],{encoding:'buffer',timeout:1500,maxBuffer:16*1024*1024},
      (err,stdout)=>{
        const b=Buffer.isBuffer(stdout)?stdout:Buffer.from(stdout||'');
        const png=!err&&b.length>=8&&b.length<=16*1024*1024&&
          b.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
        resolve(png?'data:image/png;base64,'+b.toString('base64'):'');
      });
    // A static wlroots output may not damage another frame after grim subscribes.  Wake the cursor
    // plane without changing its coordinates so the preview cannot time out as a black placeholder.
    if(runner===execFile&&process.platform==='linux'&&process.env.SWAYSOCK){
      setTimeout(()=>execFile('swaymsg',['-q','seat seat0 cursor move 0 0'],{timeout:1000},()=>{}),50).unref();
    }
  });
}

module.exports={geometry,capture};
