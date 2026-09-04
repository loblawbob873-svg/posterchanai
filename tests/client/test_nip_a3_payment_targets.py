"""NIP-A3 Lightning payment-target discovery and zap routing."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_payment_target_runtime():
    result = subprocess.run(
        ["node", str(ROOT / "tests/client/nip_a3_payment_targets_runtime.mjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1200:]


def test_every_lightning_zap_entry_uses_payment_target_discovery():
    assert "const hasLn=!!(await _lightningAddress(pk,p))" in APP
    assert APP.count("addr=await _lightningAddress(pk,") >= 2
    assert "const p=profOf(pk); const addr=await _lightningAddress(pk,p);" in APP
    assert "if(await _lightningAddress(pk,profile))" in APP


def test_profile_can_gain_a_payment_target_after_its_kind_zero_was_painted():
    profile = APP.split("async function renderProfileView", 1)[1].split("async function editProfile", 1)[0]
    assert "_loadPaymentTargets(pk).then(lightning=>" in profile
    assert "_patchProfileTips(feed,pk,Store.profile(pk)||{},lightning)" in profile
