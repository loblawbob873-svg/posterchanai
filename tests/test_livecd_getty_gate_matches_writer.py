"""The live-image gate must accept what the live-image WRITER actually writes.

`gentoo.sh livecd` writes the live getty override, then reads it back out of the packed
squashfs to prove the replacement landed. Those are two copies of one rule, and they drifted:
19daf8142 strengthened the ordering to `After=NetworkManager.service network-online.target`
("online, not merely started") and left the gate greping `^After=NetworkManager.service$`,
anchored — which that line can never match. Every `livecd` build after that commit failed,
and the message named WHO and PW, both healthy, blaming the pseudo-file trap instead.

So this runs the real writer and the real reader against each other.
"""
from pathlib import Path
import re
import subprocess

SRC = (Path(__file__).resolve().parents[1] / "os/gentoo.sh").read_text(encoding="utf-8")


def _writer_override():
    """Run the printf that writes the LIVE getty override, and return what it produced."""
    line = next(l for l in SRC.splitlines()
                if "printf '[Unit]" in l and "--autologin live" in l)
    out = subprocess.run(["bash", "-c", line.strip().rstrip("\\") + " "],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    assert "--autologin live" in out.stdout, out.stdout
    return out.stdout


def _gate_pattern():
    """The regex the gate greps the packed override with."""
    m = re.search(r"NET_ORDER=.*?grep -c(E?) '([^']+)'", SRC, re.S)
    assert m, "the live gate no longer measures the getty ordering"
    return m.group(2)


def test_the_gate_accepts_the_override_the_builder_writes():
    override = _writer_override()
    hits = subprocess.run(["grep", "-cE", _gate_pattern()],
                          input=override, capture_output=True, text=True, timeout=10)
    assert hits.stdout.strip() not in ("", "0"), (
        f"the gate pattern {_gate_pattern()!r} matches nothing in the override the "
        f"builder writes:\n{override}")


def test_the_gate_still_rejects_an_override_with_no_ordering():
    """It has to be able to FAIL, or it is not a gate."""
    unordered = "[Unit]\n[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin live --noclear %I $TERM\n"
    hits = subprocess.run(["grep", "-cE", _gate_pattern()],
                          input=unordered, capture_output=True, text=True, timeout=10)
    assert hits.stdout.strip() == "0"


def test_the_live_session_does_not_wait_for_an_address_only_it_can_obtain():
    """A live USB has no stored connection — Welcome is what configures wifi.

    Ordering the live getty after network-online.target makes the autologin wait on
    `nm-online -s -q`, which gives up after 30 seconds; until then there is no session at all.
    Measured on real hardware on the first image ever built after 19daf8142: half a minute of
    flashing plymouth, every boot. Ordering after NetworkManager STARTING is what Welcome needs.
    """
    override = _writer_override()
    after = [l for l in override.splitlines() if l.startswith("After=")]
    assert after, override
    for line in after:
        assert "NetworkManager.service" in line, line
        assert "network-online.target" not in line, (
            "the live session blocks on an address only the desktop it blocks can obtain")


def test_an_ordering_failure_says_so_instead_of_blaming_the_account():
    """A NET_ORDER failure used to print a healthy autologin/passwd pair and cite pseudoput."""
    assert "not ordered after NetworkManager.service" in SRC
    account_msg = SRC.index("That is a login prompt, not a desktop")
    order_msg = SRC.index("not ordered after NetworkManager.service")
    assert SRC.count('"$NET_ORDER" -lt 1', 0, account_msg) == 0, (
        "the ordering check is still folded into the account message's condition")
    assert order_msg > account_msg
