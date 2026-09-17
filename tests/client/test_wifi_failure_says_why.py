"""A Wi-Fi join that fails says why, not "Error invoking remote method".

Reported from a LiveUSB on another machine: "Can't connect to Wifi: says error invoking remote
method". Electron wraps every failed bridge call as "Error invoking remote method '<channel>': Error:
<reason>", and the toast showed the envelope. The shipped `wifiReason` is RUN under node against the
exact strings nmcli prints for the refusals a person can act on, and the main-process bridge now logs
nmcli's own words so the reason survives on a machine nobody can screenshot.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "static/js/client/osshell.js"
NET = ROOT / "desktop/net.js"

WRAP = "Error invoking remote method 'pc:net:connect': Error: "
CASES = {
    "psk": WRAP + "Connection activation failed: Secrets were required, but not provided.",
    "auth": WRAP + "Failed to add/activate new connection: Not authorized to control networking.",
    "nodev": WRAP + "No Wi-Fi device found.",
    "gone": WRAP + "No network with SSID 'Home' found.",
    "nm": WRAP + "Could not create NMClient object: Could not connect: No such file or directory.",
    "other": WRAP + "Connection activation failed: (7) Supplicant disconnected.",
    "bare": "Error: something odd",
}


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_every_refusal_is_said_in_words_without_the_electron_envelope():
    script = "const S=require(%s);const C=%s;const out={};for(const k in C)out[k]=S.wifiReason(new Error(C[k]),'Home');" \
             "out.none=S.wifiReason(null,'Home');process.stdout.write(JSON.stringify(out));" % (
                 json.dumps(str(MOD)), json.dumps(CASES))
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    for k, v in got.items():
        assert "invoking remote method" not in v.lower(), (k, v)
        assert not v.lower().startswith("error"), (k, v)
    assert got["psk"] == "could not join Home: the password was not accepted"
    assert "not allowed to change the network" in got["auth"]
    assert "no usable Wi-Fi adapter" in got["nodev"]
    assert "no longer in range" in got["gone"]
    assert "NetworkManager is not running" in got["nm"]
    assert got["other"] == "could not join Home: Connection activation failed: (7) Supplicant disconnected."
    assert got["bare"] == "could not join Home: something odd"
    assert got["none"] == "could not join Home"


def test_the_join_uses_it_and_the_bridge_logs_nmcli_s_reason():
    src = MOD.read_text(encoding="utf-8")
    join = src[src.index("const r = await net.connect(ssid, pw);"):]
    join = join[:join.index("closePop();")]
    assert "toast(wifiReason(e, ssid))" in join
    net = NET.read_text(encoding="utf-8")
    assert "console.warn('[net] nmcli '" in net
    assert "o.stdin" in net, "the password must still travel on stdin, never in the logged arguments"
