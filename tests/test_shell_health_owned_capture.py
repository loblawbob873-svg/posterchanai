"""Actual Node capture responder + Python health reader, with only pixel/IPC boundaries synthetic."""
import json
import os
from pathlib import Path
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
const keep=setInterval(()=>{},1000);process.on('SIGTERM',()=>{stop();clearInterval(keep);process.exit(0)});
'''
    process=subprocess.Popen(['node','-e',program,str(ROOT/'desktop/shell-health-capture.js'),str(tmp_path),str(image)],stdout=subprocess.PIPE,text=True)
    pid=int(process.stdout.readline().strip())
    yield pid,image,tmp_path
    process.terminate();process.wait(timeout=5)
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
