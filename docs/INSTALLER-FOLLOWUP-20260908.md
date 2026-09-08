# Installer follow-up — 2026-09-08

**Owner: the installer agent.** These findings require installer integration and install/reboot/ISO validation. The shared `os/gentoo.sh` was not edited by this review; neither the installer fixes nor a corrected ISO are claimed deployed.

## Five confirmed findings

1. **Optional repository setup leaves `_in` undefined.** Define the helper before consumers that can run without the optional PosterChan repository configuration.
2. **Cached downloads bypass payload validation.** Validate existing tar/AppImage bytes before selection; a cached HTML response must trigger the same fallback as an invalid new download.
3. **Repeated keyword setup can overwrite operator configuration.** Preserve an existing regular keyword file as `package.accept_keywords/00-local`; atomically replace only `posterchan-managed`. Preserve other directory entries. Reject a symlinked keyword file without modifying it, leaving resolution to the operator.
4. **Scratch fixture containment uses an unsafe string prefix.** Compare resolved paths; reject sibling-prefix traversal and symlink escape.
5. **Clean ISO rules discard Firefox's executable payload.** The fresh desktop at `192.168.0.102` had `/usr/bin/firefox-bin` and its desktop entry, but the entire `/opt/firefox` tree was missing. The wrapper failed at line 111. The clean-liveCD rule excludes every `/opt/*` except `/opt/posterchan`; installation then copies that incomplete image. Root repaired this particular desktop by emerging Firefox **155.0.1**. That repair does not fix the ISO generator.

For Firefox, preserve the required `/opt/firefox` payload while retaining exclusions for private build-host SDK/server directories. Audit required binary packages against Portage `CONTENTS`. Add final-image `unsquashfs` checks for the Firefox executable and `libxul.so`, followed by `firefox-bin --version` and the installed-image reboot gate. Test a required Firefox payload beside an unrelated private `/opt` sibling: only the required payload should survive.

## Prepared patches and evidence

The isolated review clone is `/tmp/pc-installer-findings-review-20260908`, based on `252b43ffef856301de89e203c5538afaf660b6a4`.

Durable review copies are in [installer-review-20260908](installer-review-20260908/).
These are unapplied patch artifacts, not changes to the installer. The installer owner should
review them against their current work before applying the four-findings patch followed by the
Firefox patch. The `/tmp` paths below identify the original validation artifacts.

- `/tmp/pc-installer-four-findings.patch` addresses findings 1–4 in `os/gentoo.sh`, `scripts/check_scratch_install_vm.py`, and new `tests/test_installer_followup_regressions.py`. Fourteen runtime cases use temporary paths and mocked downloads/chroot: original source **11 failed / 3 passed**; patched source **14 passed**. No packages or host `/etc` are modified by these tests.
- `/tmp/pc-session-switch-parity.patch` is a **separate helper parity change already integrated in `f8aac8f4b`**; do not apply it again. It aligns `os/bin/pc-session-switch` with the packaged helper reviewed in `5650237b7`, retaining authentication/provisioning checks and avoiding restart only when both identity and console autologin user already match. It includes eight runtime cases across both copies. Adjacent checks passed **139 tests, 52 subtests**.
- The combined isolated adjacent suite passed **197 tests, 58 subtests**. Logs: `/tmp/pc-installer-findings-red.log`, `/tmp/pc-session-switch-parity-tests.log`, and `/tmp/pc-installer-findings-parity-green.log`. Patch reverse-applicability checks, shell syntax checks, and `git diff --check` passed.
- `/tmp/pc-installer-firefox-payload.patch` separately addresses finding 5: it preserves the two exact required `/opt` trees and checks the packed squashfs for the launcher, browser executable, and `libxul.so`. Both new regression tests fail on the original source; **16 combined Firefox and four-finding tests pass** in the isolated clone. This patch remains uncommitted for installer-owner integration.
- The shared `scripts/check_installed_os_core_package.sh` requires a successful, bounded `firefox-bin --version` with Mozilla Firefox version output. Runtime fixtures cover a healthy installation, missing payload, missing library, and a false-success wrapper. It also checks the current Wayfire login entrypoint; **four targeted tests and the full installed core-package check passed**. This check opens no browser or profile.

These targeted results do not replace installer-owned fresh-install, reboot, and final ISO validation. Source handoffs: `/tmp/pc-installer-findings-handoff.md`, `/tmp/pc-firefox-installer-handoff.md`; earlier location details: `/tmp/pc-installer-followup-findings.md`.
