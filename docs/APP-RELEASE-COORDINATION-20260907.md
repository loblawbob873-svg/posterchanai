# App release coordination

The auto-mute branch preserves both other-agent commits 3d33a27f5 (installer)
and 0c6422015 (Monero Send). The Monero changes received a further independent
review, corrective commit e94e1259a, and driven regression tests before deployment.
The installer and gentoo.sh remain owned by the other agent, per the user's
instruction. This release does not execute the installer or publish an ISO.

Installer review findings were passed to that agent through ~/agent.txt:

- `_in` is defined only inside the optional posterchan.conf branch, but standalone
  `gentoo.sh shell` / `posterchan-shell` can reach the fallback without that file.
- Existing nonempty cached downloads bypass the new magic checks, so an old
  cached HTML response can still prevent a valid archive fallback.

To avoid publishing work still under installer testing, this app deployment uses
an exact temporary copy of sync.sh with only its installer-version auto-bump and
overlay-publication blocks omitted. Lint, JavaScript parsing, app pushes, node
updates, restart classification and final node consistency checks are retained.
The repository's sync.sh is unchanged. Installer overlay publication and its
package version remain for the installer agent to complete with its fixes.

The app release uses client cache v1684 and root cache v94 because the other
agent already published Android/Desktop bundles containing v1683/v93. Reusing
those versions could retain the older bundled controller across an app update.
