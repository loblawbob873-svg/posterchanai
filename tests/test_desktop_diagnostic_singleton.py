import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv, env):
    script = """const d=require('./desktop/diagnostic');
try{console.log(JSON.stringify(d.resolve(JSON.parse(process.argv[1]),JSON.parse(process.argv[2]))));}
catch(e){console.error(e.message);process.exit(2)}"""
    return subprocess.run(["node", "-e", script, json.dumps(argv), json.dumps(env)],
                          cwd=ROOT, text=True, capture_output=True)


def test_exact_private_domain_is_accepted_and_cannot_name_canonical_paths():
    token = "abc123diagnostic"
    root = f"/tmp/pc-installed-diagnostic.{token}"
    socket = f"{root}/runtime/sway-ipc.42.sock"
    result = run([f"--pc-diagnostic-token={token}", f"--pc-diagnostic-profile={root}/profile",
                  f"--pc-diagnostic-swaysock={socket}"],
                 {"PC_DIAGNOSTIC_TOKEN": token, "XDG_RUNTIME_DIR": f"{root}/runtime", "SWAYSOCK": socket})
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved["profile"].startswith("/tmp/pc-installed-diagnostic.")
    assert ".config/posterchan-desktop" not in resolved["profile"]


def test_partial_or_canonical_targeting_is_fail_closed():
    token = "abc123diagnostic"
    root = f"/tmp/pc-installed-diagnostic.{token}"
    good = [f"--pc-diagnostic-token={token}", f"--pc-diagnostic-profile={root}/profile",
            f"--pc-diagnostic-swaysock={root}/runtime/sway.sock"]
    env = {"PC_DIAGNOSTIC_TOKEN": token, "XDG_RUNTIME_DIR": f"{root}/runtime",
           "SWAYSOCK": f"{root}/runtime/sway.sock"}
    cases = [good[:1],
             [good[0], "--pc-diagnostic-profile=/home/user/.config/posterchan-desktop", good[2]],
             good]
    environments = [env, env, {**env, "SWAYSOCK": "/run/user/1000/sway-ipc.canonical.sock"}]
    for argv, case_env in zip(cases, environments):
        result = run(argv, case_env)
        assert result.returncode == 2


def test_user_data_is_changed_before_single_instance_lock():
    source = (ROOT / "desktop/main.js").read_text()
    assert source.index("app.setPath('userData', diagnostic.profile)") < source.index("app.requestSingleInstanceLock()")
