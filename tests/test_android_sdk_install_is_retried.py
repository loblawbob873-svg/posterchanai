"""The emulator workflow's Android SDK install gets a second attempt.

A corrupt SDK download ("Error on ZipFile unknown archive", 2026-10-26) failed the job before any device test ran --
the APK build waits on that verdict, so one flaky download held back the release and its Zapstore listing.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_the_sdk_install_is_retried_once_and_the_retry_only_runs_after_a_failure():
    wf = yaml.safe_load((ROOT / ".github/workflows/android-emulator.yml").read_text())
    steps = [st for job in wf["jobs"].values() for st in job.get("steps", [])]
    installs = [st for st in steps if str(st.get("uses", "")).startswith("android-actions/setup-android")]
    assert len(installs) >= 2, "the SDK install has no retry"
    first, second = installs[0], installs[1]
    assert first.get("continue-on-error") is True and first.get("id"), first
    assert second.get("if") == f"steps.{first['id']}.outcome == 'failure'", second
    assert second.get("with", {}).get("packages") == first.get("with", {}).get("packages"), (first, second)
    assert "continue-on-error" not in second, "the retry must fail the job if it fails too"
