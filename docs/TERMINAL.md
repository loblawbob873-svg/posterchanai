# Terminal — a real SSH shell, in the client

Sidebar → **Terminal** (and its own window in desktop mode). A full interactive PTY on a host the
operator listed, over a WebSocket, with xterm.js doing the emulation.

It is **off by default** and gated twice: `ssh_terminal_enabled`, plus an npub allowlist
(`ssh_terminal_users`; admins are always allowed). This is deliberate remote code execution and the
second such path in this codebase — `node_service` reaches nodes you registered, over Nostr; this
reaches any host you can log into.

## Setting it up (Admin → Nodes → SSH Terminal)

```
build   deploy@10.0.0.9:22   key=/home/pc/.ssh/id_ed25519
nas     admin@nas.lan
```

The host list is an **allowlist**, not a convenience. The client picks a *name*; it can never name an
address, so the terminal cannot be used to reach machines you did not list.

**No credential is stored.** A host either names a private key file that already exists on this
server (you put it there — nothing here ever writes one), or the person types a password that is used
for that one connect call and then forgotten. A password box that remembers is a credential store,
and a credential store for arbitrary SSH is a much worse thing to own than the terminal itself.

## Sessions last until you kill them

This is the part that makes it usable rather than a demo. A session is a **tmux-style session**: it
belongs to the server, not to the tab you are looking at it through.

* **The connection drops** — a Tor circuit, a phone locking, a train tunnel — and the shell keeps
  running. The client reconnects on its own, with a backoff, and is replayed what it missed.
* **You close the app.** Same thing. Come back and you are put straight back into it.
* **You pick it up on another device.** The session list is scoped to your *account*, so a shell
  started on the laptop appears under "still running" on the phone. Both may be attached at once.
* **Nothing expires.** `Kill` is what ends a session, and it is a separate button from `Detach` for
  exactly that reason.

If you want bounds back (a shared node), Admin → Nodes has three, all blank by default: how long a
detached session is held, an idle timeout while attached, and a hard ceiling on age.

### Tabs are separate shells, and the label is what makes them separate

The strip above the screen is tabs, not a recovery list: every entry is a distinct PTY, and `+` opens
another one. On a remote host each tab carries a **label** — `main`, then `2`, `3`, … — chosen by the
client as the first one no session of yours is using on that host, and the tab is named after it
(`server1`, `server1 2`). The label is half of the remote tmux session's name, so it is what decides
which shell you get.

That is not cosmetic. `tmux new-session -A` is attach-or-**create**, so two tabs sharing a label are
two SSH connections onto one shell — same screen, same keystrokes, same scrollback — and it looks
exactly like a New tab button that does nothing: the prompt you were already at comes back, and the
connection count climbs by one per press. Reattaching therefore names the label as well as the
session id, because a resume whose session has gone falls through to *opening* one.

### Surviving a restart of this app

There are two independent mechanisms, and they compose:

1. **The keeper process** (`posterchanai-shell.service`). The PTY lives in its own systemd unit, not
   in the web app, and the app talks to it over a unix socket. `./sync.sh` restarts the app several
   times a day; none of that touches your shell. Install it with:

   ```
   scripts/install_services.sh
   ```

   `scripts/deploy_targets.py` deliberately leaves this unit out of its "restart everything" set —
   the only unit that is — because restarting it destroys user state. Without the unit the terminal
   works identically, it just does not outlive a deploy.

2. **tmux/screen on the remote host** (`ssh_terminal_multiplex`, on by default). When the far end has
   `tmux` or `screen`, the shell is opened inside `tmux new-session -A -s pcai-<user>-<label>` —
   attach-or-create — so the session also survives a reboot of *this* box. **Nothing is installed for
   you**: with neither present you get a plain login shell and mechanism 1 still applies.

## On a phone

A soft keyboard has no Ctrl, Esc, Tab or arrows, and a shell without Ctrl-C is not a shell. There is
a key bar: Esc, Tab, a **sticky** Ctrl (press it, then a letter — a phone cannot hold two keys),
arrows, Home/End, Ctrl-C, and a ⌨ button that summons the keyboard. The font scales with the viewport
because 14px of monospace on a 390px screen is 27 columns and almost nothing prints at 27 columns.

## Over Tor / Orbot

The SSH connection is made **by this server**, not by your phone — the phone talks to PosterChan and
PosterChan talks to the host. So the terminal works from anywhere the app works, including over
Orbot, and the remote host never sees your phone. Circuits drop constantly, which is precisely why
sessions resume.

## Notes and limits

* Sessions live in the keeper's process. Rebooting this box, or restarting the keeper's own unit,
  ends them — use tmux on the far end if you need to survive that too.
* Eight sessions per account per node. `Kill` one first if you hit it.
* Host keys are auto-accepted (`AutoAddPolicy`). Refusing to connect until someone hand-populates
  `known_hosts` **on the server** would make the feature unusable; it is a real trade and it is
  logged rather than hidden.
* Needs `paramiko`. It is in `requirements.txt` and `requirements-nostr.txt`; a node missing it says
  so rather than throwing.

Tests: `tests/test_ssh_terminal.py`, `tests/test_ssh_resume.py`,
`scripts/check_terminal_mobile.py`.
