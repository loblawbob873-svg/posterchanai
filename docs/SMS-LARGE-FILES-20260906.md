# SMS large-file regression repair

Android's native Texts picker copied files into a byte array with a 12 MiB limit before the existing Blossom-link route ran. The web client also hid upload failures and handed oversized videos to MMS, which could only reject them.

The native picker now streams content URIs into durable private drafts off the UI thread. Sending chooses the link route from the file's metadata before any MMS byte-array read. Preview decoding is sampled. Files above the carrier video ceiling, above MMS staging capacity, and non-MMS documents use an encrypted link. The web client uses the same link route; only resizable photos within the native staging bound may fall back to MMS if Blossom is offline.

Large link attachments use independently AES-GCM-authenticated chunks no larger than 4 MiB including overhead. A separately encrypted manifest records ordered hashes and plaintext lengths. The random key and chunk-format flag remain in the URL fragment. The public `/f/` page validates manifest lengths and chunk hashes, decrypts sequentially, and offers the original file without requiring an account. Existing single-blob links remain supported; other shared-upload callers retain their existing format.

Review: uploads finish before any SMS is sent; a failed chunk never produces a text link. Temporary native ciphertext is removed on success and failure. Native drafts survive interrupted staging. Draft removal/replacement is blocked during active sends, and completion does not delete a newer draft from another Activity. Sync uploads retain authenticated hash commitments and keep/no-mirror headers.

Validation includes a 25 MiB native file under a 24 MiB JVM heap, independent decryption and full-content hash comparisons for native and actual JavaScript uploaders, failure of the second upload, changed media-server rejection, local-radio and remote-web send decisions, old-link compatibility, and real-browser download reconstruction/missing-chunk/invalid-manifest cases. Android content-URI staging and interrupted-copy tests are included in the emulator suite. No SMS was sent to a real contact during automated testing.

Limits: uploads still require connectivity, storage quota, and a Blossom server accepting 4 MiB blobs. Files are limited to 4096 chunks. Current desktop Chromium uses its disk-backed File System Access picker; older builds without that API retain their bounded IPC fallback. Browser file inputs and native Android use the new large-file path. Interrupted uploads can leave retained encrypted chunks on Blossom, as existing failed shared uploads can; no plaintext or decryption key is uploaded.

## Release validation

The full backend rerun passed 7,600 tests and 520 subtests (19 skipped). The full client run passed 3,455 tests and 121 subtests (one skipped). The original backend run found three obsolete picker source assertions and a pre-existing installer test timing race; those test corrections are committed, and the entire backend was rerun. Raw first-run failures are retained in the local release logs.

The release runner passed 54 additional checks, including signer transport, interactive/bulk recovery, Jellyfin compatibility, moved media reconciliation, share-link reconstruction and Texts media. Its 22 environment-dependent skips are not passes; these require attached installed desktops, VM/ISO images, services or temporary account registration. The standalone signer reconnect retry passed. The existing CSS-scale report remains advisory.

Android and desktop generated bundles each passed all 42 wallet presentation tests. The native Java and instrumentation sources compiled. The first Android emulator run (34067306678) lost the emulator before instrumentation executed and is not device-test evidence; a fresh runner retry is required before release. No real SMS was sent by these checks.

The independently committed installer change cf9924391 is preserved by merge; its 113 tests and 52 subtests passed after integration.

The fresh Android run [34068504014](https://github.com/loblawbob873-svg/posterchanai/actions/runs/34068504014) passed both lifecycle and instrumentation gates. Its downloaded XML report contains **90 tests, zero failures, errors or skips**, including large-video content-URI staging and interrupted-copy draft preservation. The APK web-asset provenance check passed. This resolves the device gate above; it does not claim physical handset/carrier verification.

## Published build verification

Deployment 772ec237 reached the main server, NAS and router. The NAS relay fetch failed during the first attempt; it was fast-forwarded from a verified Git bundle and its application service restarted. Both application nodes are active. Public app.js, sms.js, sw.js and exodus.css match the reviewed source exactly; the public download reader serves the chunk format.

Android 1.0.2214 is published at `/apk`; its public APK SHA-256 is `039c6ab4128b703900073b31822016be56cea04705513d260f3cca6090915cd0`. Its app.js, sms.js and wallet CSS match source. Desktop 1.0.1496's immutable archive passes its published SHA-512 checksum. The extracted desktop bundle passed all 42 wallet UI tests and its document workspace checks. Tests against the public Android wallet/uploader and desktop picker passed 49 cases. The OS package pin is updated to desktop 1.0.1496 after its nine pin checks passed.

The post-deployment Monero check found the reporting account with 12 unlocked unspent outputs, no pending outgoing transfer and zero blocks remaining to unlock. No additional live payments or real SMS were sent by this release validation.
