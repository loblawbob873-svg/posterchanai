"""A root-run install for another account (--service-user) leaves that account owning what it created.

2026-10-10 E2E on a fresh Debian (`./install.sh --nostr-only --service-user pc`, run as root): install rc=0 and the
service came up, but scripts/init_instance_key.py and the settings store had run as ROOT -- data/keys.json(.lock)
and /var/lib/posterchanai/local_settings.json(.lock) were root-owned, so the service could not read its operator key:
no table ever loaded and no post reached the relay, with the app running on regardless. SearXNG was cloned into
/root/searxng, which the service cannot read, and crash-looped. Runs the shipped helper under bash (id/chown stubbed)
and checks it is called before the service first starts.
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTILS = os.path.join(ROOT, "scripts", "install", "utils.sh")


def _run(tmp_path, uid, user):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "local_settings.json").write_text("{}")
    (state / "local_settings.json.lock").write_text("")
    log = tmp_path / "chown.log"
    script = """
        id() { case "$1" in -u) echo %s ;; -gn) echo pcgroup ;; *) command id "$@" ;; esac; }
        chown() { echo "$@" >> %s; }
        source %s
        PC_SERVICE_USER=%s POSTERCHANAI_STATE_DIR=%s give_state_to_service_user /extra/searxng
    """ % (uid, log, UTILS, user, state)
    (tmp_path / "repo").mkdir()
    subprocess.run(["bash", "-c", script], cwd=tmp_path / "repo", check=True, timeout=30)
    return log.read_text().splitlines() if log.exists() else [], tmp_path


def test_a_root_run_install_gives_the_service_user_its_state(tmp_path):
    calls, base = _run(tmp_path, 0, "pc")
    joined = "\n".join(calls)
    assert "-R pc:pcgroup %s" % (base / "repo") in joined, calls          # the checkout: data/keys.json etc.
    assert "local_settings.json" in joined and "local_settings.json.lock" in joined, calls


def test_it_does_nothing_when_not_root_or_when_the_service_is_root(tmp_path):
    assert _run(tmp_path / "a", 1000, "pc")[0] == []
    assert _run(tmp_path / "b", 0, "root")[0] == []


def test_it_runs_before_the_service_first_starts_and_searxng_lives_in_the_service_home():
    src = open(os.path.join(ROOT, "install.sh")).read()
    body = src[src.index("install_nostr_only() {"):]
    body = body[:body.index("\n}\n")]
    assert body.index("give_state_to_service_user") < body.index("setup_systemd"), \
        "the service starts before it owns its state"
    main = src[src.index("main() {"):]
    assert main.index("give_state_to_service_user") < main.index("setup_systemd")
    sx = open(os.path.join(ROOT, "scripts", "install", "searxng.sh")).read()
    assert re.search(r'src_dir="\$\{SEARXNG_SRC_DIR:-\$\(service_home', sx), "SearXNG still clones into $HOME"
