# pvim — PosterChanOS's editor

vim's keys, neon on black. pvim is the only editor on a fresh PosterChanOS install: `EDITOR` and `VISUAL` point at
it, so `git commit`, `crontab -e`, `visudo` and `sudoedit` open it, and `vi`, `vim`, `nvim` and `nano` are aliases for
it wherever those programs are not installed. Install vim, neovim or nano any time — the real one then wins.

`:help` inside pvim lists every key on one page.

## What it does

- **Modes:** normal, insert, replace (`R`), visual (`v`, `V`), command line (`:`), search (`/`, `?`).
- **Moving:** `h j k l`, `w b e W B E`, `0 ^ $`, `gg G {n}G {n}%`, `f t F T ; ,`, `%`, `{ }`, `H M L`, `Ctrl-d/u/f/b`,
  marks (`m` + letter, then `'` or `` ` `` + letter).
- **Editing:** operators `d c y > < gu gU g~` with any motion or text object (`iw aw i" a" i( a( i{ a{ i[ a[ i< a<`),
  `dd cc yy D C Y x X s S r J gJ ~ p P`, `Ctrl-a`/`Ctrl-x`, counts everywhere, `.` repeat, `u` / `Ctrl-r`.
- **Registers:** `"a`–`"z` (uppercase appends), `"_` (discard), `"+` (system clipboard via wl-copy/xclip).
- **Search:** vim regex (`\<word\>`, `\(group\)`, `\|`), `n N * #`, highlight with `:noh`.
- **Ex:** `:w [file]`, `:w!`, `:wq`, `:x`, `ZZ`, `:q`, `:q!`, `ZQ`, `:e file`, `:e!`, `:saveas`, `:r file`,
  `:[range]s/pat/rep/[gIi]`, `:d`, `:y`, `:m`, `:t`, `:j`, `:sort [u i]`, `:g/pat/cmd`, `:v/pat/cmd`, `:normal`,
  `:retab`, `:set …`.
- **Settings:** `~/.pvimrc` (or `/etc/pvimrc`), one command per line, e.g. `set ts=4 sw=4 et`.

## `:format` — built-in Python and shell formatting

`:format` (or `:fmt`) re-lays out the whole file as ONE undo step (`u` takes it back):

- **Python** — PEP 8 spacing around operators, commas and `=` (none for keyword arguments and defaults), two blank
  lines above a top-level `def`/`class` (above its decorators and the comments that belong to it), one above a
  nested one, trailing whitespace off. Multi-line strings are content and are never touched. The result must parse to
  the **same AST** as the original, or nothing is changed and pvim says why.
- **Shell** (`.sh`, `bash`/`sh` scripts, ebuilds) — indents `if`/`for`/`while`/`case`/functions/`{ }` blocks;
  heredocs and multi-line strings are left exactly as they are. Ebuilds and tab-indented files get tabs, everything
  else `shiftwidth` spaces. Every line's text must be unchanged apart from indentation and `bash -n` must accept the
  result, or nothing is changed.

Any other file type answers "no built-in formatter". Checked across every Python and shell file in this repo
(3,300+): no output ever changes the program, and formatting twice equals formatting once.

## The rules a system editor has to keep

- **A save writes the file in place** — same inode, owner, mode and hard links. `crontab -e` and `visudo` check the
  temp file they handed over; a rename-into-place would leave them reading a stale one.
- **Bytes round-trip.** Files are read as UTF-8 with `surrogateescape`, so a Latin-1 config or a stray binary byte is
  written back exactly; CRLF files stay CRLF; a file with no final newline keeps having none.
- **Nothing is lost on quit.** `:q` refuses over unsaved changes; `:q!` is the only way past. An internal error is
  reported on the message line and the buffer stays editable.
- **A terminal paste is one insert** (bracketed paste), never re-indented line by line.

## Where it lives

- Source: `os/bin/pvim` (one Python file; standard library only).
- Package: `os/overlay/app-editors/pvim` (`publish_overlay.sh` injects the source and stamps a timestamp version).
- `os/overlay/virtual/editor/editor-0-r9.ebuild` names pvim alone, so `@system` stops pulling nano.
- `gentoo.sh pvim-only` (run at the end of a from-scratch install and on the ISO build VM) installs pvim and the
  virtual, and depcleans nano/neovim/vim when nothing — and no `@world` entry — still wants them.
- Tests: `tests/test_pvim.py` (keys in, buffer and bytes out; the real program on a pseudo-terminal; packaging).
