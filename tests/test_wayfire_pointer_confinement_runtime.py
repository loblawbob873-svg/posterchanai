"""Run the REAL posterchan-shell plugin in an isolated compositor and measure the pointer.

WHY A RUNTIME TEST AND NOT A GREP. The rule this file pins is arithmetic on a live pointer --
"a motion event that would leave the fullscreen window's output is shortened so it does not" -- and
every way of asserting that from the source text passes against code that never runs. The plugin
that shipped before this one was measured doing the OPPOSITE of what its config said on a real
machine (force-fullscreen's constraint releases itself on the next focus change), and no test over
its text could have seen that.

So: build the shipped .cpp, start a private headless Wayfire with TWO outputs, map two windows, and
have a probe plugin emit real wlr_pointer_motion events on core -- the same signal the compositor
emits for a mouse -- then read back the delta the plugin left behind. Nothing here touches the
user's session: its own runtime dir, its own config, headless backend, pixman renderer.

The probe emits the event rather than applying it, because applying it is wlroots' job and what is
under test is the plugin's answer, not wlr_cursor.
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
METADATA = ROOT / 'os/overlay/gui-libs/posterchan-wayfire-shell/files/posterchan-shell.xml'
# FOUND, NOT SPELLED OUT. A revision bump RENAMES the ebuild — that is the whole mechanism by which
# a change reaches an installed machine (see test_a_config_change_actually_reaches_the_machine) — so
# hardcoding the filename means this file goes red on exactly the commit that releases its own fix.
_EB_DIR = ROOT / 'os/overlay/gui-libs/posterchan-wayfire-shell'
_EBUILDS = sorted(_EB_DIR.glob('posterchan-wayfire-shell-*.ebuild'))
assert _EBUILDS, 'no posterchan-wayfire-shell ebuild in ' + str(_EB_DIR)
EBUILD = _EBUILDS[-1]

PROBE = r'''
#include <wayfire/plugin.hpp>
#include <wayfire/core.hpp>
#include <wayfire/seat.hpp>
#include <wayfire/output.hpp>
#include <wayfire/output-layout.hpp>
#include <wayfire/toplevel-view.hpp>
#include <wayfire/window-manager.hpp>
#include <wayfire/signal-definitions.hpp>
#include <wayfire/txn/transaction-manager.hpp>
#include <wayfire/plugins/ipc/ipc-helpers.hpp>
#include <wayfire/plugins/ipc/ipc-method-repository.hpp>
#include <wayfire/plugins/common/shared-core-data.hpp>
class probe_t : public wf::plugin_interface_t {
 wf::shared_data::ref_ptr_t<wf::ipc::method_repository_t> methods;
 public:
 void init() override {
  /* Put the cursor somewhere known, emit one motion event exactly as wf::cursor_t does, and report
   * the delta AFTER every handler on core has had it. */
  methods->register_method("test/motion", [] (wf::json_t data) {
   wf::get_core().warp_cursor({(double)data["x"].as_double(), (double)data["y"].as_double()});
   wlr_pointer_motion_event ev{};
   ev.delta_x = ev.unaccel_dx = data["dx"].as_double();
   ev.delta_y = ev.unaccel_dy = data["dy"].as_double();
   wf::input_event_signal<wlr_pointer_motion_event> sig;
   sig.event = &ev; sig.device = NULL;
   wf::get_core().emit(&sig);
   auto at = wf::get_core().get_cursor_position();
   wf::json_t out = wf::ipc::json_ok();
   out["dx"] = ev.delta_x; out["dy"] = ev.delta_y;
   out["unaccel_dx"] = ev.unaccel_dx; out["unaccel_dy"] = ev.unaccel_dy;
   out["x"] = at.x + ev.delta_x; out["y"] = at.y + ev.delta_y;
   return out;
  });
  /* Fullscreen a view ON a named output and make it the active one, which is what a game does. */
  methods->register_method("test/stage", [] (wf::json_t data) {
   auto view = wf::toplevel_cast(wf::ipc::find_view_by_id(wf::ipc::json_get_uint64(data, "id")));
   if(!view) return wf::ipc::json_error("missing view");
   wf::output_t *target = NULL;
   for(auto o : wf::get_core().output_layout->get_outputs())
     if(std::string(o->handle->name) == wf::ipc::json_get_string(data, "output")) target = o;
   if(!target) return wf::ipc::json_error("missing output");
   if(view->get_output() != target) wf::move_view_to_output(view, target, false);
   if(data.has_member("parent")) {
     auto p = wf::toplevel_cast(wf::ipc::find_view_by_id(data["parent"].as_uint64()));
     view->set_toplevel_parent(p);
   }
   view->toplevel()->pending().fullscreen = data["fullscreen"].as_bool();
   wf::get_core().tx_manager->schedule_object(view->toplevel());
   wf::get_core().seat->focus_output(target);
   wf::get_core().default_wm->focus_request(view);
   return wf::ipc::json_ok();
  });
 }
 void fini() override {
  methods->unregister_method("test/motion");
  methods->unregister_method("test/stage");
 }
};
DECLARE_WAYFIRE_PLUGIN(probe_t);
'''


def rpc(path, method, data=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        body = json.dumps({'method': method, 'data': data or {}}).encode()
        sock.sendall(struct.pack('<I', len(body)) + body)
        def read(n):
            out = b''
            while len(out) < n:
                part = sock.recv(n - len(out))
                assert part, 'compositor closed IPC'
                out += part
            return out
        return json.loads(read(struct.unpack('<I', read(4))[0]))


def _build(tmp_path, source, name, extra=()):
    flags = subprocess.run(['pkg-config', '--cflags', '--libs', 'wayfire'],
                           capture_output=True, text=True)
    if flags.returncode:
        pytest.skip('Wayfire development headers unavailable')
    subprocess.run(['g++', '-std=c++17', '-fPIC', '-shared', *extra, str(source),
                    '-o', str(tmp_path / f'lib{name}.so'), *flags.stdout.split()],
                   check=True, capture_output=True, timeout=60)


@pytest.fixture
def compositor(tmp_path):
    tools = ('g++', 'pkg-config', 'wayfire', 'foot', 'footclient', 'wayland-scanner')
    if any(not shutil.which(tool) for tool in tools):
        pytest.skip('requires the Wayfire SDK, a compiler and foot')
    protocols = subprocess.run(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'],
                               capture_output=True, text=True)
    if protocols.returncode:
        pytest.skip('wayland-protocols is not installed')
    # The same generated header the ebuild makes: wlroots does not install it, so without this the
    # pointer-constraint guard would not compile. See the note in the ebuild.
    xml = Path(protocols.stdout.strip()) / 'unstable/pointer-constraints/pointer-constraints-unstable-v1.xml'
    if not xml.exists():
        pytest.skip('pointer-constraints-unstable-v1.xml is not installed')
    subprocess.run(['wayland-scanner', 'server-header', str(xml),
                    str(tmp_path / 'pointer-constraints-unstable-v1-protocol.h')],
                   check=True, capture_output=True, timeout=30)
    probe = tmp_path / 'probe.cpp'
    probe.write_text(PROBE)
    _build(tmp_path, PLUGIN, 'posterchan-shell', extra=('-I', str(tmp_path)))
    _build(tmp_path, probe, 'test-probe')
    # The option only exists if the shipped metadata declares it; a plugin whose XML is not read
    # cannot be configured at all, and the compositor answers "Option not found!".
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
    config.write_text('[core]\nplugins = ipc ipc-rules wm-actions posterchan-shell test-probe\n'
                      'xwayland = false\n')
    env = {**os.environ, 'XDG_RUNTIME_DIR': str(runtime), 'WLR_BACKENDS': 'headless',
           'WLR_HEADLESS_OUTPUTS': '2', 'WLR_RENDERER': 'pixman',
           'WAYFIRE_PLUGIN_XML_PATH': str(meta),
           'WAYFIRE_PLUGIN_PATH': str(tmp_path) + ':/usr/lib64/wayfire:/usr/lib/wayfire'}
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'WAYFIRE_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key, None)
    processes = []
    with (tmp_path / 'compositor.log').open('w') as log:
        proc = subprocess.Popen(['wayfire', '-c', str(config)], env=env, stdout=log, stderr=log)
        processes.append(proc)
        try:
            deadline = time.monotonic() + 15
            sock = None
            while True:
                paths = list(runtime.glob('wayfire*.socket'))
                if paths:
                    try:
                        methods = rpc(paths[0], 'list-methods')['methods']
                        if {'test/motion', 'posterchan-shell/pointer-confinement'} <= set(methods):
                            sock = paths[0]
                            break
                    except (OSError, KeyError):
                        pass
                assert proc.poll() is None, (tmp_path / 'compositor.log').read_text()
                assert time.monotonic() < deadline, 'isolated compositor did not start'
                time.sleep(.05)
            outputs = rpc(sock, 'window-rules/list-outputs')
            outputs = outputs if isinstance(outputs, list) else outputs.get('outputs', [])
            assert len(outputs) >= 2, 'this test needs two outputs to have somewhere to escape to'
            env['WAYLAND_DISPLAY'] = next(p.name for p in runtime.glob('wayland-*')
                                          if not p.name.endswith('.lock'))
            server_socket = runtime / 'test-foot.sock'
            server = subprocess.Popen(['foot', '--server=' + str(server_socket)],
                                      env=env, stdout=log, stderr=log)
            processes.append(server)
            deadline = time.monotonic() + 10
            while not server_socket.exists():
                assert server.poll() is None, (tmp_path / 'compositor.log').read_text()
                assert time.monotonic() < deadline, 'private terminal server did not start'
                time.sleep(.05)
            views = []
            for name in ('game', 'dialog'):
                child = subprocess.Popen(['footclient', '--server-socket=' + str(server_socket),
                                          '--title=' + name, 'sh', '-c', 'sleep 120'],
                                         env=env, stdout=log, stderr=log)
                processes.append(child)
                deadline = time.monotonic() + 10
                while True:
                    rows = rpc(sock, 'window-rules/list-views')
                    rows = rows if isinstance(rows, list) else rows.get('views', [])
                    match = [v for v in rows if v.get('title') == name and v.get('mapped')]
                    if match:
                        views.append(match[0])
                        break
                    assert child.poll() is None, (tmp_path / 'compositor.log').read_text()
                    assert time.monotonic() < deadline, 'test client did not map'
                    time.sleep(.05)
            yield sock, views, outputs
        finally:
            for child in reversed(processes):
                if child.poll() is None:
                    child.terminate()
            for child in reversed(processes):
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


def _enable(sock, on):
    answer = rpc(sock, 'wayfire/set-config-options',
                 {'posterchan-shell/confine_pointer_to_fullscreen': bool(on)})
    assert answer.get('result') == 'ok', answer
    assert rpc(sock, 'posterchan-shell/pointer-confinement')['enabled'] is bool(on)


def test_a_fullscreen_window_keeps_the_pointer_on_its_own_monitor(compositor):
    sock, (game, dialog), outputs = compositor
    first, second = outputs[0], outputs[1]
    box = first['geometry']
    inside = {'x': box['x'] + box['width'] - 20, 'y': box['y'] + box['height'] // 2}
    far = box['width'] + second['geometry']['width']

    def push(**kw):
        return rpc(sock, 'test/motion', {'dx': far, 'dy': 0, **inside, **kw})

    def stage(view, fullscreen, **kw):
        answer = rpc(sock, 'test/stage', {'id': view['id'], 'output': first['name'],
                                          'fullscreen': fullscreen, **kw})
        assert 'error' not in answer, answer
        time.sleep(.3)

    # ON IS THE SHIPPED DEFAULT NOW, and this is the assertion that reads it out of the BUILT
    # PLUGIN rather than out of the .xml -- the two can disagree, and only this one is what a
    # machine gets. (It shipped off; "no other os makes you fucking toggle the cursor guard".)
    assert rpc(sock, 'posterchan-shell/pointer-confinement')['enabled'] is True, \
        'the compositor did not come up confining; the metadata default did not reach the plugin'

    # And OFF still means off, which is the whole of "keeping the IPC toggle". Turning it off must
    # let the pointer go wherever it likes, fullscreen window or not.
    _enable(sock, False)
    stage(game, True)
    assert push()['x'] > box['x'] + box['width'], 'the switch is off; nothing may hold the pointer'

    _enable(sock, True)
    # ON, but the focused window is NOT fullscreen: still nothing to confine to.
    stage(game, False)
    assert rpc(sock, 'posterchan-shell/pointer-confinement')['confined'] is False
    assert push()['x'] > box['x'] + box['width'], 'only a FULLSCREEN window confines the pointer'

    # ON and fullscreen: the pointer stops on this output, and on its LAST pixel -- not on
    # box.x + box.width, which the output layout hands to the NEXT monitor.
    stage(game, True)
    state = rpc(sock, 'posterchan-shell/pointer-confinement')
    assert state['confined'] is True and state['output'] == first['name'], state
    held = push()
    assert held['x'] == box['x'] + box['width'] - 1, held
    assert push(dx=0, dy=far)['y'] == box['y'] + box['height'] - 1
    assert push(dx=-far, dy=0)['x'] == box['x']
    # It is a clamp, not a block: ordinary movement inside the output is untouched.
    assert push(dx=-30, dy=-40) ['x'] == inside['x'] - 30

    # A DIALOG OF THE FULLSCREEN WINDOW IS STILL THAT WINDOW. Focusing a launcher or an overlay
    # must not hand the pointer back to the other monitor.
    stage(dialog, False, parent=game['id'])
    assert rpc(sock, 'posterchan-shell/pointer-confinement')['confined'] is True
    assert push()['x'] == box['x'] + box['width'] - 1

    # An unrelated window focused IS the way out, and needs no setting change. (parent=0 detaches
    # it again -- a transient that keeps its parent is still the game, which is the rule above.)
    stage(dialog, False, parent=0)
    assert rpc(sock, 'posterchan-shell/pointer-confinement')['confined'] is False
    assert push()['x'] > box['x'] + box['width']

    # And turning it off releases it while the game is still fullscreen and focused.
    stage(game, True)
    assert push()['x'] == box['x'] + box['width'] - 1
    _enable(sock, False)
    assert push()['x'] > box['x'] + box['width']


def test_the_pointer_is_confined_not_captured(compositor):
    """A pointer that is already on another monitor is left alone rather than yanked back."""
    sock, (game, _dialog), outputs = compositor
    first, second = outputs[0], outputs[1]
    _enable(sock, True)
    answer = rpc(sock, 'test/stage', {'id': game['id'], 'output': first['name'], 'fullscreen': True})
    assert 'error' not in answer, answer
    time.sleep(.3)
    elsewhere = {'x': second['geometry']['x'] + 10, 'y': second['geometry']['y'] + 10}
    moved = rpc(sock, 'test/motion', {'dx': 25, 'dy': 0, **elsewhere})
    assert moved['dx'] == 25, moved
    assert moved['x'] == elsewhere['x'] + 25, moved


def test_the_option_is_declared_on_and_the_package_that_carries_it_is_required():
    """Three files have to agree or the switch is silently inert on a real machine.

    IT NOW SHIPS ON. "no other os makes you fucking toggle the cursor guard" -- and the argument it
    shipped off under (a pointer that cannot leave a monitor is a trap if it fires on the wrong
    window) describes something bounded and reversible, while the cursor walking out of a fullscreen
    game onto the second screen is what actually happens on the hardware this ships to.

    THE DEFAULT LIVES IN THREE PLACES AND THE LAST ONE WINS. wayfire reads the .xml default, the
    shipped wayfire.ini then states it explicitly, and a moment later [autostart] runs
    `pc-pointer-confine apply`, which pushes the PERSON'S stored answer over the top of both. Leave
    the helper's no-file fallback at "false" and the setting is correct in two files and off on
    every machine, every sign-in, with nothing anywhere to say so.
    """
    xml = METADATA.read_text()
    assert 'confine_pointer_to_fullscreen' in xml, \
        'an option missing from the metadata cannot be set over IPC at all'
    assert '<default>true</default>' in xml, 'this must ship ON'
    ini = (ROOT / 'os/overlay/app-misc/posterchanos-shell/files/wayfire.ini').read_text()
    assert 'confine_pointer_to_fullscreen = true' in ini
    assert 'pc-pointer-confine apply' in ini, \
        'without an autostart line the choice is forgotten at every sign-in'
    shell = next((ROOT / 'os/overlay/app-misc/posterchanos-shell').glob('posterchanos-shell-*.ebuild')).read_text()
    # Derived from the overlay, never typed: `emerge -u` only pulls a dependency it is told a
    # version for, so a revbump of the plugin that leaves this floor behind ships the new .so to
    # nobody -- and a floor hardcoded here would go stale at the next bump instead of catching it.
    plugin = next((ROOT / 'os/overlay/gui-libs/posterchan-wayfire-shell').glob('*.ebuild'))
    version = plugin.stem.replace('posterchan-wayfire-shell-', '')
    assert f'>=gui-libs/posterchan-wayfire-shell-{version}' in shell, \
        (f'the overlay carries {version} and posterchanos-shell still asks for an older floor; '
         'emerge -u will leave the installed plugin exactly where it is')
    assert ' pc-pointer-confine ' in shell, 'the helper has to be installed'
    assert 'wayland-scanner' in EBUILD.read_text(), \
        'wlroots ships no generated protocol header, so the constraint guard would not compile'
    gentoo = (ROOT / 'os/gentoo.sh').read_text()
    assert ' pc-pointer-confine ' in gentoo, 'a fresh install copies the helpers itself'


@pytest.mark.parametrize('stored,expected', [
    (None, 'true'),        # a machine nobody has ever touched this on
    ('', 'true'),          # and one whose file is empty or half-written
    ('garbage', 'true'),
    ('true\n', 'true'),
    ('false\n', 'false'),  # the ONE way to turn it off, and it still works
])
def test_the_helper_defaults_to_on_and_only_the_word_false_turns_it_off(tmp_path, stored, expected):
    """RUN the shipped helper, because this fallback is what [autostart] pushes every sign-in.

    `apply` sets the compositor from `read_pref`, so this function -- not the .xml and not
    wayfire.ini -- is what a person ends the session start with.
    """
    if not shutil.which('sh'):
        pytest.skip('no POSIX shell')
    conf = tmp_path / 'pointer-confine'
    if stored is not None:
        conf.write_text(stored)
    for helper in (ROOT / 'os/bin/pc-pointer-confine',
                   ROOT / 'os/overlay/app-misc/posterchanos-shell/files/pc-pointer-confine'):
        answer = subprocess.run(['sh', str(helper), 'get'], capture_output=True, text=True,
                                env={**os.environ, 'PC_POINTER_CONFINE_CONF': str(conf)})
        assert answer.stdout.strip() == expected, (helper.name, stored, answer.stdout, answer.stderr)


def test_both_copies_of_the_helper_are_the_same_file():
    """One is installed by the ebuild and one by os/gentoo.sh; a fix applied to one is applied to no
    machine that took the other route."""
    a = (ROOT / 'os/bin/pc-pointer-confine').read_bytes()
    b = (ROOT / 'os/overlay/app-misc/posterchanos-shell/files/pc-pointer-confine').read_bytes()
    assert a == b, 'os/bin and the overlay copy of pc-pointer-confine have drifted' 
def test_a_missing_metadata_file_does_not_take_the_whole_desktop_down(tmp_path):
    """The .so without its .xml must degrade to "off", never kill the compositor.

    `wf::option_wrapper_t` throws for an undeclared option, and on Wayfire 0.10.1 that throw ends in
    a SEGMENTATION FAULT during startup -- measured: "No such option:
    posterchan-shell/confine_pointer_to_fullscreen ... Fatal error: Segmentation fault", no desktop,
    and a text console as the only way back. The two files ship in one package, but a stale or
    hand-copied metadata directory must not be able to cost somebody their machine.
    """
    tools = ('g++', 'pkg-config', 'wayfire', 'wayland-scanner')
    if any(not shutil.which(tool) for tool in tools):
        pytest.skip('requires the Wayfire SDK and a compiler')
    protocols = subprocess.run(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'],
                               capture_output=True, text=True)
    xml = Path(protocols.stdout.strip() or '/nonexistent') / \
        'unstable/pointer-constraints/pointer-constraints-unstable-v1.xml'
    if protocols.returncode or not xml.exists():
        pytest.skip('wayland-protocols is not installed')
    subprocess.run(['wayland-scanner', 'server-header', str(xml),
                    str(tmp_path / 'pointer-constraints-unstable-v1-protocol.h')],
                   check=True, capture_output=True, timeout=30)
    _build(tmp_path, PLUGIN, 'posterchan-shell', extra=('-I', str(tmp_path)))
    # A metadata directory carrying every OTHER plugin's XML and not ours -- which is what an
    # outdated install looks like, and is not the same as an empty directory (wayfire needs the
    # core/ipc descriptions to start at all).
    meta = tmp_path / 'metadata'
    meta.mkdir()
    for name in ('/usr/share/wayfire/metadata', '/usr/local/share/wayfire/metadata'):
        if Path(name).is_dir():
            for entry in Path(name).glob('*.xml'):
                if entry.name != 'posterchan-shell.xml':
                    shutil.copy(entry, meta / entry.name)
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    config = tmp_path / 'wayfire.ini'
    config.write_text('[core]\nplugins = ipc ipc-rules posterchan-shell\nxwayland = false\n')
    env = {**os.environ, 'XDG_RUNTIME_DIR': str(runtime), 'WLR_BACKENDS': 'headless',
           'WLR_HEADLESS_OUTPUTS': '1', 'WLR_RENDERER': 'pixman',
           'WAYFIRE_PLUGIN_XML_PATH': str(meta),
           'WAYFIRE_PLUGIN_PATH': str(tmp_path) + ':/usr/lib64/wayfire:/usr/lib/wayfire'}
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'WAYFIRE_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key, None)
    log = tmp_path / 'compositor.log'
    with log.open('w') as stream:
        proc = subprocess.Popen(['wayfire', '-c', str(config)], env=env, stdout=stream, stderr=stream)
        try:
            deadline = time.monotonic() + 15
            while True:
                paths = list(runtime.glob('wayfire*.socket'))
                if paths:
                    try:
                        methods = rpc(paths[0], 'list-methods')['methods']
                        break
                    except (OSError, KeyError):
                        pass
                assert proc.poll() is None, \
                    'the compositor DIED without its metadata file:\n' + log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(.05)
            # It is loaded and working; only the switch is gone.
            assert 'posterchan-shell/set-views' in methods
            state = rpc(paths[0], 'posterchan-shell/pointer-confinement')
            assert state['enabled'] is False and state['confined'] is False, state
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
