"""Run the actual plugin and upstream move/resize plugins in an isolated compositor.

The probe only supplies the same request signal the compositor's bindings emit and
reads compositor state. It does not replace the production plugin or move policy.
No socket, config, input, or process from the user's real desktop is used.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'os/overlay/gui-libs/posterchan-wayfire-shell/files/posterchan-shell.cpp'
PROBE = r'''
#include <wayfire/plugin.hpp>
#include <wayfire/toplevel-view.hpp>
#include <wayfire/signal-definitions.hpp>
#include <wayfire/plugins/ipc/ipc-helpers.hpp>
#include <wayfire/plugins/common/shared-core-data.hpp>
class probe_t : public wf::plugin_interface_t {
 wf::shared_data::ref_ptr_t<wf::ipc::method_repository_t> methods;
 public:
 void init() override { methods->register_method("test/view", [](wf::json_t data) {
  auto v=wf::toplevel_cast(wf::ipc::find_view_by_id(wf::ipc::json_get_uint64(data,"id")));
  if(!v) return wf::ipc::json_error("missing view");
  if(data.has_member("actions")) v->set_allowed_actions(wf::ipc::json_get_uint64(data,"actions"));
  wf::json_t out; out["actions"]=v->get_allowed_actions();
  if(data.has_member("attempt")) {
   auto o=v->get_output(); auto kind=wf::ipc::json_get_string(data,"attempt");
   if(kind=="move") { wf::view_move_request_signal ev{v}; o->emit(&ev); }
   else { wf::view_resize_request_signal ev{v,8}; o->emit(&ev); }
   out["active"]=o->is_plugin_active(kind); o->cancel_active_plugins();
  }
  return out;
 }); }
 void fini() override { methods->unregister_method("test/view"); }
};
DECLARE_WAYFIRE_PLUGIN(probe_t);
'''


def rpc(path, method, data=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        body = json.dumps({'method': method, 'data': data or {}}).encode()
        sock.sendall(struct.pack('<I', len(body)) + body)
        def read(n):
            out = b''
            while len(out) < n:
                part = sock.recv(n-len(out))
                assert part, 'compositor closed IPC'
                out += part
            return out
        return json.loads(read(struct.unpack('<I', read(4))[0]))


@pytest.fixture
def compositor(tmp_path):
    if any(not shutil.which(tool) for tool in ('g++', 'pkg-config', 'wayfire', 'foot', 'footclient')):
        pytest.skip('requires Wayfire SDK, compiler and foot for real compositor test')
    flags = subprocess.run(['pkg-config', '--cflags', '--libs', 'wayfire'], capture_output=True, text=True)
    if flags.returncode:
        pytest.skip('Wayfire development headers unavailable')
    probe = tmp_path/'probe.cpp'
    probe.write_text(PROBE)
    for source, name in ((PLUGIN, 'posterchan-shell'), (probe, 'test-probe')):
        subprocess.run(['g++','-std=c++17','-fPIC','-shared',str(source),'-o',str(tmp_path/f'lib{name}.so'),
                        *flags.stdout.split()], check=True, capture_output=True, timeout=30)
    runtime=tmp_path/'runtime'; runtime.mkdir(mode=0o700)
    config=tmp_path/'wayfire.ini'
    config.write_text('[core]\nplugins = ipc ipc-rules move resize posterchan-shell test-probe\nxwayland = false\n')
    env={**os.environ, 'XDG_RUNTIME_DIR':str(runtime), 'WLR_BACKENDS':'headless',
         'WLR_HEADLESS_OUTPUTS':'1','WLR_RENDERER':'pixman',
         'WAYFIRE_PLUGIN_PATH':str(tmp_path)+':/usr/lib64/wayfire:/usr/lib/wayfire'}
    # Prevent nested tests from reaching any session bus / compositor inherited from the user.
    for key in ('WAYLAND_DISPLAY','DISPLAY','WAYFIRE_SOCKET','DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key,None)
    processes=[]
    with (tmp_path/'compositor.log').open('w') as log:
        proc=subprocess.Popen(['wayfire','-c',str(config)],env=env,stdout=log,stderr=log)
        processes.append(proc)
        try:
            deadline=time.monotonic()+10
            while True:
                paths=list(runtime.glob('wayfire*.socket'))
                if paths:
                    try:
                        methods=rpc(paths[0],'list-methods')['methods']
                        if 'test/view' in methods and 'posterchan-shell/set-views' in methods: break
                    except (OSError,KeyError): pass
                assert proc.poll() is None, (tmp_path/'compositor.log').read_text()
                assert time.monotonic()<deadline, 'isolated compositor did not start'
                time.sleep(.05)
            sock=paths[0]
            env['WAYLAND_DISPLAY']=next(p.name for p in runtime.glob('wayland-*') if not p.name.endswith('.lock'))
            server_socket=runtime/'test-foot.sock'
            server=subprocess.Popen(['foot','--server='+str(server_socket)],env=env,stdout=log,stderr=log)
            processes.append(server)
            deadline=time.monotonic()+5
            while not server_socket.exists():
                assert server.poll() is None, (tmp_path/'compositor.log').read_text()
                assert time.monotonic()<deadline, 'private terminal server did not start'
                time.sleep(.05)
            views=[]
            for name in ('PosterChan Desktop','ordinary'):
                child=subprocess.Popen(['footclient','--server-socket='+str(server_socket),'--app-id=posterchan-desktop','--title='+name,'sh','-c','sleep 60'],
                                       env=env,stdout=log,stderr=log)
                processes.append(child)
                deadline=time.monotonic()+8
                while True:
                    rows=rpc(sock,'window-rules/list-views')
                    matches=[v for v in rows if v.get('title')==name and v.get('mapped')]
                    if matches: views.append(matches[0]); break
                    assert child.poll() is None, (tmp_path/'compositor.log').read_text()
                    assert time.monotonic()<deadline, 'test client did not map'
                    time.sleep(.05)
            yield sock,views,config
        finally:
            for child in reversed(processes):
                if child.poll() is None: child.terminate()
            for child in reversed(processes):
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)


def test_exact_shell_capabilities_block_upstream_drag_and_resize(compositor):
    sock,(shell,ordinary),config=compositor
    def inspect(view, **kw): return rpc(sock,'test/view',{'id':view['id'],**kw})
    def register(ids, pid=None):
        return rpc(sock,'posterchan-shell/set-views',{'pid':pid or shell['pid'],'ids':ids})
    assert shell['pid']==ordinary['pid'], 'both test windows must share the same native client process'
    before=inspect(shell)['actions']
    assert before & 3 == 3
    assert inspect(shell,attempt='move')['active'] is True
    assert inspect(shell,attempt='resize')['active'] is True
    assert 'error' not in register([shell['id']])
    assert inspect(shell)['actions']==before & ~3
    assert inspect(shell,attempt='move')['active'] is False
    assert inspect(shell,attempt='resize')['active'] is False
    # Capabilities block interactive gestures, not main's output/hotplug placement IPC.
    target={'x':80,'y':60,'width':640,'height':480}
    placed=rpc(sock,'window-rules/configure-view',{'id':shell['id'],'geometry':target})
    assert 'error' not in placed
    deadline=time.monotonic()+3
    while True:
        current=next(v for v in rpc(sock,'window-rules/list-views') if v['id']==shell['id'])
        if current['geometry']==target: break
        assert time.monotonic()<deadline, current['geometry']
        time.sleep(.05)
    assert inspect(ordinary,attempt='move')['active'] is True
    assert inspect(ordinary,attempt='resize')['active'] is True
    # Validation is transactional: bad/foreign IDs must not unlock the registered desktop.
    assert 'error' in register([ordinary['id']])
    assert 'error' in register([ordinary['id']],pid=ordinary['pid'])
    assert 'error' in register([0])
    assert 'error' in register([shell['id']],pid=shell['pid']+100000)
    assert inspect(shell)['actions']==before & ~3
    assert 'error' not in register([shell['id'],shell['id']])
    assert 'error' not in register([])
    assert inspect(shell)['actions']==before
    assert inspect(shell,attempt='move')['active'] is True
    # Preserve unrelated capabilities modified by another plugin while protection is active.
    assert 'error' not in register([shell['id']])
    inspect(shell,actions=0)
    assert 'error' not in register([])
    assert inspect(shell)['actions']==3
    # Unloading the actual plugin restores the capabilities it owns, without resurrecting WS_CHANGE.
    assert 'error' not in register([shell['id']])
    config.write_text(config.read_text().replace(' posterchan-shell',''))
    deadline=time.monotonic()+5
    while 'posterchan-shell/set-views' in rpc(sock,'list-methods')['methods']:
        assert time.monotonic()<deadline, 'plugin did not unload'
        time.sleep(.05)
    assert inspect(shell)['actions']==3
