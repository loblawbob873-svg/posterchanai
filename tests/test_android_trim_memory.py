"""Tablet stability: the APK keeps its renderer while visible and passes Android's memory warnings to the page.

"we need tablet to be stable". Measured (desktop mode, 4 windows, DPR 2): the renderer holds ~370-500 MB
private. MainActivity set no WebView memory policy at all, and ignored onTrimMemory -- so under pressure
Android's only move was killing the renderer, which the person sees as the app reloading. Now:
  * RENDERER_PRIORITY_IMPORTANT while visible (waived in the background);
  * onTrimMemory from RUNNING_LOW up dispatches `pc:trim-memory`, and the Store trims to its keep size.
TrimPolicy is javac-RUN; the client half is RUN under node against the shipped store.js eviction.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app")


def test_the_trim_policy_relieves_from_running_low_and_when_hidden(tmp_path):
    if shutil.which("javac") is None:
        pytest.skip("no JDK")
    drv = tmp_path / "Drv.java"
    drv.write_text("package place.poster.app;public class Drv{public static void main(String[] a){"
                   "for(int l:new int[]{5,10,15,20,40,60,80})System.out.print(TrimPolicy.relieve(l)?'1':'0');}}")
    c = subprocess.run(["javac", "-d", str(tmp_path), os.path.join(APP, "TrimPolicy.java"), str(drv)], capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    out = subprocess.run(["java", "-cp", str(tmp_path), "place.poster.app.Drv"], capture_output=True, text=True).stdout
    assert out == "0111111", out


def test_mainactivity_sets_the_renderer_priority_and_forwards_trim_memory():
    src = open(os.path.join(APP, "MainActivity.java")).read()
    assert "setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, true)" in src
    assert "keepRendererWhileVisible();" in src[src.index("void onCreate("):][:12000]
    body = src[src.index("public void onTrimMemory(int level)"):][:600]
    assert "TrimPolicy.relieve(level)" in body and "pc:trim-memory" in body


def test_the_page_trims_its_cache_when_told(tmp_path):
    """store.js under node: 4000 events sit below the 4500 cap, so nothing trims on its own; the
    `pc:trim-memory` event brings the in-memory cache to its keep size."""
    if shutil.which("node") is None:
        pytest.skip("no node")
    js = tmp_path / "run.js"
    js.write_text(r"""
const listeners={};
global.window={addEventListener:(t,f)=>{(listeners[t]=listeners[t]||[]).push(f);}, dispatchEvent:e=>{(listeners[e.type]||[]).forEach(f=>f(e));}};
global.indexedDB=undefined; global.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
global.CustomEvent=function(t,o){this.type=t;this.detail=(o||{}).detail;};
global.document={addEventListener:()=>{}, visibilityState:'visible'};
require(%r);
const S=window.Store;
for(let i=0;i<4000;i++) S.saveEvent({id:i.toString(16).padStart(64,'0'),kind:1,pubkey:'a'.repeat(64),created_at:1700000000+i,tags:[],content:'x',sig:''});
const before=S.all().length;
window.dispatchEvent(new CustomEvent('pc:trim-memory',{detail:{level:15}}));
console.log(JSON.stringify({before, after:S.all().length}));
""" % os.path.join(ROOT, "static", "js", "client", "store.js"))
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-1500:]
    got = __import__("json").loads(r.stdout.strip().splitlines()[-1])
    assert got["before"] == 4000, got
    assert got["after"] <= 3000, ("the page kept its whole cache after Android warned", got)
