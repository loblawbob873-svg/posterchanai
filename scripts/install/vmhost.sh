#!/bin/bash
# Turn this machine into a PosterChan VM host (add-on: ./install.sh --vmhost). See docs/VM_HOSTING.md §1.
#
# Everything here reproduces what the first LIVE host (nas.lan, Gentoo, libvirt 12) needed — and in particular
# the two things that did not "just work" there:
#
#   * /var/lib is often BTRFS, and a VM disk on copy-on-write btrfs fragments into hundreds of thousands of
#     extents. `chattr +C` (NOCOW) only applies to files created AFTER it is set, so it is set on the storage
#     directory (and /var/lib/libvirt/images) ONLY WHILE THEY ARE EMPTY. On a directory that already holds images
#     it would do nothing for them and mislead, so it warns instead.
#   * libvirt built WITHOUT polkit (Gentoo's default USE) leaves its read-write socket root:root 0600 — the app
#     user gets "Permission denied" whatever groups it is in. A systemd socket drop-in (SocketGroup=libvirt,
#     SocketMode=0660) plus unix_sock_group/unix_sock_rw_perms in libvirtd.conf hand it to the `libvirt` group.
#     With polkit, the distro's own rule already grants the libvirt group, and nothing is changed.
#
# Also: the service user joins `libvirt` + `kvm`, the storage directory is owned <user>:<qemu group> 0751 with
# `isos/` 0755 (QEMU runs as `qemu`, Debian `libvirt-qemu`, and must traverse to the disks — app/services/vmhost/
# storage.py), the `default` NAT network is defined, started and autostarted, and /dev/kvm is checked.
#
# IDEMPOTENT: every step checks before it changes anything, so re-running only repairs what drifted.
# Tests: tests/test_install_vmhost.py runs this file under bash with stubbed system commands.
#
# Overridable (tests, unusual layouts): VMHOST_USER, VMHOST_STORAGE, VMHOST_LIBVIRT_IMAGES, VMHOST_ETC,
# VMHOST_POLKIT_ACTIONS, VMHOST_KVM_DEV, VMHOST_NETWORK_XML.

vmhost_log() { echo "   $*"; }

vmhost_packages() {
    # Nothing to do when the tools are already there — a re-run must not re-emerge libvirt.
    if command -v virsh >/dev/null 2>&1 && command -v qemu-img >/dev/null 2>&1; then
        print_success "libvirt and QEMU are installed ($(virsh --version 2>/dev/null || echo virsh))"
        return 0
    fi
    case "$DISTRO" in
        gentoo)
            # NOT sys-firmware/edk2-bin: qemu (pin-upstream-blobs) pins its own edk2 and an explicit request
            # conflicts with that pin.
            sudo emerge --noreplace app-emulation/libvirt app-emulation/qemu ;;
        debian)
            sudo apt-get update && sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
                libvirt-daemon-system libvirt-clients qemu-system-x86 qemu-utils ovmf ;;
        arch)
            sudo pacman -S --needed --noconfirm libvirt qemu-base edk2-ovmf dnsmasq ;;
        fedora)
            sudo dnf install -y libvirt-daemon-kvm libvirt-client qemu-kvm qemu-img edk2-ovmf ;;
        suse)
            sudo zypper --non-interactive install libvirt qemu-kvm qemu-tools qemu-ovmf-x86_64 ;;
        *)
            print_error "Unknown distribution — install libvirt, QEMU (x86_64), qemu-img and OVMF yourself, then re-run"
            return 1 ;;
    esac || { print_error "Package installation failed"; return 1; }
    if ! command -v virsh >/dev/null 2>&1 || ! command -v qemu-img >/dev/null 2>&1; then
        print_error "virsh/qemu-img are still missing after installing packages"
        return 1
    fi
    print_success "Installed libvirt and QEMU"
}

vmhost_qemu_group() {
    # The group QEMU runs as: Debian/Ubuntu `libvirt-qemu`, everyone else `qemu`.
    local g
    for g in libvirt-qemu qemu; do
        if getent group "$g" >/dev/null 2>&1; then echo "$g"; return 0; fi
    done
    [ "$DISTRO" = "debian" ] && echo "libvirt-qemu" || echo "qemu"
}

vmhost_groups() {
    local user="$1" g
    for g in libvirt kvm; do
        if ! getent group "$g" >/dev/null 2>&1; then
            print_warning "No '$g' group on this system — skipped"
            continue
        fi
        if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then
            print_success "'$user' is already in the $g group"
        else
            sudo usermod -aG "$g" "$user" \
                && print_success "Added '$user' to the $g group (effective when the service restarts)" \
                || print_warning "Could not add '$user' to $g — run: sudo usermod -aG $g $user"
        fi
    done
}

vmhost_is_empty_dir() { [ -d "$1" ] && [ -z "$(ls -A "$1" 2>/dev/null)" ]; }

vmhost_has_nocow() { lsattr -d "$1" 2>/dev/null | awk '{print $1}' | grep -q C; }

vmhost_nocow() {
    # chattr +C on btrfs, only while the directory is still empty (see the header).
    local dir="$1" fstype
    [ -d "$dir" ] || return 0
    fstype="$(stat -f -c %T "$dir" 2>/dev/null || true)"
    if [ "$fstype" != "btrfs" ]; then
        vmhost_log "$dir is on ${fstype:-an unknown filesystem} — no NOCOW needed"
        return 0
    fi
    if vmhost_has_nocow "$dir"; then
        print_success "$dir already has NOCOW (chattr +C)"
    elif vmhost_is_empty_dir "$dir"; then
        sudo chattr +C "$dir" && print_success "Set NOCOW (chattr +C) on $dir — VM disks created in it will not fragment" \
            || print_warning "chattr +C failed on $dir"
    else
        print_warning "$dir is on btrfs and already holds files: NOCOW only applies to NEW files, so it was not set."
        print_warning "  Move the images out, run 'sudo chattr +C $dir', and copy them back (cp, not mv) to fix them."
    fi
}

vmhost_storage() {
    local user="$1" qgroup="$2" dir="$3" isos
    isos="$dir/isos"
    if [ ! -d "$dir" ]; then
        sudo mkdir -p "$dir" || { print_error "Could not create $dir"; return 1; }
        print_success "Created $dir"
    fi
    # NOCOW BEFORE isos/ exists: an empty directory is the only kind +C can still help.
    vmhost_nocow "$dir"
    if [ ! -d "$isos" ]; then
        sudo mkdir -p "$isos" || { print_error "Could not create $isos"; return 1; }
    fi
    vmhost_nocow "$isos"
    if [ "$(stat -c '%U:%G %a' "$dir" 2>/dev/null)" = "$user:$qgroup 751" ] \
            && [ "$(stat -c '%U:%G %a' "$isos" 2>/dev/null)" = "$user:$qgroup 755" ]; then
        print_success "Storage $dir is already $user:$qgroup 0751 (isos/ 0755)"
    else
        sudo chown "$user:$qgroup" "$dir" "$isos" || print_warning "Could not chown $dir to $user:$qgroup"
        sudo chmod 0751 "$dir" && sudo chmod 0755 "$isos" || print_warning "Could not set the storage modes"
        print_success "Storage $dir ($user:$qgroup 0751, isos/ 0755)"
    fi
    # Parents must be traversable by the qemu user too (libvirt reports this as "Permission denied" on start).
    local parent
    parent="$(dirname "$dir")"
    while [ "$parent" != "/" ] && [ -n "$parent" ]; do
        if [ -d "$parent" ] && [ "$(( 0$(stat -c %a "$parent" 2>/dev/null || echo 755) & 1 ))" -eq 0 ]; then
            print_warning "$parent is not traversable by other users — QEMU cannot reach the disks (sudo chmod o+x $parent)"
        fi
        parent="$(dirname "$parent")"
    done
}

vmhost_has_polkit() { [ -f "${VMHOST_POLKIT_ACTIONS:-/usr/share/polkit-1/actions}/org.libvirt.unix.policy" ]; }

vmhost_set_conf() {
    # key = value in libvirtd.conf: replace a (commented or not) line for the key, or append one. Idempotent.
    local conf="$1" key="$2" value="$3"
    if grep -Eq "^[[:space:]]*$key[[:space:]]*=[[:space:]]*$value[[:space:]]*$" "$conf" 2>/dev/null; then
        return 1                                    # already set: nothing changed
    fi
    if grep -Eq "^[[:space:]]*#?[[:space:]]*$key[[:space:]]*=" "$conf" 2>/dev/null; then
        sudo sed -i -E "0,/^[[:space:]]*#?[[:space:]]*$key[[:space:]]*=.*/s||$key = $value|" "$conf"
    else
        echo "$key = $value" | sudo tee -a "$conf" >/dev/null
    fi
    return 0
}

vmhost_socket_access() {
    local etc="${VMHOST_ETC:-/etc}" changed=0 dropin_dir dropin want conf
    if vmhost_has_polkit; then
        print_success "libvirt uses polkit — the libvirt group's access comes from the distro's polkit rule"
        return 0
    fi
    print_warning "libvirt has no polkit here: its read-write socket is root-only unless handed to the libvirt group"
    dropin_dir="$etc/systemd/system/libvirtd.socket.d"
    dropin="$dropin_dir/posterchan-group.conf"
    want="$(printf '[Socket]\nSocketGroup=libvirt\nSocketMode=0660\n')"
    if [ "$(cat "$dropin" 2>/dev/null)" != "$want" ]; then
        if sudo mkdir -p "$dropin_dir" && printf '%s\n' "$want" | sudo tee "$dropin" >/dev/null; then
            changed=1
            print_success "Wrote $dropin"
        else
            print_warning "Could not write $dropin"
        fi
    else
        print_success "Socket drop-in already in place ($dropin)"
    fi
    conf="$etc/libvirt/libvirtd.conf"
    if [ -f "$conf" ]; then
        if vmhost_set_conf "$conf" unix_sock_group '"libvirt"'; then changed=1; fi
        if vmhost_set_conf "$conf" unix_sock_rw_perms '"0770"'; then changed=1; fi
    else
        print_warning "$conf not found — set unix_sock_group = \"libvirt\" and unix_sock_rw_perms = \"0770\" by hand"
    fi
    if [ "$changed" = 1 ] && command -v systemctl >/dev/null 2>&1; then
        sudo systemctl daemon-reload || true
        # The socket unit owns the socket file: restarting it (and the daemon behind it) applies the new mode.
        sudo systemctl restart libvirtd.socket 2>/dev/null || true
        sudo systemctl restart libvirtd.service 2>/dev/null || true
        print_success "Restarted libvirtd with the group socket"
    fi
}

vmhost_daemon() {
    command -v systemctl >/dev/null 2>&1 || return 0
    if systemctl is-enabled --quiet libvirtd.socket 2>/dev/null || systemctl is-enabled --quiet libvirtd.service 2>/dev/null; then
        print_success "libvirtd is enabled"
    else
        sudo systemctl enable --now libvirtd.socket 2>/dev/null || sudo systemctl enable --now libvirtd 2>/dev/null \
            && print_success "Enabled libvirtd" || print_warning "Could not enable libvirtd — start it by hand"
    fi
}

vmhost_network() {
    local V="virsh -c qemu:///system" xml="${VMHOST_NETWORK_XML:-/usr/share/libvirt/networks/default.xml}"
    if ! sudo $V net-info default >/dev/null 2>&1; then
        if [ -f "$xml" ]; then
            sudo $V net-define "$xml" >/dev/null && print_success "Defined the default network"
        else
            print_warning "No 'default' network and no $xml to define it from — create a NAT network named default"
            return 0
        fi
    fi
    if sudo $V net-info default 2>/dev/null | grep -Eq '^Autostart:[[:space:]]+yes'; then
        print_success "The default network starts with the host"
    else
        sudo $V net-autostart default >/dev/null && print_success "The default network now starts with the host"
    fi
    if sudo $V net-info default 2>/dev/null | grep -Eq '^Active:[[:space:]]+yes'; then
        print_success "The default network is active"
    else
        sudo $V net-start default >/dev/null && print_success "Started the default network" \
            || print_warning "Could not start the default network"
    fi
}

# DOCKER'S FORWARD POLICY DROPS EVERY VM PACKET. Docker sets the iptables FORWARD policy to DROP, and
# netfilter runs every table's forward hook: libvirt's own nft accept rules pass a VM's packets and
# Docker's iptables table then drops them. Measured on nas.lan: the VM could reach its gateway and
# nothing past it — no overlay sync, no binhost, a "running" VM with no network. Docker keeps the
# DOCKER-USER chain for exactly this, but the rules there vanish on reboot, so a unit re-applies them.
# Only libvirt's NAT bridges (virbr+) and br0-br3 (what the bridged-network setting creates) are let
# through — never br+, which would also match Docker's own br-<id> networks and undo their isolation.
vmhost_docker_forward() {
    command -v docker >/dev/null 2>&1 || [ -n "${VMHOST_FORCE_DOCKER:-}" ] || return 0
    local dir="${VMHOST_UNIT_DIR:-/etc/systemd/system}" unit=posterchan-vm-forward.service
    sudo tee "$dir/$unit" >/dev/null <<'UNIT'
[Unit]
Description=Let libvirt VMs through Docker's FORWARD DROP (PosterChan VM host)
After=docker.service libvirtd.service network-online.target
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for i in virbr+ br0 br1 br2 br3; do iptables -C DOCKER-USER -i "$i" -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER -i "$i" -j ACCEPT; iptables -C DOCKER-USER -o "$i" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER -o "$i" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; done'

[Install]
WantedBy=multi-user.target docker.service
UNIT
    sudo systemctl daemon-reload >/dev/null 2>&1 || true
    if sudo systemctl enable --now "$unit" >/dev/null 2>&1; then
        print_success "Docker is installed: VM traffic is allowed through its FORWARD policy ($unit)"
    else
        print_warning "Could not enable $unit — VMs may have no network while Docker's FORWARD policy is DROP"
    fi
}

vmhost_check_kvm() {
    local dev="${VMHOST_KVM_DEV:-/dev/kvm}"
    if [ -e "$dev" ]; then
        print_success "$dev present — hardware virtualisation is available"
    else
        print_warning "$dev is missing: enable VT-x/AMD-V in the firmware and load kvm_intel/kvm_amd — without it VMs run"
        print_warning "  as slow software emulation (and this host should not be offered as a VM host)"
    fi
}

setup_vmhost() {
    print_banner 2>/dev/null || true
    echo -e "${BOLD:-}🖥  Setting up this machine as a PosterChan VM host (libvirt/QEMU)${NC:-}"
    echo ""
    [ -n "$DISTRO" ] || detect_distro
    local user storage qgroup
    user="${VMHOST_USER:-${SUDO_USER:-$(whoami)}}"
    storage="${VMHOST_STORAGE:-/var/lib/posterchan/vms}"

    print_step "Packages"
    vmhost_packages || return 1
    qgroup="$(vmhost_qemu_group)"

    print_step "Service user '$user'"
    vmhost_groups "$user"

    print_step "Storage"
    vmhost_storage "$user" "$qgroup" "$storage" || return 1
    vmhost_nocow "${VMHOST_LIBVIRT_IMAGES:-/var/lib/libvirt/images}"

    print_step "libvirt daemon and socket"
    vmhost_daemon
    vmhost_socket_access

    print_step "Network"
    vmhost_network
    vmhost_docker_forward

    print_step "KVM"
    vmhost_check_kvm

    print_step "Final check (as '$user')"
    local out
    if out="$(sudo -u "$user" virsh -c qemu:///system list --all 2>&1)"; then
        echo "$out" | sed 's/^/   /'
        print_success "'$user' can drive qemu:///system"
    else
        echo "$out" | sed 's/^/   /'
        print_warning "'$user' cannot reach qemu:///system yet — if it was just added to the libvirt group, restart"
        print_warning "  the app service (group membership is read when a process starts), then re-run this check"
    fi
    echo ""
    echo "   Next: Admin → VMs → \"Run the VM host on this server\" (storage: $storage), then Save."
    echo "   Restart posterchanai.service first if '$user' was added to a group just now."
}
