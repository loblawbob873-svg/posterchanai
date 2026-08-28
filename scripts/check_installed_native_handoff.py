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
  const shell=(snap.windows||[]).find(w=>Number(w.id)===Number(snap.shellId));
  const rows=(snap.windows||[]).filter(w=>/firefox|telegram/i.test(String(w.app||'')));
  const native=WANTED?rows.find(w=>Number(w.id)===Number(WANTED)):(rows.length===1?rows[0]:null);
  const frame=native&&document.querySelector('.osw-native[data-native="'+Number(native.id)+'"]');
  if(frame)frame.dataset.pcCheckNative=String(Number(native.id));
  const bar=frame&&frame.querySelector('.osw-bar'),body=frame&&frame.querySelector('.osw-body');
  const br=body&&body.getBoundingClientRect(),visual=window.visualViewport,
    scale=shell&&window.PCOSNative&&PCOSNative.scaleFrom(shell.rect,
      visual&&visual.width>0?visual.width:window.innerWidth,
      visual&&visual.height>0?visual.height:window.innerHeight);
  const mapped=br&&scale&&PCOSNative.mapRect({left:br.left,top:br.top,width:br.width,height:br.height},scale);
  const buttons=frame?[...frame.querySelectorAll('.osw-btns [data-w]')].map(b=>b.dataset.w):[];
  return {nativeFrames:frame?1:0,unsafe:rows.length&&!native,
    htmlFrames:document.querySelectorAll('.osw:not(.osw-native)').length,
    frameStashed:!!(frame&&frame.classList.contains('native-stashed')),
    framePrepared:!!(frame&&frame.classList.contains('native-handoff-prepared')),
    frameFocused:!!(frame&&frame.classList.contains('focused')),
    chrome:!!(bar&&bar.getClientRects().length&&buttons.includes('min')&&buttons.includes('max')&&buttons.includes('close')),
    border:!!(body&&parseFloat(getComputedStyle(body).borderTopWidth)>0),
    mapped:mapped&&{x:mapped.x,y:mapped.y,w:mapped.w,h:mapped.h},
    shell:shell&&{id:shell.id,workspace:String(shell.workspace||''),rect:shell.rect},
    native:native&&{id:native.id,workspace:String(native.workspace||''),rect:native.rect,
      stashed:!!native.stashed,focused:!!native.focused}};
})()"""

DRAG = r"""(async direction=>{
  const bar=document.querySelector('.osw-native[data-pc-check-native="'+Number(WANTED)+'"] .osw-bar');if(!bar)return false;
  const r=bar.getBoundingClientRect(),sx=r.left+r.width/2,sy=r.top+12,id=91,
    wx=Number(window.screenX)||0,wy=Number(window.screenY)||0;
  bar.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:id,pointerType:'mouse',
    button:0,buttons:1,clientX:sx,clientY:sy,screenX:wx+sx,screenY:wy+sy}));
  // Cross the edge by more than os.js's eight-pixel handoff threshold.  Keeping screen and client
  // coordinates in the same virtual-desktop space is what distinguishes this from an edge snap.
  const x=direction==='right'?innerWidth+16:-16,y=innerHeight/2;
  const move=()=>document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:id,
    pointerType:'mouse',button:-1,buttons:1,clientX:x,clientY:y,screenX:wx+x,screenY:wy+y}));
  move();await new Promise(r=>setTimeout(r,400));move();
  document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:id,pointerType:'mouse',
    button:0,buttons:0,clientX:x,clientY:y,screenX:wx+x,screenY:wy+y}));return true;
})"""


async def installed_pages():
    raw = json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
    pages = []
    for page in raw:
        if page.get("type") != "page" or not page.get("url", "").startswith("app://posterchan/"):
            continue
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            if await cdp.eval("!!(window.PCOS&&window.pcWM&&PCOS.isOn()&&pcWM.snapshot)"):
                pages.append(page)
    return pages


async def states(pages, wanted):
    result = []
    for page in pages:
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            result.append(await cdp.eval(STATE.replace("WANTED", json.dumps(wanted))))
    return result


async def drag(page, direction, wanted):
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        assert await cdp.eval(DRAG.replace("WANTED", json.dumps(wanted))
                              + "(" + json.dumps(direction) + ")")


def owner_index(rows):
    owners = [i for i, row in enumerate(rows) if row["nativeFrames"]]
    assert len(owners) == 1, rows
    return owners[0]


def assert_paired(row):
    assert row["chrome"] and row["border"], row
    assert not row["framePrepared"] and not row["frameStashed"], row
    assert row["frameFocused"] and row["native"]["focused"], row
    mapped, native = row["mapped"], row["native"]["rect"]
    # Sway reports the client rectangle while the managed HTML body reserves native decoration.
    # Position and width remain exact; the measured 7px title/deco height is bounded separately.
    assert mapped, row
    assert abs(mapped["x"] - native["x"]) <= 3, row
    assert abs(mapped["y"] - native["y"]) <= 3, row
    assert abs(mapped["w"] - native["width"]) <= 3, row
    assert abs(mapped["h"] - native["height"]) <= 12, row


async def main():
    wanted = int(os.environ.get("PC_NATIVE_APP_ID") or 0)
    if not wanted:
        print("SKIP installed native handoff requires PC_NATIVE_APP_ID for a disposable window")
        return 2
    pages = await installed_pages()
    if len(pages) < 2:
        print("SKIP installed native handoff requires two PosterChanOS renderer surfaces")
        return 2
    before = await states(pages, wanted)
    assert all(row.get("shell") for row in before), (
        "installed package lacks exact per-renderer shellId; update Desktop before handoff testing", before)
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
        await drag(pages[source], direction, wanted)
        await asyncio.sleep(3)
        moved = await states(pages, wanted)
        assert owner_index(moved) == destination, moved
        assert [row["htmlFrames"] for row in moved] == html_counts, moved
        assert not moved[destination]["frameStashed"], moved[destination]
        assert moved[destination]["native"] and not moved[destination]["native"]["stashed"], moved[destination]
        assert moved[destination]["native"]["workspace"] == moved[destination]["shell"]["workspace"], moved[destination]
        assert_paired(moved[destination])

        await drag(pages[destination], reverse, wanted)
        await asyncio.sleep(3)
        restored = await states(pages, wanted)
        assert owner_index(restored) == source, restored
        assert [row["htmlFrames"] for row in restored] == html_counts, restored
        assert restored[source]["native"]["id"] == native_id, restored[source]
        assert restored[source]["native"]["workspace"] == restored[source]["shell"]["workspace"], restored[source]
        assert_paired(restored[source])
    finally:
        current = await states(pages, wanted)
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
