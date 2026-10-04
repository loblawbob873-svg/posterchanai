"""posterchan-shell/input-passthrough, run in a real (isolated, headless) Wayfire.

"is it possible to make posterchan/axolotl so it don't interfere with clicking widgets/app elements
behind it?" Measured on the laptop: Electron's setShape and setIgnoreMouseEvents do nothing for a click on
Wayland -- the compositor decides which surface a click hits, from the surface's input region. So the
plugin keeps a passthrough window's input region empty. This builds the SHIPPED .cpp, starts a private
headless Wayfire (nothing of the user's session is touched -- same harness as the pointer-confinement
test), maps a window that commits continuously (foot printing), and asks wlroots itself
(wlr_surface_point_accepts_input) whether a point of it takes a click: yes; after "on" no, and still no
after many commits; after "off" yes again; and closing the window while it is on does not take the
compositor down. Skips where the Wayfire SDK is not installed (it is on PosterChanOS).
"""
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from tests.test_wayfire_pointer_confinement_runtime import rpc, _build, PLUGIN, METADATA

PROBE = r'''
#include <wayfire/plugin.hpp>
#include <wayfire/core.hpp>
#include <wayfire/view.hpp>
#include <wayfire/nonstd/wlroots-full.hpp>
#include <wayfire/plugins/ipc/ipc-helpers.hpp>
#include <wayfire/plugins/ipc/ipc-method-repository.hpp>
#include <wayfire/plugins/common/shared-core-data.hpp>
class probe_t : public wf::plugin_interface_t {
 wf::shared_data::ref_ptr_t<wf::ipc::method_repository_t> methods;
 public:
 void init() override {
  methods->register_method("test/accepts", [] (wf::json_t data) {
   auto view = wf::ipc::find_view_by_id(wf::ipc::json_get_uint64(data, "id"));
   wlr_surface *s = view ? view->get_wlr_surface() : nullptr;
   if(!s) return wf::ipc::json_error("missing view");
   wf::json_t out = wf::ipc::json_ok();
   out["accepts"] = wlr_surface_point_accepts_input(s, data["x"].as_double(), data["y"].as_double());
   return out;
  });
 }
 void fini() override { methods->unregister_method("test/accepts"); }
};
DECLARE_WAYFIRE_PLUGIN(probe_t);
'''


@pytest.fixture
def compositor(tmp_path):
    tools = ('g++', 'pkg-config', 'wayfire', 'foot', 'footclient', 'wayland-scanner')
    if any(not shutil.which(tool) for tool in tools):
        pytest.skip('requires the Wayfire SDK, a compiler and foot')
    protocols = subprocess.run(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], capture_output=True, text=True)
    if protocols.returncode:
        pytest.skip('wayland-protocols is not installed')
    base = Path(protocols.stdout.strip())
    for xml, header in (('unstable/pointer-constraints/pointer-constraints-unstable-v1.xml', 'pointer-constraints-unstable-v1-protocol.h'),
                        ('stable/xdg-shell/xdg-shell.xml', 'xdg-shell-protocol.h')):
        subprocess.run(['wayland-scanner', 'server-header', str(base / xml), str(tmp_path / header)], check=True, capture_output=True, timeout=30)
    probe = tmp_path / 'probe.cpp'
    probe.write_text(PROBE)
    _build(tmp_path, PLUGIN, 'posterchan-shell', extra=('-I', str(tmp_path)))
    _build(tmp_path, probe, 'test-probe', extra=('-I', str(tmp_path)))
    meta = tmp_path / 'metadata'
    meta.mkdir()
    for name in ('/usr/share/wayfire/metadata', '/usr/local/share/wayfire/metadata'):
        if Path(name).is_dir():
            for entry in Path(name).glob('*.xml'):
                shutil.copy(entry, meta / entry.name)
    shutil.copy(METADATA, meta / 'posterchan-shell.xml')
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    config = tmp_path / 'wayfire.ini'
    config.write_text('[core]\nplugins = ipc ipc-rules wm-actions posterchan-shell test-probe\nxwayland = false\n')
    env = {**os.environ, 'XDG_RUNTIME_DIR': str(runtime), 'WLR_BACKENDS': 'headless', 'WLR_HEADLESS_OUTPUTS': '1',
           'WLR_RENDERER': 'pixman', 'WAYFIRE_PLUGIN_XML_PATH': str(meta),
           'WAYFIRE_PLUGIN_PATH': str(tmp_path) + ':/usr/lib64/wayfire:/usr/lib/wayfire'}
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'WAYFIRE_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key, None)
    processes = []
    with (tmp_path / 'compositor.log').open('w') as log:
        proc = subprocess.Popen(['wayfire', '-c', str(config)], env=env, stdout=log, stderr=log)
        processes.append(proc)
        try:
            deadline = time.monotonic() + 15
            while True:
                paths = list(runtime.glob('wayfire*.socket'))
                if paths:
                    try:
                        if {'test/accepts', 'posterchan-shell/input-passthrough'} <= set(rpc(paths[0], 'list-methods')['methods']):
                            sock = paths[0]
                            break
                    except (OSError, KeyError):
                        pass
                assert proc.poll() is None, (tmp_path / 'compositor.log').read_text()
                assert time.monotonic() < deadline, 'isolated compositor did not start'
                time.sleep(.05)
            env['WAYLAND_DISPLAY'] = next(p.name for p in runtime.glob('wayland-*') if not p.name.endswith('.lock'))
            server_socket = runtime / 'test-foot.sock'
            server = subprocess.Popen(['foot', '--server=' + str(server_socket)], env=env, stdout=log, stderr=log)
            processes.append(server)
            deadline = time.monotonic() + 10
            while not server_socket.exists():
                assert time.monotonic() < deadline, 'private terminal server did not start'
                time.sleep(.05)
            # A window that keeps committing: it prints forever.
            child = subprocess.Popen(['footclient', '--server-socket=' + str(server_socket), '--title=dancer',
                                      'sh', '-c', 'while :; do date; sleep .05; done'], env=env, stdout=log, stderr=log)
            processes.append(child)
            deadline = time.monotonic() + 10
            while True:
                rows = rpc(sock, 'window-rules/list-views')
                rows = rows if isinstance(rows, list) else rows.get('views', [])
                match = [v for v in rows if v.get('title') == 'dancer' and v.get('mapped')]
                if match:
                    break
                assert time.monotonic() < deadline, 'test client did not map'
                time.sleep(.05)
            yield sock, match[0], child, proc
        finally:
            for c in reversed(processes):
                if c.poll() is None:
                    c.terminate()
            for c in reversed(processes):
                try:
                    c.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    c.kill()
                    c.wait(timeout=3)


def _accepts(sock, view):
    r = rpc(sock, 'test/accepts', {'id': view['id'], 'x': 20, 'y': 20})
    assert r.get('result') == 'ok', r
    return r['accepts']


def test_passthrough_empties_the_input_region_and_keeps_it_empty_across_commits(compositor):
    sock, view, _child, _proc = compositor
    assert _accepts(sock, view) is True, 'a normal window must take a click'
    assert rpc(sock, 'posterchan-shell/input-passthrough', {'id': view['id'], 'on': True}).get('result') == 'ok'
    assert _accepts(sock, view) is False, 'passthrough on, and the window still takes the click'
    time.sleep(1.0)                                   # ~20 commits from a window that keeps printing
    assert _accepts(sock, view) is False, 'a commit gave the window its clicks back'
    assert rpc(sock, 'posterchan-shell/input-passthrough', {'id': view['id'], 'on': False}).get('result') == 'ok'
    assert _accepts(sock, view) is True, 'passthrough off, and the window still lets clicks through'
    time.sleep(.5)
    assert _accepts(sock, view) is True


def test_a_window_closed_while_passing_clicks_through_does_not_take_the_compositor_down(compositor):
    sock, view, child, proc = compositor
    assert rpc(sock, 'posterchan-shell/input-passthrough', {'id': view['id'], 'on': True}).get('result') == 'ok'
    child.terminate()
    child.wait(timeout=5)
    time.sleep(.5)
    assert proc.poll() is None, 'the compositor died when the window went away'
    assert 'posterchan-shell/input-passthrough' in rpc(sock, 'list-methods')['methods']
    # And "off" for a window that no longer exists is an answer, not a crash.
    assert rpc(sock, 'posterchan-shell/input-passthrough', {'id': view['id'], 'on': False}).get('result') == 'ok'


