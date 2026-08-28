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

    assert "posterchan-release.keystore -srcstorepass posterchan" not in workflow
    assert "KEYSTORE_PASSWORD=posterchan" not in workflow
    assert '$APKSIGNER verify --print-certs' in workflow
    assert '$APKSIGNER sign' in workflow
    assert '--lineage "$RUNNER_TEMP/signing-lineage"' in workflow
    assert 'test "$ACTUAL_CERT" = "$EXPECTED_CERT"' in workflow
