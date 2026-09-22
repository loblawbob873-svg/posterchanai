# Session recovery — 2026-09-22
ALL DEPLOYED: every node + origin + github on 0deeccd4; desktop 1.0.1650 overlay published.
Open items (not ours to do now):
 - Firefox theme 1.1.0: submitted LISTED on AMO, awaiting Mozilla review; firefox-theme.yml runs
   every 6h and publishes the signed xpi; then run scripts/publish_overlay.sh to ship it.
 - Laptop (.154) + Desktop (.102): user runs `update-posterchan` + reboot (repairs kernel via
   pc-kernel-guard, removes live launcher, brings desktop 1.0.1650). Don't restart .102 ourselves.
 - Build VM "PosterChan" (nas.lan, 192.168.122.32): freshly reinstalled from the gated 0921 ISO,
   LUKS 123456, root ssh key from server1, internal libvirt net moved to 192.168.124.x.
 - ISO: https://iso.poster.place/posterchanos.iso (nas distfiles/iso via router nginx + CF tunnel).
 - app.js is 25 bytes under its 2.85 MB single-asset budget — trim before adding to it.
