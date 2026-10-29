"""System Settings → Secure Boot → Travel mode ("If you travel, you will want a password to unlock", 2026-10-29).

The SHIPPED ossecureboot.js under node: the page offers the switch only once Secure Boot is running, says which way the
disk unlocks, asks before turning the password on (a forgotten password is a lost disk), and calls the bridge with the
right direction. The helper half is tests/test_pc_anticheat.py (TravelBox).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
RUN = r"""
require(%(src)s);
const SB = globalThis.PCSecureBoot, enc = s => String(s), icon = () => '';
const out = {};
const page = ac => SB.page(ac, '', icon, enc);
out.offerOn = page({state:'enabled', secureBoot:'enabled', tpm:'on', travel:false});
out.offerOff = page({state:'enabled', secureBoot:'enabled', tpm:'travel', travel:true});
out.notRunning = page({state:'enabled', secureBoot:'disabled', tpm:'waiting'});
// bind() against a host holding the travel block, with a bridge that records what it was asked.
async function press(travelOn, confirmed){
  const calls = [], toasts = [];
  const btn = {disabled:false, textContent:'', onclick:null};
  const block = {getAttribute: () => travelOn ? 'on' : 'off'};
  const host = {querySelector: sel => sel === '[data-sb-travel]' ? block : sel === '[data-sb-travel-toggle]' ? btn : null};
  const PC = () => ({ uiConfirm: async () => confirmed, toast: t => toasts.push(t) });
  const bridge = { travel: async on => { calls.push(on); return {}; } };
  let rerendered = 0;
  SB.bind(host, () => rerendered++, PC, bridge);
  await btn.onclick();
  return {calls, toasts, rerendered};
}
(async () => {
  out.turnOn = await press(false, true);
  out.cancelled = await press(false, false);
  out.turnOff = await press(true, false);      // off never asks: the person is signed in already
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def got():
    if not NODE:
        pytest.skip("node required")
    src = json.dumps(str(ROOT / "static/js/client/ossecureboot.js"))
    r = subprocess.run([NODE, "-e", RUN % {"src": src}], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_switch_is_offered_once_secure_boot_runs_and_says_which_way_the_disk_unlocks(got):
    assert 'data-sb-travel="off"' in got["offerOn"] and "Turn travel mode on" in got["offerOn"]
    assert "no password at startup" in got["offerOn"]
    assert 'data-sb-travel="on"' in got["offerOff"] and "Turn travel mode off" in got["offerOff"]
    assert 'data-sb-tpm="travel"' in got["offerOff"] and "password is asked at every start" in got["offerOff"]
    assert "data-sb-travel" not in got["notRunning"], "travel mode offered before Secure Boot (and its TPM unlock) runs"


def test_turning_it_on_asks_first_and_cancel_changes_nothing(got):
    assert got["turnOn"]["calls"] == [True] and "Travel mode is on" in got["turnOn"]["toasts"][0]
    assert got["cancelled"]["calls"] == [], "a cancelled confirm still turned travel mode on"


def test_turning_it_off_needs_nothing_typed(got):
    assert got["turnOff"]["calls"] == [False] and "unlocks by itself again" in got["turnOff"]["toasts"][0]
