import importlib.util
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "scripts/apk_embedded_build.py"
    spec = importlib.util.spec_from_file_location("apk_embedded_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apk_version_is_read_from_the_artifact_not_release_notes(tmp_path):
    apk = tmp_path / "posterchan.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/public/index.html", "<script>window.__PC_APP_BUILD__ = 1694;</script>")
    assert _module().embedded_build(str(apk)) == 1694


def test_mirror_is_atomic_and_refuses_downgrades():
    script = (ROOT / "scripts/refresh_apk.sh").read_text()
    assert "apk_embedded_build.py" in script
    assert 'if [ "$build" -lt "$current" ]' in script
    assert 'mv -f -- "$tmp" "$DEST/posterchan.apk"' in script
    assert "Version 1\\.0" not in script


def test_deploy_uses_the_versioned_source_controlled_mirror_script():
    sync = (ROOT / "sync.sh").read_text()
    # A rolling GitHub release can briefly serve the replaced asset from a stale CDN node. A single
    # delayed fetch reproduced /apk remaining four builds behind; retries must outlive that window.
    assert sync.count("./scripts/refresh_apk.sh") >= 3
    assert "sleep 240" in sync and "sleep 120" in sync
    assert "/home/verita84/posterchan-apk/refresh.sh" not in sync


def _wait(tmp_path, answers, **env):
    """Run the shipped scripts/wait_apk_build.sh with a stub `gh` answering `answers` in turn."""
    import os
    import subprocess
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "answers").write_text("\n".join(answers) + "\n")
    (bin_ / "gh").write_text('#!/bin/bash\nf=%s\nline=$(head -1 "$f"); sed -i 1d "$f"; '
                             'echo "$*" >> %s; printf "%%s\\n" "$line"\n'
                             % (tmp_path / "answers", tmp_path / "calls"))
    (bin_ / "gh").chmod(0o755)
    e = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", PC_APK_POLL="0", PC_APK_NONE="3", PC_APK_LIMIT="6")
    e.update(env)
    r = subprocess.run(["bash", str(ROOT / "scripts/wait_apk_build.sh"), "abc123def"], env=e,
                       capture_output=True, text=True, timeout=30)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return r.returncode, r.stdout, calls


def test_the_mirror_waits_for_this_commits_build_through_the_emulator_checks(tmp_path):
    """Deploy 104: the APK waited ~40 min on the emulator checks, the fixed-delay refreshes all fetched the
    PREVIOUS build, and /apk served 2469 while 2472 was published. It must wait for THIS commit's run."""
    rc, out, calls = _wait(tmp_path, ["queued null", "in_progress null", "in_progress null", "completed success"])
    assert rc == 0 and "built" in out, out
    assert len(calls) == 4 and all("--commit abc123def" in c and "android.yml" in c for c in calls), calls


def test_a_failed_build_is_never_mirrored(tmp_path):
    rc, out, _ = _wait(tmp_path, ["in_progress null", "completed failure"])
    assert rc == 1 and "failure" in out, out


def test_a_deploy_with_no_android_build_does_not_wait_the_full_hour(tmp_path):
    rc, out, calls = _wait(tmp_path, ["", "", "", "", ""])
    assert rc == 0 and "no Android build" in out and len(calls) == 3, (out, calls)


def test_sync_waits_for_the_build_before_the_first_refresh():
    sync = (ROOT / "sync.sh").read_text()
    block = sync[sync.index("./scripts/wait_apk_build.sh"):]
    assert block.index("./scripts/wait_apk_build.sh") < block.index("./scripts/refresh_apk.sh")
