# Updating after the Android signing-key rotation

PosterChan rotated its Android signing key after the former key was exposed. The
old private key is not used for releases or loaded into CI. New APKs are signed
only by the current key and carry Android's signed proof-of-rotation from the
former certificate to the current certificate.

## Android 9 and newer (API 28+)

Zapstore publishes only `posterchan.apk`, the API 28+ artifact signed by the
current key with the proof-of-rotation embedded. If Zapstore's metadata
preflight accepts the update, update normally. If it reports a certificate
mismatch, download `posterchan.apk` from the official GitHub release and open it
over the existing installation; Android itself then verifies the cryptographic
lineage. Do not use the Android 8 reinstall artifact and do not uninstall:
uninstalling is unnecessary on Android 9+ and discards local app data.

## Android 8 and 8.1 (API 26–27)

Android 8 cannot verify APK Signature Scheme v3 proof-of-rotation. A device with
an older PosterChan signed by the retired key therefore cannot install a current
release over that installation. Continuing to sign Android 8 updates with the
exposed private key would let anyone holding that key impersonate a release, so
PosterChan does not offer a transparent in-place upgrade on these versions.

Before migrating, open PosterChan and export or copy out anything stored only on
the device, including any local account key, settings, downloads, drafts, and
unsynced files. Verify that the export can be opened somewhere safe. Then:

1. Uninstall the old PosterChan installation.
2. Download and install `posterchan-android8-reinstall.apk` from the official
   GitHub release. Zapstore publishes only the Android 9+ lineage artifact.
3. Import the saved data and sign in again.

Uninstalling erases PosterChan's private app data. If the old app cannot be
opened long enough to export it, do not uninstall until the data-recovery choice
is understood; there is no cryptographically safe in-place APK migration for
API 26–27 after retiring the exposed signer.
