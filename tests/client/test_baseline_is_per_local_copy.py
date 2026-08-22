from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_executor_identifies_the_local_copy_when_reading_and_certifying_baseline():
    src = (ROOT / "static/js/client/syncexec.js").read_text(encoding="utf-8")
    assert "io.baselineComplete(key, o.id)" in src
    assert "io.markBaselineComplete(key, o.id)" in src


def test_persisted_baseline_is_bound_to_the_android_folder_handle():
    src = (ROOT / "static/js/client/sync.js").read_text(encoding="utf-8")
    assert "async function _loadBaseline(key, copy)" in src
    assert "String(v.copy || '') === String(copy || '')" in src
    assert "copy:String(copy || '')" in src


def test_old_unscoped_completion_cannot_make_a_recreated_folder_established():
    src = (ROOT / "static/js/client/sync.js").read_text(encoding="utf-8")
    body = src[src.index("async function _loadBaseline"):src.index("async function _saveBaseline")]
    assert "v.complete === true" in body
    assert "v.copy" in body
