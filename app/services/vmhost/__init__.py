"""PosterChan VM hosting — this node's own Proxmox-like VM host, managed over encrypted Nostr.

Kept deliberately light: the relay subprocess imports `kinds` from here, so nothing at package level
may pull in the database, libvirt or the settings store. See docs/VM_HOSTING.md.
"""
