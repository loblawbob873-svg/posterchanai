# Test and publish a build — start to finish

Three things ship: the **code** (server + web client), the **apps** (desktop, Android), and the
**PosterChanOS ISO**. Do them in this order.

## 1. Code: test, then deploy

```bash
./test.sh                 # the whole suite (~10 min); exit 0 = nothing failed
git add <new files>       # sync.sh commits with -a, which skips untracked files
./sync.sh                 # full regression gate → commit → push origin → pull + restart every node
git push github master:main
```

`sync.sh` refuses to deploy if any test fails, and ends by checking every node is on the same
commit. Don't edit files while it runs — it aborts with "source changed". More: [TESTING.md](TESTING.md).

## 2. Apps: built by CI on every push

| App | Where it comes from |
|---|---|
| Android APK | GitHub Actions **Android APK**; published only if **Android emulator checks** pass on the same commit |
| Desktop (Linux/Windows/macOS) | GitHub Actions **Desktop apps** → release `desktop-v1.0.<run>` |

PosterChanOS machines only get a desktop build once the overlay is pinned to it:

```bash
venv-unified/bin/python scripts/bump_desktop_overlay.py 1.0.<run>   # checks the hashes first
git add -A os/overlay && git commit -m "chore(overlay): pin desktop 1.0.<run>" && ./sync.sh
```

Then `update-posterchan` on a machine installs it.

## 3. The ISO: build → gate → publish

There is no single script: the three steps are separate on purpose, so an image is never published
before it has installed and booted.

**Build** (on the Gentoo build machine, as root):

```bash
PC_ISO_CLEAN=y PC_ISO_OUT=/root/probe ./os/gentoo.sh livecd
```

**Gate** (on a KVM host; each exits 0 = pass, 1 = fail, 2 = could not run):

```bash
ISO=/root/probe/posterchan-live-YYYYMMDD.iso
python3 scripts/check_livecd_install_vm.py "$ISO" --server --rounds 2   # installs, boots, enables the server, sees posts
python3 scripts/pcos_installer_vm_probe.py "$ISO" --no-inject           # the graphical installer
python3 scripts/check_livecd_welcome.py "$ISO"                          # boots to the first-run screen
```

**The Server button** (System Settings → PosterChan Server → Enable, clicked as the first owner) —
two halves, see the docstring of `scripts/check_server_enable_ui.py`:

```bash
# on the KVM host, next to check_livecd_install_vm.py:
python3 check_livecd_install_vm.py "$ISO" --disk /root/probe/ui.qcow2 --keep-disk --evidence-dir /root/probe/ev-install
python3 vm_server_ui_boot.py /root/probe/ui.qcow2 /root/probe/ev-install/OVMF_VARS.fd /root/probe/ev-ui
# here:
ssh -N -J nas.lan -L 9322:127.0.0.1:9322 root@<kvm-host> &
PC_UI_CDP_PORT=9322 venv-unified/bin/python scripts/check_server_enable_ui.py
```

Power the VM off as soon as it passes — a server left running gets rate-limited by the relays.

Keep work files on a real disk (e.g. `/root/probe`), not a tmpfs `/var/tmp` — a 40 GB VM disk in RAM
gets qemu OOM-killed.

**Publish** (only after every gate passed):

```bash
scripts/publish_iso.sh "$ISO"   # uploads, verifies sha256, then swaps it in atomically
```

It serves at <https://iso.poster.place/posterchanos.iso> with a `.sha256` beside it.
