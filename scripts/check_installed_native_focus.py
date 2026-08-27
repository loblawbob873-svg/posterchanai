#!/usr/bin/env python3
"""Prove installed PosterChanOS can cover a real native application.

Run against an authenticated installed Electron renderer exposed on a loopback-only CDP port.
The machine must already have a harmless Firefox or Telegram window. No native-window pixels,
titles, profile data, or page contents leave the machine; this checks only compositor state.
"""

import asyncio
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_installed_desktop_account import BASE, CDP

SAFE = re.compile(r"(?:probe|disposable|test)", re.I)


def descendants(node, output=None):
    if node.get("type") == "output":
        output = node.get("name")
    yield node, output
    for key in ("nodes", "floating_nodes"):
        for child in node.get(key) or []:
            yield from descendants(child, output)


def resolve(tree, app_id, pid):
    if not app_id or not SAFE.search(app_id):
        raise ValueError("PC_NATIVE_APP_ID must identify a disposable probe/test app")
    hits = [(n, out) for n, out in descendants(tree)
            if str(n.get("app_id") or "") == app_id and int(n.get("pid") or -1) == pid]
    if len(hits) != 1:
        raise ValueError(f"refusing ambiguous target: {len(hits)} windows match app_id+pid")
    return hits[0]


def ppm_stats(data):
    pixels = data.split(b"\n", 3)[3]
    mean = sum(pixels) / len(pixels)
    variance = sum(x*x for x in pixels) / len(pixels) - mean*mean
    return mean, variance, sum(x < 24 for x in pixels) / len(pixels)


CLICK = r"""(selector=>{
  const e=document.querySelector(selector);if(!e)return false;const r=e.getBoundingClientRect();
  e.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:1,
    clientX:r.left+10,clientY:r.top+10}));
  e.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,button:0,pointerId:1}));
  e.click();return true;
})"""
# Generic overlap selector retained as an audit marker; runtime tags the exact created Terminal-like
# cover instead of risking an unrelated user window: `.osw:not(.osw-native) .osw-bar`.

SETUP = r"""(async wanted=>{
  if(!PCOS.isOn())PCOS.enter();
  const staleStart=document.querySelector('#os-start.on');if(staleStart)staleStart.click();
  await PCOS.refresh();await new Promise(r=>setTimeout(r,800));
  const snap=await pcWM.snapshot(),allowed=/firefox|telegram/i;
  const rows=(snap.windows||[]).filter(w=>allowed.test(String(w.app||'')));
  const row=wanted?rows.find(w=>Number(w.id)===Number(wanted)):(rows.length===1?rows[0]:null);
  if(!row)return {skip:true,why:wanted?'requested native window is not on this surface'
    :'more than one native window is visible; set PC_NATIVE_APP_ID'};
  const frame=document.querySelector('.osw-native[data-native="'+Number(row.id)+'"]');
  if(!frame)return {skip:true,why:'could not identify the requested native frame safely'};
  frame.dataset.pcCheckNative=String(Number(row.id));
  const created=!!PCOS.routeView('global');await new Promise(r=>setTimeout(r,500));
  const cover=document.querySelector('.osw:not(.osw-native).focused');
  if(cover)cover.dataset.pcCheckCover='1';
  return {id:Number(row.id),initiallyStashed:!!row.stashed,created,
    nativeFrames:document.querySelectorAll('.osw-native').length,
    htmlFrames:document.querySelectorAll('.osw:not(.osw-native)').length};
})"""

STATE = r"""(async id=>{
  const frame=document.querySelector('.osw-native[data-pc-check-native="'+Number(id)+'"]'),snap=await pcWM.snapshot(),row=(snap.windows||[])
    .find(w=>Number(w.id)===Number(id));
  const body=frame&&frame.querySelector('.osw-body'),r=body&&body.getBoundingClientRect();
  return {nativeFocused:!!(frame&&frame.classList.contains('focused')),
    nativeStashed:!!(frame&&frame.classList.contains('native-stashed')),
    compositorFocused:!!(row&&row.focused),compositorStashed:!!(row&&row.stashed),
    shellFocused:(snap.windows||[]).some(w=>/^(posterchan(-desktop)?|place\.poster\.desktop)$/i.test(String(w.app||''))&&w.focused),
    rect:r&&{x:r.x,y:r.y,width:r.width,height:r.height}};
})"""

CLEANUP = r"""(async info=>{
  const start=document.querySelector('#os-start.on');if(start)start.click();
  if(info.created){const close=document.querySelector('.osw:not(.osw-native).focused .osw-x');
    if(close)close.click();}
  if(info.initiallyStashed)await pcWM.hide(info.id);else await pcWM.focus(info.id);
  return true;
})"""


async def choose_native_page(wanted):
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    for page in pages:
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            usable = await cdp.eval(r"""(async wanted=>{
              if(!window.PCOS||!window.pcWM)return false;
              if(!PCOS.isOn())PCOS.enter();await new Promise(r=>setTimeout(r,700));
              const snap=await pcWM.snapshot();return wanted
                ?(snap.windows||[]).some(w=>Number(w.id)===Number(wanted))
                :document.querySelectorAll('.osw-native').length>0;
            })""" + "(" + json.dumps(wanted) + ")")
            if usable:
                return page
    raise RuntimeError("no installed PosterChan surface owns a native frame")


async def main():
    wanted = int(os.environ.get("PC_NATIVE_APP_ID") or 0)
    page = await choose_native_page(wanted)
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        info = await cdp.eval(SETUP + "(" + json.dumps(wanted) + ")")
        if info.get("skip"):
            print("SKIP installed focus test: " + info.get("why", "no safe native surface"))
            return 2
        assert info["nativeFrames"] > 0 and info["htmlFrames"] > 0, info
        try:
            selector = f'.osw-native[data-pc-check-native="{info["id"]}"] .osw-bar'
            assert await cdp.eval(CLICK + "(" + json.dumps(selector) + ")")
            await asyncio.sleep(1.2)
            native = await cdp.eval(STATE + f"({json.dumps(info['id'])})")
            assert native["nativeFocused"] and native["compositorFocused"], native
            assert not native["nativeStashed"] and not native["compositorStashed"], native
            evidence = os.environ.get("PC_NATIVE_EVIDENCE_DIR")
            if evidence:
                Path(evidence).mkdir(parents=True, exist_ok=True)
                before_shot = await cdp.call("Page.captureScreenshot", {"format": "png", "clip": {
                    **native["rect"], "scale": 1}})
                (Path(evidence) / "native-before.png").write_bytes(base64.b64decode(before_shot["data"]))

            # A normal Terminal overlap must leave native pixels live; only the transient Start
            # overlay intentionally parks them so its controls can receive input.
            assert await cdp.eval(CLICK + "('.osw[data-pc-check-cover=\"1\"] .osw-bar')")
            await asyncio.sleep(.4)
            overlapped = await cdp.eval(STATE + f"({json.dumps(info['id'])})")
            assert not overlapped["nativeStashed"] and not overlapped["compositorStashed"], overlapped
            assert await cdp.eval("!!document.querySelector('#os-start') && (document.querySelector('#os-start').click(),true)")
            # Wait for both authorities and for the exact HTML placeholder rectangle to settle.
            # A scratchpad container's Sway rect is not this rectangle and may point at black space.
            covered = None
            prior_rect = None
            stable = 0
            for _ in range(60):
                await asyncio.sleep(.1)
                covered = await cdp.eval(STATE + f"({json.dumps(info['id'])})")
                rect = covered.get("rect")
                ready = (covered["shellFocused"] and covered["nativeStashed"] and
                         covered["compositorStashed"] and rect and
                         rect["width"] > 80 and rect["height"] > 80)
                stable = stable + 1 if ready and rect == prior_rect else 0
                prior_rect = rect
                if stable >= 4:
                    break
            else:
                raise AssertionError({"placeholderNeverSettled": covered})
            assert covered["shellFocused"], covered
            # The HTML frame remains the selected window while its compositor surface is parked;
            # `focused` here is desktop selection, not proof that Firefox still owns the keyboard.
            assert not covered["compositorFocused"], covered
            assert covered["nativeStashed"] and covered["compositorStashed"], covered
            shot = await cdp.call("Page.captureScreenshot", {"format": "png", "clip": {
                **covered["rect"], "scale": 1}})
            image = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
            if evidence:
                (Path(evidence) / "native-parked.png").write_bytes(base64.b64decode(shot["data"]))
            stat = ImageStat.Stat(image)
            mean, variance = sum(stat.mean) / 3, sum(stat.var) / 3
            pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
            nearblack = sum(1 for px in pixels for c in px if c < 24) / (image.width * image.height * 3)
            assert mean >= 45 and variance >= 250 and nearblack <= .92, {
                "blackPlaceholder": {"mean": mean, "variance": variance, "nearblack": nearblack},
                "state": covered}
            # The placeholder itself is the restore target; click it and require exact native state.
            assert await cdp.eval(CLICK + "(" + json.dumps(selector.replace(" .osw-bar", " .osw-body")) + ")")
            # Synthetic CDP pointer events are not trusted, so the document's outside-click handler
            # may leave Start open even though a real pointer closes it. Detect that exact overlay,
            # dismiss it explicitly, then repeat the same placeholder click—never a blind screen click.
            await cdp.eval("(()=>{const b=document.querySelector('#os-start.on');if(b)b.click();return true})()")
            assert await cdp.eval(CLICK + "(" + json.dumps(selector.replace(" .osw-bar", " .osw-body")) + ")")
            for _ in range(40):
                await asyncio.sleep(.1)
                restored = await cdp.eval(STATE + f"({json.dumps(info['id'])})")
                if restored["nativeFocused"] and restored["compositorFocused"] and not restored["nativeStashed"]:
                    break
            else:
                raise AssertionError({"placeholderClickDidNotRestore": restored})
            if evidence:
                after_shot = await cdp.call("Page.captureScreenshot", {"format": "png", "clip": {
                    **restored["rect"], "scale": 1}})
                (Path(evidence) / "native-after.png").write_bytes(base64.b64decode(after_shot["data"]))
        finally:
            await cdp.eval(CLEANUP + f"({json.dumps(info)})")

    print(f"OK installed native app yields/restores; parked mean={mean:.2f} variance={variance:.2f} nearblack={nearblack:.4f}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port: " + str(exc))
        raise SystemExit(2)
