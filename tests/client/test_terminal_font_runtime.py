"""Readable terminal defaults and durable user-controlled size, from shipped functions."""
import json
import subprocess

from tests.client.test_terminal_tab_chord_runtime import _function


def test_font_uses_visible_pane_and_saves_size_across_reload():
    code=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const saved=new Map();let width=1000,fits=0,focuses=0;
const make=()=>({window:{innerWidth:1280,innerHeight:900},document:{getElementById:()=>({getBoundingClientRect:()=>({width})})},
 localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},_fitPixels:'cached',_fit:()=>fits++, _focus:()=>focuses++,toast:()=>{}});
let ctx=make();vm.createContext(ctx);vm.runInContext(SOURCE,ctx);
assert.equal(ctx.fontSize(),14,'desktop text must not shrink with page zoom');
width=390;assert.equal(ctx.fontSize(),11,'narrow panes keep a usable grid');
width=1000;ctx._changeFont(1);assert.equal(ctx.fontSize(),15);assert.equal(ctx._fitPixels,'');assert.equal(fits,1);assert.equal(focuses,1);
ctx=make();vm.createContext(ctx);vm.runInContext(SOURCE,ctx);assert.equal(ctx.fontSize(),15,'saved size survives module reload');
for(let i=0;i<50;i++)ctx._changeFont(1);assert.equal(ctx.fontSize(),24);
for(let i=0;i<50;i++)ctx._changeFont(-1);assert.equal(ctx.fontSize(),10);
saved.set('pc_tty_font_size','NaN');assert.equal(ctx.fontSize(),14);
saved.set('pc_tty_font_size','999');assert.equal(ctx.fontSize(),14);
console.log('readable terminal font and persistence passed');
'''.replace('SOURCE',json.dumps(_function('fontSize')+'\n'+_function('_changeFont')))
    result=subprocess.run(['node','-e',code],text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
