#!/usr/bin/env python3
"""Exercise the real PosterChanOS native-window edge preview and snap."""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP


async def main():
    wanted = int(os.environ.get("PC_NATIVE_APP_ID") or 0)
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    source = None
    original = None
    native_id = None
    initially_stashed = False
    for page in pages:
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            state = await cdp.eval(r"""(async wanted=>{if(!window.PCOS||!window.pcWM)return null;
              if(!PCOS.isOn())PCOS.enter();await PCOS.refresh();await new Promise(r=>setTimeout(r,700));
              const snap=await pcWM.snapshot(),rows=(snap.windows||[])
                .filter(w=>/firefox|telegram/i.test(String(w.app||'')));
              const row=wanted?rows.find(w=>Number(w.id)===Number(wanted)):(rows.length===1?rows[0]:null);
              if(!row)return {unsafe:rows.length};
              const frame=document.querySelector('.osw-native[data-native="'+Number(row.id)+'"]');
              if(!frame)return {unsafe:rows.length};frame.dataset.pcCheckNative=String(Number(row.id));
              return {frames:document.querySelectorAll('.osw-native').length,row};})"""
                + "(" + json.dumps(wanted) + ")")
            if state and state.get("frames"):
                source, native_id = page, int(state["row"]["id"])
                original = state["row"]["rect"]
                initially_stashed = bool(state["row"].get("stashed"))
                break
    if not source:
        print("SKIP no Firefox or Telegram frame is available for installed snap testing")
        return 2

    async with CDP(source["webSocketDebuggerUrl"]) as cdp:
        try:
            preview = await cdp.eval(r"""(id=>{const b=document.querySelector('.osw-native[data-pc-check-native="'+Number(id)+'"] .osw-bar'),
              r=b.getBoundingClientRect(),sx=r.left+r.width/2,sy=r.top+10,pid=92;
              b.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:pid,
                pointerType:'mouse',button:0,buttons:1,clientX:sx,clientY:sy,screenX:sx,screenY:sy}));
              const x=innerWidth-1,y=innerHeight/2;
              document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:pid,
                pointerType:'mouse',button:-1,buttons:1,clientX:x,clientY:y,screenX:x,screenY:y}));
              const ghost=document.querySelector('.os-ghost');
              return !!ghost&&getComputedStyle(ghost).display==='block';})""" + "(" + str(native_id) + ")")
            assert preview, "native edge drag displayed no snap preview"
            await cdp.eval(r"""(()=>{const x=innerWidth-1,y=innerHeight/2;
              document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:92,
                pointerType:'mouse',button:0,buttons:0,clientX:x,clientY:y,screenX:x,screenY:y}));
              return true;})()""")
            await asyncio.sleep(2)
            snapped = await cdp.eval(r"""(async id=>{const frame=document.querySelector('.osw-native[data-pc-check-native="'+Number(id)+'"]'),
              fr=frame.getBoundingClientRect(),snap=await pcWM.snapshot(),
              shell=(snap.windows||[]).find(w=>/^(posterchan(-desktop)?|place\.poster\.desktop)$/i.test(String(w.app||''))),
              row=(snap.windows||[]).find(w=>Number(w.id)===Number(id));
              return {frame:{x:fr.x,y:fr.y,w:fr.width,h:fr.height},
                snapped:frame.classList.contains('snapped'),
                frameStashed:frame.classList.contains('native-stashed'),shell:shell&&shell.rect,
                native:row&&{rect:row.rect,stashed:!!row.stashed}};})(""" + str(native_id) + ")")
            assert snapped["snapped"] and not snapped["frameStashed"], snapped
            assert snapped["native"] and not snapped["native"]["stashed"], snapped
            shell, native = snapped["shell"], snapped["native"]["rect"]
            assert shell["width"] * .4 < native["width"] < shell["width"] * .6, snapped
            assert native["x"] >= shell["x"] + shell["width"] / 2 - 80, snapped
            assert abs(native["width"] - snapped["frame"]["w"]) < 40, snapped
            # A right-edge drop is a half-screen tile, not merely a horizontal move. Check both
            # authorities: the HTML frame must span the usable output vertically and Firefox /
            # Telegram's compositor surface must fill that frame's body. Small differences are
            # the PosterChan frame border/title bar, never a retained floating-window height.
            usable_height = shell["height"] - 72
            assert abs(snapped["frame"]["y"]) <= 16, snapped
            assert abs(snapped["frame"]["h"] - usable_height) <= 32, snapped
            assert abs(native["y"] - snapped["frame"]["y"]) <= 80, snapped
            assert native["height"] >= snapped["frame"]["h"] - 100, snapped
            assert native["height"] <= snapped["frame"]["h"] + 24, snapped
        finally:
            await cdp.eval(f"(async()=>{{await pcWM.restore({native_id},{int(original['x'])},"
                           f"{int(original['y'])},{int(original['width'])},{int(original['height'])});"
                           + (f"await pcWM.hide({native_id});" if initially_stashed else
                              f"await pcWM.focus({native_id});") + "return true})()")

    print("OK installed native edge preview and snap follow the PosterChan frame")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port: " + str(exc))
        raise SystemExit(2)
