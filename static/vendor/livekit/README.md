# LiveKit client frame encryption

Vendored unmodified `dist/livekit-client.umd.js`, `dist/livekit-client.e2ee.worker.js`,
and `LICENSE` from the npm `livekit-client@2.17.2` package. Apache-2.0 license.

Concord calls use a per-participant BaseKeyProvider with keySize256,
sharedKey=false, ratchetWindowSize0 and failureTolerance-1. CORD-07 sender material
is imported as HKDF key material, matching Armada's LiveKit frame layer.

Upstream: https://github.com/livekit/client-sdk-js/tree/v2.17.2
b8c6ba76d6d29c890b2a7dfd1ee06a5747bb198a74c43c8887265d0e075fe708  livekit-client.e2ee.worker.js
800b013b0a0c9f8b8194d76225bf3dc9837028739fa641215068b112627edd15  livekit-client.umd.js
