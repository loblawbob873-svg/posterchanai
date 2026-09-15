"""Actual Electron loopback captures an isolated output tone, never a microphone."""
import json
import math
import os
from pathlib import Path
import select
import shutil
import struct
import subprocess
import time
import wave

from tests.test_native_window_reload_electron import _native_popen, _stop_native, _missing_runtime

ROOT = Path(__file__).resolve().parents[1]


def test_linux_system_audio_is_the_playback_monitor(tmp_path):
    candidates = [Path(os.environ.get('PC_ELECTRON_BINARY', '/nonexistent-electron')),
                  ROOT / 'desktop/node_modules/electron/dist/electron']
    electron = next((p for p in candidates if p.is_file()), None)
    xvfb = shutil.which('Xvfb')
    if electron is None or not all(shutil.which(p) for p in ('pulseaudio', 'pactl', 'paplay')):
        _missing_runtime('Electron and isolated PulseAudio tools required')
    if not xvfb and not all(shutil.which(p) for p in ('wayfire', 'Xwayland')):
        _missing_runtime('Xvfb or Wayfire plus Xwayland required for isolated display')
    source = (ROOT / 'desktop/main.js').read_text()
    handler = source[source.index('function wirePermissions()'):source.index('// ---- screen-source picker')]
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    config = tmp_path / 'wayfire.ini'
    config.write_text('[core]\nplugins = ipc ipc-rules\nxwayland = false\n')
    env = {**os.environ, 'XDG_RUNTIME_DIR': str(runtime), 'WLR_BACKENDS': 'headless',
           'WLR_HEADLESS_OUTPUTS': '1', 'WLR_RENDERER': 'pixman', 'GDK_BACKEND': 'wayland',
           'PULSE_SERVER': 'unix:' + str(runtime / 'pulse-test.sock'),
           'PULSE_SINK': 'fixture_output', 'PULSE_SOURCE': 'fixture_microphone'}
    for key in ('ELECTRON_RUN_AS_NODE', 'WAYLAND_DISPLAY', 'DISPLAY', 'WAYFIRE_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key, None)
    tone = tmp_path / 'tone.wav'
    with wave.open(str(tone), 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b''.join(struct.pack('<hh', *([int(8000 * math.sin(2 * math.pi * 750 * i / 48000))] * 2)) for i in range(48000 * 12)))
    page = tmp_path / 'capture.html'
    page.write_text('<!doctype html><body>Isolated system audio capture</body>')
    script = '''const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict');
app.setPath('userData',PROFILE);
let win,remoteCaptureGeneration=0,remoteControlDisplayId='',remoteControlDisplayExplicit=false;
const isOurs=url=>url.startsWith('file:'),isWebxdcSandbox=()=>false,screenLog=()=>{};
const pickScreenSource=async()=>win.webContents.mainFrame;
HANDLER
const timer=setTimeout(()=>{console.error('capture timed out');app.exit(1)},20000);
app.whenReady().then(async()=>{
 wirePermissions();win=new BrowserWindow({show:true,webPreferences:{contextIsolation:true,nodeIntegration:false}});
 await win.loadFile(PAGE);
 const result=await win.webContents.executeJavaScript(`(async()=>{
  navigator.mediaDevices.getUserMedia=()=>Promise.reject(Error('microphone fallback forbidden'));
  const stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true,systemAudio:'include'});
  const tracks=stream.getAudioTracks();if(tracks.length!==1)throw Error('expected one system audio track, got '+tracks.length);
  const context=new AudioContext({sampleRate:48000});await context.resume();
  const input=context.createMediaStreamSource(stream),analyser=context.createAnalyser();analyser.fftSize=4096;input.connect(analyser);
  const bins=new Float32Array(analyser.frequencyBinCount);let peak=-Infinity,frequency=0;
  for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,100));analyser.getFloatFrequencyData(bins);
   for(let b=1;b<bins.length;b++)if(bins[b]>peak){peak=bins[b];frequency=b*context.sampleRate/analyser.fftSize;}}
  stream.getTracks().forEach(t=>t.stop());await context.close();return {peak,frequency,audio:tracks.length,ended:tracks[0].readyState};
 })()`,true);
 assert.equal(result.audio,1);assert.equal(result.ended,'ended');assert(result.peak>-80,JSON.stringify(result));
 assert(Math.abs(result.frequency-750)<20,JSON.stringify(result));console.log('SYSTEM_OUTPUT_TONE_PASS '+JSON.stringify(result)+' Electron '+process.versions.electron);
 clearTimeout(timer);app.exit(0);
}).catch(e=>{console.error(e);app.exit(1)});
'''
    script = script.replace('PROFILE', json.dumps(str(tmp_path / 'profile'))).replace('HANDLER', handler).replace('PAGE', json.dumps(str(page)))
    entry = tmp_path / 'main.cjs'
    entry.write_text(script)
    processes = []
    with (tmp_path / 'native.log').open('w') as log:
        try:
            if not xvfb:
                compositor = _native_popen(['wayfire', '-c', str(config)], env=env, stdout=log, stderr=log)
                processes.append(compositor)
                deadline = time.monotonic() + 30
                while not any(not p.name.endswith('.lock') for p in runtime.glob('wayland-*')):
                    assert compositor.poll() is None, (tmp_path / 'native.log').read_text()
                    assert time.monotonic() < deadline, 'isolated compositor timed out'
                    time.sleep(.05)
                env['WAYLAND_DISPLAY'] = next(p.name for p in runtime.glob('wayland-*') if not p.name.endswith('.lock'))
            readfd, writefd = os.pipe()
            try:
                command = ([xvfb, '-displayfd', str(writefd), '-screen', '0', '1280x800x24', '-nolisten', 'tcp', '-ac', '-noreset'] if xvfb else
                           ['Xwayland', '-displayfd', str(writefd), '-nolisten', 'tcp', '-ac', '-noreset', '-shm'])
                xserver = _native_popen(command, env=env, pass_fds=(writefd,), stdout=log, stderr=log)
                processes.append(xserver)
                os.close(writefd)
                writefd = None
                assert select.select([readfd], [], [], 10)[0], 'isolated display timed out'
                display = os.read(readfd, 32).decode().strip()
                assert display.isdigit()
            finally:
                os.close(readfd)
                if writefd is not None:
                    os.close(writefd)
            env.update(DISPLAY=':' + display, GDK_BACKEND='x11')
            pulse = _native_popen(['pulseaudio', '--daemonize=no', '--use-pid-file=no', '--exit-idle-time=-1', '--disable-shm=yes', '-n',
                                  '--load=module-native-protocol-unix socket=' + str(runtime / 'pulse-test.sock') + ' auth-anonymous=1',
                                  '--load=module-null-sink sink_name=fixture_output rate=48000 channels=2',
                                  '--load=module-null-source source_name=fixture_microphone rate=48000 channels=2'], env=env, stdout=log, stderr=log)
            processes.append(pulse)
            deadline = time.monotonic() + 10
            while not (runtime / 'pulse-test.sock').exists():
                assert pulse.poll() is None, (tmp_path / 'native.log').read_text()
                assert time.monotonic() < deadline, 'isolated audio server timed out'
                time.sleep(.05)
            for args in [('set-default-sink', 'fixture_output'), ('set-default-source', 'fixture_microphone'), ('set-sink-volume', 'fixture_output', '100%')]:
                subprocess.run(['pactl', *args], env=env, check=True, capture_output=True, timeout=5)
            player = _native_popen(['paplay', '--device=fixture_output', str(tone)], env=env, stdout=log, stderr=log)
            processes.append(player)
            native = _native_popen([str(electron), '--no-sandbox', '--disable-gpu', '--ozone-platform=x11', str(entry)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(native)
            stdout, stderr = native.communicate(timeout=25)
            (tmp_path / 'electron.log').write_text(stdout + '\n' + stderr)
            assert native.returncode == 0, stdout + '\n' + stderr
            assert 'SYSTEM_OUTPUT_TONE_PASS' in stdout
        finally:
            for process in reversed(processes):
                _stop_native(process)
