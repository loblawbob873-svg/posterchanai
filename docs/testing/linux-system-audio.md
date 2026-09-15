# Linux remote desktop system audio

The Electron display grant returns `audio: 'loopback'` on Linux and Windows
only when the page requests audio. Linux captures the output monitor through
PulseAudio, including PipeWire's PulseAudio compatibility server. It does not
request a microphone. Remote Desktop reports whether an actual audio track was
returned; backend failure must not be advertised as sound sharing.

The pinned Electron 44 build uses Chromium 152.0.7977.54. Its
[display callback](https://github.com/electron/electron/blob/v44.0.0/shell/browser/electron_browser_context.cc)
accepts loopback on Linux, and Chromium's
[PulseAudio manager](https://chromium.googlesource.com/chromium/src/+/refs/tags/152.0.7977.54/media/audio/pulse/audio_manager_pulse.cc)
creates `PulseLoopbackManager` for the loopback device. The old
`PulseaudioLoopbackForScreenShare` flag is unnecessary in this version.
The Electron session documentation still describes this option as Windows-only;
the pinned implementation and native regression establish this Linux behavior.

Run the native regression with the installed pinned binary:

```sh
PC_ELECTRON_BINARY=/path/to/electron python -m pytest -q tests/test_desktop_linux_loopback_runtime.py
```

The test runs the shipped permission/display handler in real Electron, with an
isolated Wayfire/Xwayland display and private PulseAudio daemon. It plays 750 Hz
only to an output sink, sets a separate silent source as the default microphone,
and verifies the captured frequency, track count, and stopped track. It never
captures the machine's normal audio server. Missing native prerequisites skip
unless `PC_REQUIRE_NATIVE_IPC_TEST=1` requires them. The deterministic display
handler tests separately cover audio opt-in, platforms, cancellation and an
origin change while the picker is open.

This proves native output capture with Electron 44's PulseAudio backend. A
physical PipeWire desktop-to-phone session, its speaker volume, and network
transport still require device verification; this fixture does not claim those.
