"""Regression guards for Android release-signing key hygiene."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_release_keystore_is_not_present_in_the_source_tree():
    forbidden = list(ROOT.glob("mobile/**/*.keystore")) + list(ROOT.glob("mobile/**/*.jks"))
    assert not forbidden, f"private Android signing key material is present: {forbidden}"


def test_tracked_signing_secret_guard_passes():
    subprocess.run(
        ["python3", "scripts/check_no_android_signing_secrets.py"],
        cwd=ROOT,
        check=True,
    )


def test_gradle_reads_release_credentials_from_the_environment():
    gradle = (ROOT / "mobile/android/app/build.gradle").read_text()
    for name in (
        "POSTERCHAN_ANDROID_KEYSTORE",
        "POSTERCHAN_ANDROID_STORE_PASSWORD",
        "POSTERCHAN_ANDROID_KEY_ALIAS",
        "POSTERCHAN_ANDROID_KEY_PASSWORD",
    ):
        assert f"System.getenv('{name}')" in gradle

    assert 'storePassword "' not in gradle
    assert 'keyPassword "' not in gradle


def test_workflow_uses_github_secrets_for_signing():
    workflow = (ROOT / ".github/workflows/android.yml").read_text()
    for name in (
        "ANDROID_KEYSTORE_BASE64",
        "ANDROID_STORE_PASSWORD",
        "ANDROID_KEY_ALIAS",
        "ANDROID_KEY_PASSWORD",
        "ANDROID_SIGNING_CERT_SHA256",
        "ANDROID_SIGNING_LINEAGE_BASE64",
    ):
        assert f"secrets.{name}" in workflow

    for retired_name in (
        "ANDROID_OLD_KEYSTORE_BASE64",
        "ANDROID_OLD_STORE_PASSWORD",
        "ANDROID_OLD_KEY_ALIAS",
        "ANDROID_OLD_KEY_PASSWORD",
        "POSTERCHAN_ANDROID_OLD_KEYSTORE",
    ):
        assert retired_name not in workflow

    assert "posterchan-release.keystore -srcstorepass posterchan" not in workflow
    assert "KEYSTORE_PASSWORD=posterchan" not in workflow
    assert '$APKSIGNER verify --min-sdk-version 28 --verbose --print-certs' in workflow
    assert '$APKSIGNER sign' in workflow
    assert '--lineage "$RUNNER_TEMP/signing-lineage"' in workflow
    assert '--ks "$POSTERCHAN_ANDROID_KEYSTORE"' in workflow
    assert "--next-signer" not in workflow
    assert 'test "$ACTUAL_CERT" = "$EXPECTED_CERT"' in workflow
    assert '--rotation-min-sdk-version 28' in workflow
    assert '--v1-signing-enabled false --v2-signing-enabled false' in workflow
    assert '--out "$RUNNER_TEMP/posterchan-api28.apk" "$UNSIGNED_APK"' in workflow
    assert '--out "$RUNNER_TEMP/posterchan-android8.apk" "$UNSIGNED_APK"' in workflow
    assert '--min-sdk-version 26 --max-sdk-version 27 --verbose --print-certs' in workflow
    assert "apksigner rejected the rotated APK for Android 9+" in workflow
    assert "apksigner rejected the current-signer APK for an Android 8 fresh install" in workflow
    assert "EDDF3A7983DF49221A5ACE0D0CA52C899D34EB88A4155B0829B05C0AFC31F342" not in workflow
    assert 'cp "$ANDROID8_APK" posterchan-android8-reinstall.apk' in workflow
    assert "posterchan-android8-reinstall.apk" in workflow
    assert "POSTERCHAN_ANDROID_KEYSTORE='' POSTERCHAN_ANDROID_STORE_PASSWORD=''" in workflow


def test_android_8_migration_does_not_promise_an_unsafe_in_place_update():
    recovery = " ".join(
        (ROOT / "docs/ZAPSTORE_SIGNING_RECOVERY.md").read_text().split()
    )
    assert "Android 8 and 8.1 (API 26–27)" in recovery
    assert "cannot install a current release over that installation" in recovery
    assert "export or copy out anything stored only on the device" in recovery
    assert "Uninstall the old PosterChan installation" in recovery
    assert "posterchan-android8-reinstall.apk" in recovery
    assert "Zapstore publishes only `posterchan.apk`, the API 28+ artifact" in recovery
    assert "Import the saved data and sign in again" in recovery
    assert "no cryptographically safe in-place APK migration" in recovery


def test_workflow_builds_distinct_lineage_and_android_8_artifacts():
    workflow = (ROOT / ".github/workflows/android.yml").read_text()
    first = workflow.index("          $APKSIGNER sign \\")
    second = workflow.index("          $APKSIGNER sign \\", first + 1)
    verification = workflow.index("          API28_APK=", second)
    api28_sign = workflow[first:second]
    android8_sign = workflow[second:verification]

    assert '--min-sdk-version 28' in api28_sign
    assert '--lineage "$RUNNER_TEMP/signing-lineage"' in api28_sign
    assert '--v1-signing-enabled false --v2-signing-enabled false' in api28_sign
    assert '--min-sdk-version 26' in android8_sign
    assert "--lineage" not in android8_sign
    assert '--v1-signing-enabled true --v2-signing-enabled true' in android8_sign

    release_files = workflow[workflow.index("          files: |"):]
    assert "            posterchan.apk" in release_files
    assert "            posterchan-android8-reinstall.apk" in release_files
