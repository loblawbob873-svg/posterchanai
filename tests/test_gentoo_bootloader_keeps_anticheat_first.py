"""`gentoo.sh bootloader` on an anti-cheat machine leaves the signed entry FIRST.

Found 2026-10-27 ("if i run gentoo.sh bootloader on desktop, how will it know how to install the bootloader
properly"): bootloader's `efibootmgr -c` puts the new PosterChanOS entry -- unsigned systemd-boot -- at the head of
BootOrder. On a machine that ran `gentoo.sh anticheat` with Secure Boot on, the firmware refuses it, and the machine
would not start until Secure Boot was turned off. Runs the SHIPPED keepAnticheatFirst against a stateful efibootmgr.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / "os/gentoo.sh").read_text()

FAKE = r"""#!/bin/sh
# A stateful efibootmgr: BootOrder in $STATE/order, entries in $STATE/entries; `-o X` rewrites the order.
case "$*" in
  *-o*) for a in "$@"; do [ "$prev" = "-o" ] && echo "$a" > "$STATE/order"; prev="$a"; done; exit 0 ;;
esac
echo "BootCurrent: 0001"
echo "BootOrder: $(cat "$STATE/order")"
cat "$STATE/entries"
"""


def _fn(name):
    i = SH.index(name + "() {")
    return SH[i:SH.index("\n}\n", i) + 3]


def _run(tmp, order, entries, marker=True):
    bin_ = tmp / "bin"
    bin_.mkdir()
    (bin_ / "efibootmgr").write_text(FAKE)
    os.chmod(bin_ / "efibootmgr", 0o755)
    state = tmp / "state"
    state.mkdir()
    (state / "order").write_text(order + "\n")
    (state / "entries").write_text(entries)
    root = tmp / "root"
    (root / "etc/posterchan").mkdir(parents=True)
    if marker:
        (root / "etc/posterchan/anticheat").write_text("2026-10-27T00:00:00Z\n")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "STATE": str(state), "PC_AC_ROOT": str(root)}
    r = subprocess.run(["bash", "-c", _fn("keepAnticheatFirst") + "\nkeepAnticheatFirst"], capture_output=True,
                       text=True, timeout=30, env=env)
    return r, (state / "order").read_text().strip()


ENTRIES = ("Boot0006* PosterChanOS (anti-cheat)\tHD(1,GPT,...)/File(\\EFI\\posterchan\\shimx64.efi)\n"
           "Boot0007* PosterChanOS\tHD(1,GPT,...)/File(\\EFI\\systemd\\systemd-bootx64.efi)\n"
           "Boot0001* UEFI Misc Device\tPciRoot(0x0)\n")


def test_the_signed_entry_goes_back_in_front_of_the_new_systemd_boot_entry(tmp_path):
    # What `efibootmgr -c` leaves behind: the freshly made systemd-boot entry (0007) first.
    r, order = _run(tmp_path, "0007,0006,0001", ENTRIES)
    assert r.returncode == 0, r.stderr
    assert order == "0006,0007,0001", ("the anti-cheat entry must be first, systemd-boot kept as the fallback", order)
    assert "stays first" in r.stdout


def test_a_machine_without_anticheat_is_left_exactly_as_bootloader_made_it(tmp_path):
    r, order = _run(tmp_path, "0007,0001", ENTRIES.replace("Boot0006* PosterChanOS (anti-cheat)", "Boot0006* Other"),
                    marker=False)
    assert r.returncode == 0 and order == "0007,0001" and r.stdout == "", (order, r.stdout)


def test_anticheat_on_but_its_entry_missing_says_so_and_changes_nothing(tmp_path):
    r, order = _run(tmp_path, "0007,0001", ENTRIES.replace("PosterChanOS (anti-cheat)", "Something else"))
    assert order == "0007,0001"
    assert "sudo gentoo.sh anticheat" in r.stdout, r.stdout


def test_bootloader_calls_it_right_after_making_its_entry():
    body = SH[SH.index("bootloader() {"):]
    made = body.index("Firmware boot entry created: PosterChanOS")
    assert "keepAnticheatFirst" in body[made:made + 200], "bootloader no longer restores the anti-cheat entry"
