"""scripts/verify_live_signin.py: a real sign-in on production after every deploy (2026-10-22). "Could not run" is
never a pass, and the deploy fails loudly only on a real failure."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("clj", ROOT / "scripts" / "verify_live_signin.py")
clj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clj)


def test_no_url_no_key_or_no_chrome_is_could_not_run(monkeypatch, tmp_path):
    assert clj.main(["x"]) == 2
    monkeypatch.setattr(clj, "KEY_FILE", tmp_path / "absent.key")
    assert clj.main(["x", "https://poster.place"]) == 2
    bad = tmp_path / "bad.key"
    bad.write_text("not-a-key")
    monkeypatch.setattr(clj, "KEY_FILE", bad)
    assert clj.main(["x", "https://poster.place"]) == 2
    good = tmp_path / "good.key"
    good.write_text("1" * 64)
    monkeypatch.setattr(clj, "KEY_FILE", good)
    monkeypatch.setattr(clj, "CHROME", str(tmp_path / "no-chrome"))
    assert clj.main(["x", "https://poster.place"]) == 2


def _with(monkeypatch, tmp_path, results):
    k = tmp_path / "k.key"
    k.write_text("1" * 64)
    monkeypatch.setattr(clj, "KEY_FILE", k)
    monkeypatch.setattr(clj, "CHROME", __file__)            # exists; journey() is replaced below

    async def fake(base, sk):
        return results
    monkeypatch.setattr(clj, "journey", fake)
    return clj.main(["x", "https://poster.place"])


def test_any_failed_step_fails_and_all_passing_passes(monkeypatch, tmp_path):
    ok = [("login", clj.PASS, 1.0, ""), ("console", clj.PASS, 0, "clean")]
    assert _with(monkeypatch, tmp_path, ok) == 0
    assert _with(monkeypatch, tmp_path, ok + [("telegram", clj.FAIL, 40, "still CONNECTING")]) == 1
    assert _with(monkeypatch, tmp_path, [("login", clj.SKIP, 0, "Chrome did not start")]) == 2


def test_sync_fails_the_deploy_only_on_a_real_failure():
    sync = (ROOT / "sync.sh").read_text()
    block = sync[sync.index('echo "[sync] signing in to'):]
    block = block[:block.index("sign-in verified")]
    assert 'if [ "$_JOURNEY_RC" = "1" ]' in block and "exit 1" in block
    assert block.index('elif [ "$_JOURNEY_RC" = "2" ]') > block.index("exit 1"), "a could-not-run must not fail the deploy"
    assert sync.index('echo "[sync] production verified"') < sync.index('echo "[sync] signing in to')


def test_the_key_never_lives_in_the_repo():
    assert ".config" in str(clj.KEY_FILE) and str(ROOT) not in str(clj.KEY_FILE)


def test_which_console_lines_mean_the_page_is_broken():
    """The first live run failed on two lines that are not this site being broken (a third-party relay that does not
    resolve, and the boot's own signed-out probe) -- and those filters must not swallow our own failures."""
    from scripts.verify_live_signin import counts
    host = "poster.place"
    third = ("browser:network: WebSocket connection to 'wss://relay.0xchat.com/' failed: net::ERR_NAME_NOT_RESOLVED "
             "https://poster.place/static/js/client/relay.js?v=1")
    assert not counts("browser:network", third, host, True)
    probe = "Failed to load resource: the server responded with a status of 401 (Unauthorized) https://poster.place/api/auth/me"
    assert not counts("browser:network", probe, host, False), "the signed-out probe before sign-in"
    assert counts("browser:network", probe, host, True), "a 401 on our own session AFTER sign-in is a real failure"
    ticket = "Failed to load resource: the server responded with a status of 401 (Unauthorized) https://poster.place/client/concord/tunnel-ticket"
    assert counts("browser:network", ticket, host, True)
    ours = ("WebSocket connection to 'wss://poster.place/ws/concord-relay?u=wss%3A%2F%2Frelay.dreamith.to&t=' failed: "
            "Unexpected response code: 403 https://poster.place/static/js/client/relay.js?v=1")
    assert counts("browser:network", ours, host, True), "our own tunnel refusing is ours, whatever relay it was for"
    assert counts("console.error", "TypeError: x is undefined", host, True)
    assert not counts("console.error", "ResizeObserver loop limit exceeded", host, True)
