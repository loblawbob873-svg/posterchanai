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
    assert "./scripts/refresh_apk.sh" in sync
    assert "/home/verita84/posterchan-apk/refresh.sh" not in sync
