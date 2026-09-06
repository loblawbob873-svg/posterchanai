# Terminal replay and stream decoding review

The isolated fixes address three executable failures:

- A replay frame whose end is newer than the cursor can still overlap earlier text.
  The renderer now removes the overlapping prefix, respecting local UTF-16 counters
  and remote UTF-8 byte counters. Previously the test rendered `hello🙂lo🙂 world`.
- While network writes wait, the SSH session continues draining and evicting old output.
  The stream now advances to the retained buffer's actual start before counting the
  next frame. A four-byte buffer test previously repeated `BBBB` into `BBBBBBBBBB`.
  The fallback router uses the same cursor correction.
- Local shell pipe chunks can split a Unicode character. Node now decodes UTF-8 as a
  stream instead of decoding every buffer separately. The byte-by-byte fixture used
  to turn box drawing, emoji, accents and Japanese into replacement characters.

The new regression tests failed against 7ee72ed42 and pass with these changes.
The terminal, local PTY, keeper, session ownership/resume and handoff set passed
59 tests. The native bundle workflows run the new client replay test; the desktop
workflow additionally runs the decoder test against its actual localterm.js.

`scripts/diagnose_terminal_screen_codex.py` drives the shipped term.js/xterm with
an isolated GNU screen/Codex PTY. It inserts a draft, edits with Backspace, resizes
between phone and desktop widths, checks the actual PTY grid against xterm, and
verifies the draft renders once. It never submits the draft or attaches to an
existing session. The check passed with the installed Codex 0.153.3 and GNU screen
4.09.01. Artifacts: /tmp/pc-terminal-screen-fixed2.log and the temporary artifact
path it reports. This basic check also passed before the fixes; it does not prove
which failure occurred in the user's long-running session.

The full suite also exposed a packaging test reading untracked, locally generated
run scripts. Fresh Intel/NVIDIA/AMD generators did not source data/secrets.env,
although this operator's copies did. The generators now source it; the replacement
test generates each launcher in a temporary install and executes it with fixture
settings and a recording interpreter. All four backends preserve settings and argv.
Three backend cases failed before the fix; all eleven wallet packaging tests pass.

Full combined-suite and deployment verification remain required. No gentoo.sh edits
are included in this branch. Native handset behavior is covered by a separate
emulator run, not inferred from these terminal tests.
