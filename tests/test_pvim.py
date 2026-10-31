"""pvim, PosterChanOS's only editor, typed at key by key.

"we need a new program, terminal, called pvim, it's a cyberpunk posterchan version of vim. Should be the default gentoo
text editor so we don't need nano,nvim,vim" (2026-10-30). Because it is the ONLY editor, `git commit`, `crontab -e`,
`visudo` and `sudoedit` all land in it: these tests drive the shipped engine (os/bin/pvim) with real key sequences and
read back the buffer and the BYTES on disk -- vim muscle memory has to do what it does in vim, and a save must hand a
file back exactly as the program that asked for it expects (same inode, same line endings, same final newline).
"""
import importlib.machinery
import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PVIM = ROOT / 'os' / 'bin' / 'pvim'


def _load():
    loader = importlib.machinery.SourceFileLoader('pvim_under_test', str(PVIM))
    spec = importlib.util.spec_from_loader('pvim_under_test', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


pv = _load()


def ed_with(text, path=None, tmp=None):
    if path is None:
        path = tmp / 'f.txt'
    Path(path).write_bytes(text.encode() if isinstance(text, str) else text)
    e = pv.Editor(str(path), clipboard=False)
    return e


def typed(e, keys):
    e.run_keys(keys)
    return '\n'.join(e.lines)


# ── motions and edits ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('start,keys,want', [
    ('one two three', 'dw', 'two three'),
    ('one two three', 'wdw', 'one three'),
    ('one two three', 'wwdw', 'one two '),               # last word: stops at the end of the line
    ('one two three', 'cwXX<Esc>', 'XX two three'),        # cw is ce on a word
    ('one two three', 'ce<Esc>', ' two three'),
    ('one two three', 'd2w', 'three'),
    ('one two three', '2dw', 'three'),
    ('one two three', 'wD', 'one '),
    ('one two three', 'wC!<Esc>', 'one !'),
    ('one two three', '$x', 'one two thre'),
    ('one two three', '3x', ' two three'),
    ('one two three', 'wX', 'onetwo three'),
    ('one two three', 'fwdb', 'one wo three'),
    ('one two three', 'dtt', 'two three'),
    ('one two three', 'dft', 'wo three'),
    ('one two three', '$dFt', 'one two e'),
    ('one two three', 'wdiw', 'one  three'),
    ('one two three', 'wdaw', 'one three'),
    ('one two three', 'wciwX<Esc>', 'one X three'),
    ('one two three', '~~~', 'ONE two three'),
    ('one two three', 'gUiw', 'ONE two three'),
    ('one two three', 'wrX', 'one Xwo three'),
    ('one two three', 'w3rX', 'one XXX three'),
    ('one two three', 'A!<Esc>', 'one two three!'),
    ('one two three', 'I> <Esc>', '> one two three'),
    ('one two three', 'wiX<Esc>', 'one Xtwo three'),
    ('one two three', 'waX<Esc>', 'one tXwo three'),
    ('one two three', '3iab<Esc>', 'abababone two three'),
    ('one two three', 'wRXYZ<Esc>', 'one XYZ three'),
    ('one two three', 'wRXY<BS><BS><Esc>', 'one two three'),   # backspace in Replace restores
    ('a("b", c)', 'f"di"', 'a("", c)'),
    ('a("b", c)', 'f(ci(x<Esc>', 'a(x)'),
    ('a("b", c)', 'fbda(', 'a'),
    ('f(a, [1, 2])', 'f1di[', 'f(a, [])'),
    ('x = {a: 1}', 'fadi{', 'x = {}'),
    ('5 apples', '<C-a>', '6 apples'),
    ('5 apples', '10<C-x>', '-5 apples'),
    ('end (x) here', 'f(%x', 'end (x here'),
])
def test_one_line_commands(tmp_path, start, keys, want):
    assert typed(ed_with(start, tmp=tmp_path), keys) == want


@pytest.mark.parametrize('start,keys,want', [
    ('a\nb\nc', 'dd', 'b\nc'),
    ('a\nb\nc', 'jdd', 'a\nc'),
    ('a\nb\nc', '2dd', 'c'),
    ('a\nb\nc', 'dj', 'c'),
    ('a\nb\nc', 'jdk', 'c'),
    ('a\nb\nc', 'dG', ''),
    ('a\nb\nc', 'Gdgg', ''),
    ('a\nb\nc', 'yyp', 'a\na\nb\nc'),
    ('a\nb\nc', 'yyP', 'a\na\nb\nc'),
    ('a\nb\nc', 'ddp', 'b\na\nc'),
    ('a\nb\nc', 'yj2p', 'a\na\nb\na\nb\nb\nc'),
    ('a\nb\nc', 'Yjp', 'a\nb\na\nc'),
    ('a\nb\nc', 'J', 'a b\nc'),
    ('a\nb\nc', '3J', 'a b c'),
    ('a\n    b', 'J', 'a b'),
    ('a\nb\nc', 'gJ', 'ab\nc'),
    ('a\nb\nc', 'ox<Esc>', 'a\nx\nb\nc'),
    ('a\nb\nc', 'Ox<Esc>', 'x\na\nb\nc'),
    ('a\nb\nc', '3ox<Esc>', 'a\nx\nx\nx\nb\nc'),
    ('a\nb\nc', 'Gox<Esc>', 'a\nb\nc\nx'),
    ('a\nb\nc', 'jccX<Esc>', 'a\nX\nc'),
    ('a\nb\nc', 'jSX<Esc>', 'a\nX\nc'),
    ('a\nb\nc', '>>', '    a\nb\nc'),
    ('    a\nb\nc', '<<', 'a\nb\nc'),
    ('a\nb\nc', '>j', '    a\n    b\nc'),
    ('a\nb\nc', 'jVjd', 'a'),
    ('a\nb\nc', 'Vjy2jp', 'a\nb\nc\na\nb'),
    ('abc\ndef', 'lvjd', 'af'),
    ('abc\ndef', 'vlly$p', 'abcabc\ndef'),
    ('abc\ndef', 'vjU', 'ABC\nDef'),
    ('abc\ndef', 'Vj>', '    abc\n    def'),
    ('abc', 'vllrx', 'xxx'),
    ('abc\ndef', 'vjJ', 'abc def'),
    ('  x\ny', 'A<CR>z<Esc>', '  x\n  z\ny'),                    # autoindent
    ('if x:\n    y', 'jA<CR>z<Esc>', 'if x:\n    y\n    z'),
    ('a\nb', 'A<BS><BS>X<Esc>', 'X\nb'),
    ('a\nb', 'ji<BS>X<Esc>', 'aXb'),                            # backspace at column 0 joins
    ('one two', 'A<C-w>X<Esc>', 'one X'),
    ('a\n\nb\nc\n\nd', '}dd', 'a\nb\nc\n\nd'),
    ('a\n\nb\nc\n\nd', 'jj}d{', 'a\n\nd'),
])
def test_multi_line_commands(tmp_path, start, keys, want):
    assert typed(ed_with(start, tmp=tmp_path), keys) == want


def test_counts_motions_and_gg_G(tmp_path):
    e = ed_with('\n'.join('line %d' % i for i in range(1, 21)), tmp=tmp_path)
    e.run_keys('G')
    assert e.row == 19
    e.run_keys('gg')
    assert e.row == 0
    e.run_keys('5G')
    assert e.row == 4
    e.run_keys('3j')
    assert e.row == 7
    e.run_keys(':12<CR>')
    assert e.row == 11
    e.run_keys('50%')
    assert e.row == 9
    e.run_keys('$')
    assert e.col == len(e.line) - 1
    e.run_keys('j')
    assert e.col == len(e.line) - 1, '$ then j keeps the end of line'


def test_undo_redo_and_dot(tmp_path):
    e = ed_with('a b c d', tmp=tmp_path)
    e.run_keys('dw')
    assert e.lines == ['b c d']
    e.run_keys('..')
    assert e.lines == ['d']
    e.run_keys('u')
    assert e.lines == ['c d']
    e.run_keys('uu')
    assert e.lines == ['a b c d']
    e.run_keys('<C-r>')
    assert e.lines == ['b c d']
    e.run_keys('u')
    e.run_keys('ciwX<Esc>w.')
    assert e.lines == ['X X c d'], 'dot repeats a change that typed text'
    e.run_keys('u')
    assert e.lines == ['X b c d'], 'an insert session is ONE undo step'
    e.run_keys('A!<Esc>j.')
    assert e.lines == ['X b c d!!']
    e2 = ed_with('a\nb\nc\nd', tmp=tmp_path)
    e2.run_keys('dd2.')
    assert e2.lines == ['d'], '2. repeats dd with a count of two'


def test_registers_and_named(tmp_path):
    e = ed_with('alpha\nbeta', tmp=tmp_path)
    e.run_keys('"ayyj"byy"ap"bp')
    assert e.lines == ['alpha', 'beta', 'alpha', 'beta']
    e.run_keys('gg"Ayyg"ap') if False else None
    e2 = ed_with('one two', tmp=tmp_path)
    e2.run_keys('yiwwviwp')
    assert e2.lines == ['one one']
    e3 = ed_with('x', tmp=tmp_path)
    e3.run_keys('"_ddp')
    assert e3.lines == [''], 'the black hole register keeps nothing'


def test_search_and_star(tmp_path):
    e = ed_with('foo\nbar\nfoo bar\nbaz', tmp=tmp_path)
    e.run_keys('/bar<CR>')
    assert (e.row, e.col) == (1, 0)
    e.run_keys('n')
    assert (e.row, e.col) == (2, 4)
    e.run_keys('n')
    assert (e.row, e.col) == (1, 0), 'search wraps'
    e.run_keys('N')
    assert (e.row, e.col) == (2, 4)
    e.run_keys('gg*')
    assert (e.row, e.col) == (2, 0)
    e.run_keys('?ba<CR>')
    assert (e.row, e.col) == (1, 0)
    e.run_keys('gg/nothere<CR>')
    assert e.msg_err and 'not found' in e.msg
    e.run_keys('gg/\\<ba[rz]\\><CR>n')
    assert (e.row, e.col) == (2, 4), 'vim regex: word boundaries and a class'


def test_substitute(tmp_path):
    e = ed_with('cat cat\ndog cat\nbird', tmp=tmp_path)
    e.run_keys(':s/cat/dog/<CR>')
    assert e.lines[0] == 'dog cat'
    e.run_keys(':%s/cat/cow/g<CR>')
    assert e.lines == ['dog cow', 'dog cow', 'bird']
    e.run_keys(':%s/\\(dog\\) \\(cow\\)/\\2 \\1/<CR>')
    assert e.lines[:2] == ['cow dog', 'cow dog']
    e.run_keys(':2,3s/o/0/g<CR>')
    assert e.lines == ['cow dog', 'c0w d0g', 'bird']
    e.run_keys(':%s#bird#a/b#<CR>')
    assert e.lines[2] == 'a/b'
    e.run_keys(':%s/d.g/\\U&/<CR>')
    assert e.lines[0] == 'cow DOG'
    e.run_keys(':%s/cow /cow\\r/<CR>')
    assert e.lines[:2] == ['cow', 'DOG']
    e.run_keys('u')
    assert e.lines[0] == 'cow DOG', ':s is one undo step'
    e.run_keys(':%s/zzz/y/<CR>')
    assert e.msg_err


def test_ex_line_commands(tmp_path):
    e = ed_with('c\na\nb\na\nd', tmp=tmp_path)
    e.run_keys(':sort<CR>')
    assert e.lines == ['a', 'a', 'b', 'c', 'd']
    e.run_keys(':sort u<CR>')
    assert e.lines == ['a', 'b', 'c', 'd']
    e.run_keys(':2,3d<CR>')
    assert e.lines == ['a', 'd']
    e.run_keys(':g/a/d<CR>')
    assert e.lines == ['d']
    e2 = ed_with('x1\ny\nx2', tmp=tmp_path)
    e2.run_keys(':g/x/s/x/Z/<CR>')
    assert e2.lines == ['Z1', 'y', 'Z2']
    e2.run_keys('u')
    assert e2.lines == ['x1', 'y', 'x2'], ':g is one undo step'
    e2.run_keys(':v/x/d<CR>')
    assert e2.lines == ['x1', 'x2']
    e2.run_keys(':1m$<CR>')
    assert e2.lines == ['x2', 'x1']
    e2.run_keys(':1t.<CR>')                 # the cursor followed the moved line, so `.` is line 2
    assert e2.lines == ['x2', 'x1', 'x2']
    e2.run_keys(':%normal Ax<CR>')
    assert e2.lines == ['x2x', 'x1x', 'x2x']
    e2.run_keys(':nonsense<CR>')
    assert e2.msg_err and 'Not an editor command' in e2.msg


def test_set_options(tmp_path):
    e = ed_with('\tx', tmp=tmp_path)
    e.run_keys(':set ts=4 sw=2 et nonu<CR>')
    assert e.opts['tabstop'] == 4 and e.opts['shiftwidth'] == 2 and e.opts['expandtab'] and not e.opts['number']
    e.run_keys(':retab<CR>')
    assert e.lines == ['    x']
    e.run_keys('>>')
    assert e.lines == ['      x']
    e.run_keys(':set bogus<CR>')
    assert e.msg_err


# ── files: what crontab, visudo, git and sudoedit get back ────────────────────────────────────────────────────
def test_save_is_in_place_and_keeps_mode(tmp_path):
    p = tmp_path / 'crontab.tmp'
    p.write_text('# m h dom mon dow command\n')
    os.chmod(p, 0o600)
    link = tmp_path / 'hardlink'
    os.link(p, link)
    ino = p.stat().st_ino
    e = pv.Editor(str(p), clipboard=False)
    e.run_keys('o0 3 * * * backup<Esc>:w<CR>')
    st = p.stat()
    assert st.st_ino == ino, 'a save must not replace the file (crontab/visudo read the inode they handed over)'
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert link.read_text() == '# m h dom mon dow command\n0 3 * * * backup\n', 'hard links see the save'
    assert not e.modified


def test_line_endings_and_final_newline_round_trip(tmp_path):
    for raw in (b'a\r\nb\r\n', b'a\nb', b'a\nb\n', b'', b'\n', b'caf\xe9 \xff\n', 'naïve 漢字\n'.encode()):
        p = tmp_path / 'rt.txt'
        p.write_bytes(raw)
        e = pv.Editor(str(p), clipboard=False)
        e.run_keys(':w<CR>')
        assert p.read_bytes() == raw, (raw, p.read_bytes())
    p.write_bytes(b'a\r\nb\r\n')
    e = pv.Editor(str(p), clipboard=False)
    e.run_keys('ox<Esc>:w<CR>')
    assert p.read_bytes() == b'a\r\nx\r\nb\r\n', 'a DOS file stays DOS'


def test_quit_guards_and_wq(tmp_path):
    p = tmp_path / 'msg'
    p.write_text('')
    e = pv.Editor(str(p), clipboard=False)
    e.run_keys('iFix the thing<Esc>')
    e.run_keys(':q<CR>')
    assert e.msg_err and 'No write since last change' in e.msg, ':q must refuse over unsaved work'
    with pytest.raises(pv.Quit):
        e.run_keys(':wq<CR>')
    assert p.read_text() == 'Fix the thing\n'
    e2 = pv.Editor(str(p), clipboard=False)
    e2.run_keys('dd')
    with pytest.raises(pv.Quit):
        e2.run_keys(':q!<CR>')
    assert p.read_text() == 'Fix the thing\n', ':q! discards'
    e3 = pv.Editor(str(p), clipboard=False)
    e3.run_keys('Aa<Esc>')
    with pytest.raises(pv.Quit):
        e3.run_keys('ZZ')
    assert p.read_text() == 'Fix the thinga\n'
    e4 = pv.Editor(str(p), clipboard=False)
    with pytest.raises(pv.Quit):
        e4.run_keys(':x<CR>')       # nothing changed: :x does not touch the file


def test_write_as_new_file_and_refusals(tmp_path):
    e = pv.Editor(None, clipboard=False)
    e.run_keys('ihello<Esc>')
    e.run_keys(':w<CR>')
    assert e.msg_err and 'No file name' in e.msg
    target = tmp_path / 'new.txt'
    e.run_keys(':w %s<CR>' % target)
    assert target.read_text() == 'hello\n' and e.path == str(target) and not e.modified
    other = tmp_path / 'other.txt'
    other.write_text('keep')
    e.run_keys(':w %s<CR>' % other)
    assert e.msg_err and 'exists' in e.msg and other.read_text() == 'keep', ':w over another file needs !'
    ro = tmp_path / 'ro'
    ro.write_text('x\n')
    os.chmod(ro, 0o444)
    e2 = pv.Editor(str(ro), clipboard=False)
    e2.run_keys('Ay<Esc>:w<CR>')
    if os.geteuid() != 0:
        assert e2.msg_err and ro.read_text() == 'x\n', 'a read-only file needs :w!'


def test_edit_other_file_guards_unsaved(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.write_text('A\n')
    b.write_text('B\n')
    e = pv.Editor(str(a), clipboard=False)
    e.run_keys('x:e %s<CR>' % b)
    assert e.path == str(a) and e.msg_err
    e.run_keys(':e! %s<CR>' % b)
    assert e.path == str(b) and e.lines == ['B']


def test_paste_does_not_staircase(tmp_path):
    e = ed_with('', tmp=tmp_path)
    e.run_keys('i')
    e.feed('<PASTE>def f():\n    return 1\n')
    e.feed('<Esc>')
    assert e.lines == ['def f():', '    return 1', ''], 'a terminal paste must not be re-indented line by line'


def test_filetypes_and_highlight():
    assert pv.filetype_of('/tmp/x.py') == 'python'
    assert pv.filetype_of('/var/db/repos/x/foo-1.ebuild') == 'sh'
    assert pv.filetype_of('/repo/.git/COMMIT_EDITMSG') == 'gitcommit'
    assert pv.filetype_of('/etc/portage/make.conf') == 'conf'
    assert pv.filetype_of('/usr/local/bin/tool', '#!/bin/bash') == 'sh'
    groups = {g for _, _, g in pv.highlight('def f(x): return "s"  # c', 'python')}
    assert {'keyword', 'string', 'comment', 'func'} <= groups
    assert [g for _, _, g in pv.highlight('# Please enter the commit message', 'gitcommit')] == ['comment']


def test_wide_and_tab_columns():
    cols = pv.display_cols('\tab漢c', 8)
    assert cols == [0, 8, 9, 10, 12, 13]


def test_cli_version_and_no_tty(tmp_path):
    import subprocess
    import sys
    out = subprocess.run([sys.executable, str(PVIM), '--version'], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith('pvim ')
    out = subprocess.run([sys.executable, str(PVIM), str(tmp_path / 'x')], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 1 and 'terminal' in out.stderr


# ── the real program in a real terminal ───────────────────────────────────────────────────────────────────────
def _run_in_pty(args, chunks, timeout=10, rows=24, cols=80):
    """Start pvim on a pseudo-terminal, type `chunks` (a list of bytes, with pauses), return (exit code, output)."""
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import sys
    import termios
    import time
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    env = dict(os.environ, TERM='xterm-256color', LANG='C.UTF-8', LC_ALL='C.UTF-8', HOME=str(Path(args[-1]).parent if args else '/tmp'))
    proc = subprocess.Popen([sys.executable, str(PVIM), *args], stdin=slave, stdout=slave, stderr=slave, env=env,
                            close_fds=True, start_new_session=True)
    os.close(slave)
    out = b''

    def drain(t):
        nonlocal out
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    return
    drain(0.8)
    for c in chunks:
        if isinstance(c, tuple) and c[0] == 'resize':
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', c[1], c[2], 0, 0))
            os.killpg(proc.pid, __import__('signal').SIGWINCH)
        else:
            os.write(master, c)
        drain(0.3)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        drain(0.2)
        os.close(master)
        raise AssertionError('pvim did not exit; screen tail: %r' % out[-600:])
    drain(0.2)
    os.close(master)
    return proc.returncode, out


def _text(out):
    """What a person reads: the terminal output with its colour and cursor escapes removed."""
    import re
    return re.sub(rb'\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]', b'', out).decode('utf-8', 'replace')


def test_in_a_terminal_type_save_quit(tmp_path):
    p = tmp_path / 'notes.txt'
    p.write_text('first\n')
    rc, out = _run_in_pty([str(p)], [b'o', 'second line with ünïcode 漢字'.encode(), b'\x1b', b':wq\r'])
    assert rc == 0, out[-400:]
    assert p.read_text() == 'first\nsecond line with ünïcode 漢字\n'
    assert 'NORMAL' in _text(out) and 'PosterChan' in _text(out), 'the status line (mode badge, branding) was drawn'


def test_in_a_terminal_intro_paste_and_resize(tmp_path):
    rc, out = _run_in_pty([], [('resize', 30, 100), b':q\r'])
    text = _text(out)
    assert rc == 0 and 'JACKED IN' in text and 'PosterChanOS' in text, ('the start screen draws and :q quits', text[-400:])
    # PosterChan herself: half blocks in 24-bit colour, written after curses (ptop's art, scripts/make_ptop_logo.py)
    assert '▀'.encode() in out and b'\x1b[38;2;' in out, 'the portrait was not drawn on the start screen'
    p = tmp_path / 'paste.py'
    p.write_text('')
    paste = b'\x1b[200~def f():\n    return 1\n\x1b[201~'
    rc, out = _run_in_pty([str(p)], [b'i', paste, b'\x1b', b':wq\r'])
    assert rc == 0, out[-400:]
    assert p.read_text() == 'def f():\n    return 1\n\n', 'a bracketed paste arrives as typed, not re-indented'


def test_in_a_terminal_q_refuses_then_q_bang(tmp_path):
    p = tmp_path / 'keep.txt'
    p.write_text('keep\n')
    rc, out = _run_in_pty([str(p)], [b'dd', b':q\r', b':q!\r'])
    assert rc == 0 and b'No write since last change' in out
    assert p.read_text() == 'keep\n'


# ── packaging: a fresh install carries pvim and no other editor ───────────────────────────────────────────────
GENTOO = (ROOT / 'os' / 'gentoo.sh').read_text()
OVERLAY = ROOT / 'os' / 'overlay'


def test_the_fresh_install_package_set_has_pvim_and_no_other_editor():
    base = next(l for l in GENTOO.split('\n') if l.startswith('BASE_PACKAGES='))
    assert 'app-editors/pvim' in base.split()[0:] or ' app-editors/pvim' in base
    for other in ('app-editors/neovim', 'app-editors/nano', 'app-editors/vim'):
        assert other not in base, other


def test_the_install_makes_pvim_the_only_editor_before_it_finishes():
    build = GENTOO.split('\nbuildGentoo() {', 1)[1].split('\n}\n', 1)[0]
    assert 'gentoo.sh pvim-only' in build
    assert build.index('gentoo.sh pvim-only') < build.index('finalizeInstall ||'), 'nano must be gone before the machine is finished'
    fn = GENTOO.split('\npvimOnly() {', 1)[1].split('\n}\n', 1)[0]
    assert 'virtual/editor' in fn and '--depclean "$ed"' in fn
    assert 'elif [ "$1" = "pvim-only" ]; then\n\tpvimOnly' in GENTOO


def test_the_overlay_virtual_names_pvim_alone_and_outranks_gentoos():
    v = (OVERLAY / 'virtual' / 'editor' / 'editor-0-r9.ebuild').read_text()
    rdep = v.split('RDEPEND="', 1)[1].split('"', 1)[0].split()
    assert rdep == ['app-editors/pvim'], rdep
    cats = (OVERLAY / 'profiles' / 'categories').read_text().split()
    assert 'app-editors' in cats and 'virtual' in cats


def test_the_package_installs_the_shipped_editor_and_sets_editor():
    eb = next((OVERLAY / 'app-editors' / 'pvim').glob('pvim-*.ebuild')).read_text()
    assert 'doexe "${FILESDIR}/pvim"' in eb and 'doenvd' in eb
    env = (OVERLAY / 'app-editors' / 'pvim' / 'files' / '99pvim').read_text()
    assert 'EDITOR="/usr/bin/pvim"' in env and 'VISUAL="/usr/bin/pvim"' in env
    pub = (ROOT / 'scripts' / 'publish_overlay.sh').read_text()
    assert 'bin/pvim" "$TMP/app-editors/pvim/files/pvim"' in pub, 'the overlay must carry THIS os/bin/pvim'


def test_the_aliases_never_shadow_a_real_editor(tmp_path):
    import subprocess
    alias_file = OVERLAY / 'app-editors' / 'pvim' / 'files' / 'pvim-aliases.sh'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'vim').write_text('#!/bin/sh\n')
    (bindir / 'vim').chmod(0o755)
    out = subprocess.run(['bash', '-c', 'source "$1"; alias', '_', str(alias_file)], capture_output=True, text=True,
                         env={'PATH': '%s:/usr/bin:/bin' % bindir})
    aliases = out.stdout
    assert "alias vim=" not in aliases, 'an installed vim must stay vim'
    for name in ('vi', 'nvim', 'nano'):
        if subprocess.run(['bash', '-c', 'command -v %s' % name], env={'PATH': '/usr/bin:/bin'}, capture_output=True).returncode:
            assert ("alias %s='pvim'" % name) in aliases, (name, aliases)
