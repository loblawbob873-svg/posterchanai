# LiveKit client frame encryption

Vendored unmodified `dist/livekit-client.umd.js`, `dist/livekit-client.e2ee.worker.js`,
and `LICENSE` from the npm `livekit-client@2.19.2` package. Apache-2.0 license.

Concord calls use a per-participant BaseKeyProvider with keySize256,
sharedKey=false, ratchetWindowSize0 and failureTolerance-1. CORD-07 sender material
is imported as HKDF key material, matching Armada's LiveKit frame layer.
2.19.2 because that is the version Armada's lockfile ships (soapbox-pub/armada package-lock.json,
2026-10-22): both clients must produce the same encrypted frames. Upgrade together.

Upstream: https://github.com/livekit/client-sdk-js/tree/v2.19.2
f9e5289f11fe0a8f47245f041202fe85af8d2bf76a2e12b4bb3e19449464ba09  livekit-client.e2ee.worker.js
2e8fd28afad004dcad97c0eb124d4d28ce5437205a881f533f2667960de83990  livekit-client.umd.js
