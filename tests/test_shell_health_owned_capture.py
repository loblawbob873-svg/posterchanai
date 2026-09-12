"""Actual Node capture responder + Python health reader, with only pixel/IPC boundaries synthetic."""
import json
import os
from pathlib import Path
import pathlib
import re
import subprocess
import types
import struct
import zlib
import pytest
from tests.test_wayfire_shell_health import _png, MARKER, SECONDARY_MARKER

ROOT=Path(__file__).resolve().parents[1]


def health_module():
    module=types.ModuleType('health_fixture')
    exec(compile((ROOT/'os/bin/pc-wayfire-health').read_text(),'pc-wayfire-health','exec'),module.__dict__)
    return module


@pytest.fixture
def responder(tmp_path):
    image=tmp_path/'renderer.png';_png(image)
    program=r'''
const fs=require('fs');const {armShellHealthCapture}=require(process.argv[1]);
const runtime=process.argv[2],image=process.argv[3];
const target={isDestroyed:()=>false,webContents:{capturePage:async rect=>{
 if(JSON.stringify(rect)!==JSON.stringify({x:0,y:0,width:96,height:96}))throw Error('unexpected capture');
 fs.appendFileSync(runtime+'/captures.log','capture\n');return{toPNG:()=>fs.readFileSync(image)};
}}};
const stop=armShellHealthCapture(target,{runtime,viewId:()=>23,interval:10});
console.log(process.pid);
const bye=()=>{try{stop();}finally{clearInterval(keep);process.exit(0);}};
const keep=setInterval(()=>{},1000);process.on('SIGTERM',bye);process.on('SIGINT',bye);
/* THIS CHILD MUST NOT OUTLIVE THE RUN THAT STARTED IT.
   The fixture terminates it after the yield, which covers a passing or failing test and NOT a
   pytest process that is itself killed — a suite timeout, an OOM kill, an operator stopping a
   gate. `setInterval` then keeps this alive for ever with nobody left to signal it. Measured on
   this server: eight of these were still running after TWENTY-SIX HOURS, holding 107MB, and the
   /tmp trees they pin are tmpfs, i.e. RAM. That is what eventually killed a 45-minute test run
   with "system is running low on memory".
   Two independent exits, because the failure is precisely that the parent cannot act: stdin EOFs
   when the parent goes away whatever killed it, and the deadline bounds the process even if stdin
   was never a pipe. The test needs a couple of seconds; sixty is pure headroom. */
process.stdin.on('end',bye);process.stdin.on('error',bye);try{process.stdin.resume();}catch(_){}
setTimeout(bye,60000);
'''
    # THE HELPER'S STDOUT IS DATA, SO IT MUST NOT BE DECORATED. node colourises console.log when
    # FORCE_COLOR is set in the environment — a terminal, a CI runner or an agent harness may set
    # it — and this test then reads "\x1b[33m4104269\x1b[39m" where it expects a pid, and a
    # decorated path where it expects a filename. Disable it for the child rather than stripping
    # escapes at each parse: there is one source and several readers.
    env={**os.environ,'NO_COLOR':'1','FORCE_COLOR':'0'}
    # stdin is a PIPE so the child can notice this process going away (see the program above).
    process=subprocess.Popen(['node','-e',program,str(ROOT/'desktop/shell-health-capture.js'),str(tmp_path),str(image)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,env=env)
    # DIGITS, NOT THE WHOLE LINE. node colourises `console.log(process.pid)` when FORCE_COLOR is
    # set in the environment — which a terminal, a CI runner or an agent harness may well do — and
    # the helper then prints "\x1b[33m4104269\x1b[39m". Parsing the raw line made this test fail
    # with `invalid literal for int()` about code that is entirely fine.
    pid=int(re.search(r"\d+", process.stdout.readline()).group(0))
    try:
        yield pid,image,tmp_path
    finally:
        # In a `finally`, so a test that raises still reaps its child. (The child now also exits on
        # its own when this process dies, which is the case a `finally` cannot cover.)
        try:
            process.terminate(); process.wait(timeout=5)
        except Exception:
            process.kill(); process.wait(timeout=5)
    assert not (tmp_path/f'posterchan-shell-health-{pid}').exists(), 'private health artifacts leaked'


def test_covered_output_uses_fresh_owned_png_without_focus_or_minimize(responder,monkeypatch):
    pid,image,runtime=responder;health=health_module();monkeypatch.setenv('XDG_RUNTIME_DIR',str(runtime))
    calls=[]
    def request(method,data=None):
        calls.append(method)
        assert method=='list-methods', 'health must not manipulate windows'
        return {'methods':[]}
    health.request=request
    black=runtime/'output.png';_png(black,marker=False)
    def grim(args,**kwargs):
        Path(args[-1]).write_bytes(black.read_bytes());return types.SimpleNamespace(returncode=0)
    health.subprocess=types.SimpleNamespace(run=grim,TimeoutExpired=subprocess.TimeoutExpired,DEVNULL=subprocess.DEVNULL,PIPE=subprocess.PIPE)
    output={'id':1,'name':'DP-1'};shell={'id':23,'pid':pid,'output-id':1}
    assert health.rendered([output],[shell]), 'another app covering output must not fail a painted renderer'
    assert (runtime/'captures.log').read_text().count('capture')==1
    _png(image,marker=False)
    assert not health.rendered([output],[shell]), 'fresh black renderer must fail, not reuse old valid screenshot'
    assert (runtime/'captures.log').read_text().count('capture')==2
    assert not list((runtime/f'posterchan-shell-health-{pid}').iterdir()), 'helper must clean request/response'


def test_stale_and_wrong_surface_captures_cannot_prove_health(responder,monkeypatch):
    pid,image,runtime=responder;health=health_module();monkeypatch.setenv('XDG_RUNTIME_DIR',str(runtime))
    health.request=lambda *args:{'methods':[]}
    directory=runtime/f'posterchan-shell-health-{pid}'
    stale=directory/('24-'+'0'*32+'.png');stale.write_bytes(image.read_bytes());stale.chmod(0o600)
    target=runtime/'result.png'
    try:
        assert not health.owned_capture({'id':24,'pid':pid},str(target))
        assert not health.owned_capture({'id':23,'pid':pid+1},str(target))
        assert not target.exists()
    finally:stale.unlink()


def test_existing_viewshot_is_used_without_hotloading_or_renderer_files(tmp_path):
    health=health_module();image=tmp_path/'view.png';_png(image);calls=[]
    def request(method,data=None):
        calls.append(method)
        if method=='list-methods':return {'methods':['view-shot/capture']}
        assert method=='view-shot/capture'
        assert data['view-id']==23
        Path(data['file']).write_bytes(image.read_bytes())
        return {'result':'ok'}
    health.request=request
    target=tmp_path/'result.png'
    assert health.owned_capture({'id':23,'pid':os.getpid()},str(target))
    assert health.has_marker(target)
    assert calls==['list-methods','view-shot/capture']


def test_owned_surface_must_fill_its_output_not_another_monitor():
    health=health_module()
    outputs=[{'id':1,'geometry':{'x':3840,'y':0,'width':3840,'height':2560}}]
    shell={'id':23,'pid':os.getpid(),'app-id':'place.poster.desktop','title':'PosterChan Desktop',
           'output-id':1,'geometry':{'x':0,'y':0,'width':3840,'height':2560}}
    popup={**shell,'id':24,'title':'Messages'}
    health.request=lambda method:outputs if method.endswith('list-outputs') else [shell,popup]
    assert health.shell_layout(os.getpid())
    bounds=shell.pop('geometry')
    assert health.shell_layout(os.getpid()) is None
    shell['geometry']=bounds
    shell['geometry']['width']=float('nan')
    assert health.shell_layout(os.getpid()) is None
    shell['geometry']['width']=640
    assert health.shell_layout(os.getpid()) is None
    shell['geometry']['width']=3840;shell['output-id']=2
    assert health.shell_layout(os.getpid()) is None


@pytest.mark.parametrize('colours', [MARKER, SECONDARY_MARKER])
def test_actual_96_pixel_crop_decodes_both_surface_markers(tmp_path, colours):
    health=health_module()
    pixels=bytearray()
    for y in range(96):
        pixels.append(0)
        for x in range(96):
            rgb=colours[(y >= 5)*2+(x >= 5)] if 1 <= x < 9 and 1 <= y < 9 else (0,0,0)
            pixels.extend((*rgb,255))
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    image=tmp_path/'crop.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',96,96,8,6,0,0,0))+
                      chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b''))
    assert health.has_marker(image,colours)
    assert not health.has_marker(image,SECONDARY_MARKER if colours == MARKER else MARKER)


def test_the_helper_cannot_outlive_the_run_that_started_it():
    """A TEST THAT LEAKS A DAEMON EVENTUALLY KILLS THE MACHINE, AND THIS ONE DID.

    The fixture reaps its node child after the yield, which covers a passing or failing test and
    NOT a pytest process that is itself killed — a suite timeout, an operator stopping a gate, an
    OOM. `setInterval` then keeps the child alive with nobody left to signal it.

    Measured here: eight of these were still running after twenty-six hours holding 107MB, and the
    /tmp trees they pin are tmpfs — RAM. The suite had been quietly eating the box for a day, and
    it ended by killing a 45-minute gate run outright with "system is running low on memory".

    Verified by experiment, in both directions: with these lines the child dies within seconds of
    its parent being SIGKILLed; without them it survives.
    """
    src = pathlib.Path(__file__).read_text()
    program = src.split("program=r, 1)[1].split(", 1)[0]
    assert "process.stdin.on('end'" in program, (
        "the helper no longer notices its parent going away — a killed run orphans it for ever")
    assert "setTimeout(bye," in program, (
        "the helper has no maximum lifetime, so it leaks whenever stdin is not a pipe")
    assert "stdin=subprocess.PIPE" in src, (
        "the child's stdin is not a pipe, so it can never see EOF when this process dies")
    fixture = src[src.index("def responder("):src.index("def test_", src.index("def responder("))]
    assert "finally:" in fixture, "a test that raises would leave its child running"
