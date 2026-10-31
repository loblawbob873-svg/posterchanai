# PBS — PosterChan Broadcast Studio

PBS is the broadcaster built into the PosterChan client. It is our own OBS. Its job is to make streaming on Nostr
one step: build a scene, press **Go Live**, and you are live on Nostr through your PosterChan instance. There is no
stream key to copy, no encoder to configure and no separate announce form.

Open it from the sidebar (**PBS Studio**), the phone's ☰ More sheet, the Android launcher tile, the desktop's start
menu, or **Go Live → Open PBS Studio**.

Code: `static/js/client/pbs.js` (the studio). The publish path lives in `static/js/client/streams.js`
(`_studioGoLive`, `_whipConnect`, `_endLive`). The server side is `app/routers/streams.py` (ingest, the WHIP proxy, and
the WHIP DELETE). The desktop side is `desktop/main.js` (the source picker, the hardware-encode switches, and
`pc:capture:info`).

## What it does

| Part | How |
|---|---|
| **Sources** | One scene with layers, listed bottom to top: screen or window capture, camera, image overlays and text overlays. You can drag a layer on the preview, resize it from its corner handle, set its opacity, move it up or down, hide it, or delete it. Text overlays have content, font, size, colour, an outline and a shadow. |
| **Compositor** | A `<canvas>` at the output size (720p by default, or 1080p) and frame rate (30 or 60 fps). `captureStream()` turns it into the video track. Frames are drawn on a **Worker clock**, so a backgrounded window keeps broadcasting. A page timer would drop to 1 Hz there. |
| **Audio** | WebAudio mixes two channels: the microphone (you pick the device) and the screen's own sound, where the platform provides it. Each channel goes through a gain into one `MediaStreamDestination`. Every channel has a volume fader, a mute button and a peak/RMS meter in dBFS: green up to −18, yellow up to −6, red above that. **The meters read after the fader**, so they show what viewers hear. |
| **Go Live** | Covered in the next section. |
| **Chat** | The stream's chat (kind 1311, plus reactions) appears in a panel beside the preview, and you can reply from there. It is the same chat code the stream page uses. |
| **Saved setup** | The scene, mixer, output settings and stream words are stored in one kind-30078 document, `d=pcai:pbs`, encrypted with NIP-44 to your own key. It follows your account to any device. |

## How it ties into the instance

When you press **Go Live**, PBS does the following:

1. It asks **your instance** for your ingest: `GET /api/streams/ingest` with your session. This returns your stream
   token, the instance's WHIP URL and the HLS URL viewers will play. The key never appears on screen.
2. It publishes the composited stream over **WHIP** to `/api/streams/whip/<token>`. The instance proxies the
   request to its own MediaMTX with your key. This is the same WHIP path the phone camera has always used (one
   shared function, `_whipConnect`).
3. Once the media server has accepted the stream, PBS snapshots the preview as a thumbnail and uploads it to your
   Blossom server. You can choose an image from your drive instead.
4. It signs and publishes the **NIP-53 kind-30311** event with `status=live`, `streaming=<the instance's HLS>`, and
   your title, description and tags. It also parks the "ended" sentinel with the server, and optionally posts to
   your followers with a watch link. This is the same machinery the Go Live sheet uses (`_publishLive`).
5. The status bar shows: connecting / LIVE / not live with the reason, the elapsed time, the outgoing video bitrate
   (from `getStats`), viewers, and the encoder.

When you press **End stream**, PBS:
* sends a **WHIP DELETE** for the session the server named. The proxy now returns
  `Location: /api/streams/whip/<token>/<session>` and forwards the DELETE to MediaMTX, so the path stops at once
  instead of when MediaMTX notices a dead PeerConnection;
* republishes the 30311 as `ended`.

The quick camera and screen paths in the Go Live sheet got the WHIP DELETE as well.

Every failure says why, and nothing is published on any failure path:

* no instance
* the account isn't allowed to stream
* streaming is turned off
* the instance is unreachable
* the media server is down
* WHIP is refused

The thumbnail is uploaded only after WHIP succeeds, so a failed attempt uploads nothing either.

**External encoder.** People who still use OBS, ffmpeg or Larix can find the RTMP server, stream key and stream URL
under **External encoder**, both in PBS and in the Go Live sheet. That section stays folded until it is needed.

## Upload size and hardware encoding

The goal is that **what leaves your machine is already small**.

* The video is negotiated as **H.264 first** (`setCodecPreferences`). MediaMTX can only remux H.264 to HLS, and H.264
  is the codec every GPU encodes in hardware: VA-API on Intel and AMD under Linux, NVENC, Media Foundation on
  Windows, VideoToolbox on macOS, and MediaCodec on Android.
* The video sender is capped with `setParameters` (`maxBitrate`, `maxFramerate`). The **Standard** preset is 1500
  kbps at 720p30, which equals the node's live-stream clamp ceiling (`stream_clamp_bitrate`/`_height`/`_fps`). At
  that preset the clamp has nothing to cut, so the stream is not encoded twice. **Saver** is 900 kbps and **High** is
  3000 kbps. Changing the preset while live re-caps the sender immediately.
* The canvas track carries `contentHint = 'detail'`. The encoder then keeps resolution, which keeps text overlays
  sharp.
* **Desktop app, Linux:** Chromium's VA-API encoder is off by default. `desktop/main.js` turns on
  `AcceleratedVideoEncoder` and `VaapiVideoEncoder` in the **single** `--enable-features` switch, next to
  `WebRTCPipeWireCapturer`. Chromium keeps only the last of a repeated switch, so a second switch would have killed
  Wayland screen capture. Windows and macOS encode H.264 in hardware by default, so nothing is added there.
* **The status bar reports the encoder that is actually running,** read from `getStats`
  (`outbound-rtp.encoderImplementation`, `powerEfficientEncoder`). It shows "hardware (VaapiVideoEncodeAccelerator)"
  or "software (OpenH264)". Some browsers report neither field and show "unknown".

WebCodecs `VideoEncoder` (`hardwareAcceleration: 'prefer-hardware'`) was considered and **not** used. WHIP carries a
WebRTC session, and WebRTC owns its encoder. A WebCodecs path would need a second transport, such as raw RTP or
WebTransport, that the instance doesn't have. WebRTC's H.264 path already uses the platform's hardware encoder
wherever the browser or Electron has one enabled, and the status bar shows whether it did.

## Platforms

| Where | Screen capture | System audio | Hardware encode |
|---|---|---|---|
| **Web browser** (desktop) | `getDisplayMedia`'s own picker: screen, window or tab | Chrome: tab and, on Windows/ChromeOS, system audio when ticked in the picker | Browser's choice (Chrome uses the GPU encoder where enabled) |
| **Desktop app — Windows** | Our picker (`desktopCapturer` + `setDisplayMediaRequestHandler`, screens and windows with thumbnails) | Yes (`audio: 'loopback'`) | Media Foundation, on by default |
| **Desktop app — macOS** | The system picker (macOS 15+ takes over) | **Not yet** — see the open questions | VideoToolbox, on by default |
| **Desktop app — Linux / PosterChanOS** | xdg-desktop-portal ScreenCast over PipeWire (the portal's own monitor chooser) | Yes (PulseAudio/PipeWire loopback) | VA-API, switched on by `main.js` |
| **Phone browser / Android app** | **None** — mobile browsers and the WebView have no `getDisplayMedia`; PBS says so | — | MediaCodec (H.264) |

**macOS Screen Recording.** If the permission is off, macOS hands the app a black capture or refuses it outright.
PBS asks the desktop app (`pcCapture.info()` → `systemPreferences.getMediaAccessStatus('screen')`). When the answer
is `denied`, PBS doesn't attempt a capture. It explains where to turn the permission on and shows a button that
opens **Privacy & Security → Screen Recording**.

**No instance** (a desktop or APK bundle used with relays only): PBS still composes, previews and saves your setup.
Go Live is disabled, with a note that streaming needs a PosterChan instance.

On **PosterChanOS**, PBS replaces OBS, which is no longer installed (`os/gentoo.sh`). PBS captures through the same
portal and PipeWire packages, so those stay.

## The saved document

* `d=pcai:pbs`, kind 30078, NIP-44 to self. It is registered in `store.js` `_isPinned` and in `app.js` `_CARRY_D`.
* Image overlays are referenced by **Blossom URL**. When there is nowhere to upload (no instance, or an upload
  refused), they are kept in this device's IndexedDB under an `idb:` id. They are **never inlined** into the
  document.
* A save is published only after a relay **answered** the read (`complete`, or `Relay.homeAnswered`). A read nobody
  answered, or a document that won't decrypt, is "could not read". PBS then uses the device copy, says so, and
  writes nothing over the account's setup. A relay that answered with no document means a new, empty studio.
  `tests/client/home_relay_write_gates_runtime.mjs` drives this rule.
* Live captures (screen, camera, microphone) belong to the session, not the document. After a reload, those layers
  show "not capturing" with a start button. Browsers require a click before a capture starts.

## Tests

Each of these fails without the feature it covers. See the commit message for the fail-first runs.

* `tests/client/test_pbs_compositor_full_app.py`: text and image overlays measured in the output pixels, drag,
  resize, opacity, camera, screen, layer order, hide, resolution and fps.
* `tests/client/test_pbs_audio_full_app.py`: mic selection, meters, per-source volume, mute and desktop audio.
* `tests/client/test_pbs_golive_full_app.py`: token fetch, WHIP with both tracks, the 30311 `live` with the HLS URL,
  `setParameters` caps, encoder reporting, chat, and Stop (DELETE plus `ended`).
* `tests/client/test_pbs_errors_full_app.py`: no instance, token refused, streaming off, unreachable, media server
  down, WHIP refused, and the macOS permission.
* `tests/client/test_pbs_persist_full_app.py`: save, reload in a fresh profile, and a failed read that never
  overwrites the saved setup.
* `tests/client/test_pbs_layout_full_app.py`: 390 px and 1280 px, the phone's no-screen note, sprite icons, field
  focus and the segmented controls.
* `tests/client/test_pbs_golive_sheet_full_app.py`: the Go Live sheet leads to PBS, OBS is folded under External
  encoder, and the shared WHIP path with DELETE.
* `tests/client/test_pbs_doc_rules.py`: the document rule, `normalize`, `encoderKind` and `dbOf`, under node.
* `tests/test_desktop_pbs_capture.py`: the Electron switches and `pc:capture:info`.
* `tests/test_stream_whip_teardown.py`: the server's WHIP Location and DELETE.
* `tests/test_posterchanos_profile.py`: OBS is no longer in the OS.

## Open questions

* RTMP or SRT **out** from PBS, for restreaming to Twitch or YouTube or to another server. The browser can't speak
  RTMP, so this would need either a server-side relay (the instance re-pushes the HLS/RTSP path) or the desktop
  app's bundled ffmpeg.
* macOS system audio. Electron's `audio: 'loopback'` on macOS needs ScreenCaptureKit (macOS 13+) and a Chromium
  feature flag. The current handler keeps macOS video-only, because a wrong grant fails the whole capture. This
  needs a test on a Mac.
* Multiple scenes with hotkeys or a scene switcher. The document already stores a list of sources and can grow a
  list of scenes.
