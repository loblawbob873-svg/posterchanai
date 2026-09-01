from pathlib import Path


BUILD = (Path(__file__).parents[1] / "mobile/build-www.sh").read_text(encoding="utf-8")


def test_each_apk_build_gets_a_distinct_shell_cache_without_clearing_user_data():
    assert "-apk" + '" + build + "' in BUILD
    assert "could not stamp APK service-worker cache" in BUILD
    assert "deleteAllData" not in BUILD


def test_native_shell_reloads_once_when_new_worker_claims_first_launch():
    assert "navigator.serviceWorker.addEventListener('controllerchange'" in BUILD
    assert "if (changed) return; changed = true" in BUILD
    assert "location.reload()" in BUILD
