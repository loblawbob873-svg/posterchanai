#!/usr/bin/env python3
"""Drag a real native app across installed PosterChanOS renderers and back.

The check uses title-bar PointerEvents, not pcWM.handoff directly. It inspects only application ids,
rectangles and stash state; no native pixels, titles, browsing data or messages leave the machine.
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP


STATE = r"""(async()=>{
  await PCOS.refresh();await new Promise(r=>setTimeout(r,400));const snap=await pcWM.snapshot();
  const shell=(snap.windows||[]).find(w=>/^posterchan(-desktop)?$/i.test(String(w.app||'')));
  const native=(snap.windows||[]).find(w=>/firefox|telegram/i.test(String(w.app||'')));
  return {nativeFrames:document.querySelectorAll('.osw-native').length,
    htmlFrames:document.querySelectorAll('.osw:not(.osw-native)').length,
    frameStashed:!!document.querySelector('.osw-native.native-stashed'),
    shell:shell&&{id:shell.id,rect:shell.rect},
    native:native&&{id:native.id,rect:native.rect,stashed:!!native.stashed}};
})()"""

DRAG = r"""(async direction=>{
  const bar=document.querySelector('.osw-native .osw-bar');if(!bar)return false;
  const r=bar.getBoundingClientRect(),sx=r.left+r.width/2,sy=r.top+12,id=91;
  bar.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:id,pointerType:'mouse',
    button:0,buttons:1,clientX:sx,clientY:sy,screenX:sx,screenY:sy}));
  const x=direction==='right'?innerWidth-1:1,y=innerHeight/2;
  const move=()=>document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:id,
    pointerType:'mouse',button:-1,buttons:1,clientX:x,clientY:y,screenX:x,screenY:y}));
  move();await new Promise(r=>setTimeout(r,400));move();
  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:id,pointerType:'mouse',
    button:0,buttons:0,clientX:x,clientY:y,screenX:x,screenY:y}));return true;
})"""


async def installed_pages():
    raw = json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
    pages = []
    for page in raw:
        if page.get("type") != "page" or not page.get("url", "").startswith("app://posterchan/"):
            continue
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            if await cdp.eval("!!(window.__PC&&__PC.me&&__PC.me()&&window.PCOS&&window.pcWM)"):
                pages.append(page)
    return pages


async def states(pages):
    result = []
    for page in pages:
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            result.append(await cdp.eval(STATE))
    return result


async def drag(page, direction):
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        assert await cdp.eval(DRAG + "(" + json.dumps(direction) + ")")


def owner_index(rows):
    owners = [i for i, row in enumerate(rows) if row["nativeFrames"]]
    assert len(owners) == 1, rows
    return owners[0]


async def main():
    pages = await installed_pages()
    if len(pages) < 2:
        print("SKIP installed native handoff requires two PosterChanOS renderer surfaces")
        return 2
    before = await states(pages)
    source = owner_index(before)
    native_id = before[source]["native"]["id"]
    initially_stashed = before[source]["native"]["stashed"]
    html_counts = [row["htmlFrames"] for row in before]
    shells = [(i, row["shell"]["rect"]["x"]) for i, row in enumerate(before) if row["shell"]]
    assert len(shells) >= 2, before
    destination = max(shells, key=lambda item: item[1])[0] if source == min(shells, key=lambda item: item[1])[0] else min(shells, key=lambda item: item[1])[0]
    direction = "right" if before[destination]["shell"]["rect"]["x"] > before[source]["shell"]["rect"]["x"] else "left"
    reverse = "left" if direction == "right" else "right"
    try:
        await drag(pages[source], direction)
        await asyncio.sleep(3)
        moved = await states(pages)
        assert owner_index(moved) == destination, moved
        assert [row["htmlFrames"] for row in moved] == html_counts, moved
        assert not moved[destination]["frameStashed"], moved[destination]
        assert moved[destination]["native"] and not moved[destination]["native"]["stashed"], moved[destination]
        target_rect = moved[destination]["shell"]["rect"]
        native_rect = moved[destination]["native"]["rect"]
        assert target_rect["x"] <= native_rect["x"] < target_rect["x"] + target_rect["width"], moved[destination]

        await drag(pages[destination], reverse)
        await asyncio.sleep(3)
        restored = await states(pages)
        assert owner_index(restored) == source, restored
        assert [row["htmlFrames"] for row in restored] == html_counts, restored
        assert restored[source]["native"]["id"] == native_id, restored[source]
        source_rect = restored[source]["shell"]["rect"]
        native_rect = restored[source]["native"]["rect"]
        assert source_rect["x"] <= native_rect["x"] < source_rect["x"] + source_rect["width"], restored[source]
    finally:
        current = await states(pages)
        owner = owner_index(current)
        async with CDP(pages[owner]["webSocketDebuggerUrl"]) as cdp:
            if initially_stashed:
                await cdp.eval(f"pcWM.hide({int(native_id)})")
            else:
                await cdp.eval(f"pcWM.focus({int(native_id)})")

    print("OK installed native title-bar handoff moved across renderers and returned")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port: " + str(exc))
        raise SystemExit(2)
