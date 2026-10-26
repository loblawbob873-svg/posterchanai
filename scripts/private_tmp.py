"""One throwaway TMPDIR per test run, removed when the run ends -- so the suite cleans up after itself.

On the machine this suite runs on, /tmp is a tmpfs: every byte left there is RAM. Headless Chrome
writes `.com.google.Chrome.XXXXXX` files and `com.google.Chrome.chrome_chrome_url_fetcher_*`
directories into TMPDIR and does not remove them when a test terminates it; pytest keeps its
`pytest-of-<user>` trees; `checkall.py` made a `pc-checkall-<pid>` directory per run and never
removed it; and a gate run is several hundred Chromes. Measured on 2026-09-23: 36,662 Chrome temp
files, 38,941 fetcher directories and 523 checkall directories -- about 8 GB of RAM, enough that a
deploy was killed for low memory.

Every one of those tools honours TMPDIR, so the fix is not a cleaner that chases each of them: it is
pointing TMPDIR at a directory that belongs to this run and deleting the whole directory at the end.

  * `scoped()`  -- a context manager for a PARENT that only needs its CHILDREN to use it (the deploy
                   gate): the caller puts the path in the child's env; nothing global changes.
  * `enter()`   -- for a whole PROCESS (checkall.py, a plain `pytest` via conftest): sets TMPDIR in
                   os.environ so every child inherits it, and removes the directory at exit. Nested
                   runs reuse the outer run's directory (PC_PRIVATE_TMP), so a gate that starts
                   pytest that starts Chrome makes ONE directory, removed once, by its owner.

The directory is created directly under the system temp root with a SHORT name, and pytest's own
tmp_path trees are NOT moved into it (PYTEST_DEBUG_TEMPROOT keeps them at the original root, and
pytest's retention settings delete them): some tests bind Unix sockets inside tmp_path, and one
directory deeper those paths pass the 108-byte AF_UNIX limit.

A RUN THAT IS KILLED NEVER REACHES ITS CLEANUP -- a SIGKILL from the memory watchdog, a crash, a
timeout. Its directory stayed in RAM for ever, and the next killed run added another: on 2026-10-14
12.5 GB of tmpfs was already gone with the box idle, and the gate on top of it took server1 down.
So every run directory names its OWNER (pid + that process's start time, `.owner`), and `make()`
first removes every `pct*` directory whose owner is no longer running. A directory with no `.owner`
is left alone unless it is a day old (an older build's run, or one being set up this instant).
"""
import atexit
import contextlib
import os
import shutil
import tempfile
import time

ENV_MARK = 'PC_PRIVATE_TMP'


def _root():
    # Whatever TMPDIR says (normally /tmp): the run's directory lives where its files would have.
    return tempfile.gettempdir()


OWNER = '.owner'
_UNOWNED_AGE = 24 * 3600


def _start_time(pid):
    """The kernel's start time of `pid` (field 22 of /proc/<pid>/stat), or None if it is not running.
    Compared with the recorded one so a recycled pid does not keep a dead run's directory alive."""
    try:
        with open(f'/proc/{int(pid)}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _owner_alive(path):
    try:
        with open(os.path.join(path, OWNER)) as f:
            pid, start = f.read().split()
    except FileNotFoundError:
        try:
            return time.time() - os.stat(path).st_mtime < _UNOWNED_AGE
        except OSError:
            return True
    except (OSError, ValueError):
        return True                      # unreadable: never delete what we cannot judge
    return _start_time(pid) == start


def sweep(root=None):
    """Remove the run directories of runs that are no longer running. Returns what it removed."""
    root = root or _root()
    gone = []
    try:
        names = os.listdir(root)
    except OSError:
        return gone
    for name in names:
        path = os.path.join(root, name)
        if not name.startswith('pct') or os.path.islink(path) or not os.path.isdir(path):
            continue
        if not _owner_alive(path):
            shutil.rmtree(path, ignore_errors=True)
            gone.append(path)
    return gone


def make(prefix='pct-'):
    sweep()
    path = tempfile.mkdtemp(prefix=prefix, dir=_root())
    with open(os.path.join(path, OWNER), 'w') as f:
        f.write(f'{os.getpid()} {_start_time(os.getpid())}')
    return path


def remove(path):
    if path and os.path.basename(path.rstrip('/')).startswith('pct'):
        shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def scoped(prefix='pct-'):
    """A private temp dir for children to use; removed on exit, whatever happened."""
    outer = os.environ.get(ENV_MARK)
    if outer and os.path.isdir(outer):
        yield outer                      # an enclosing run owns it and will remove it
        return
    path = make(prefix)
    try:
        yield path
    finally:
        remove(path)


def _keep_pytest_short(env, root):
    """pytest's tmp_path trees stay under the ORIGINAL temp root, not inside the private dir.

    Nested one level deeper, a test's socket path (`…/pytest-of-u/pytest-0/<test name>0/x.sock`)
    passes the 108-byte AF_UNIX limit -- measured: the pc-idle and Wayfire runtime tests failed
    with "AF_UNIX path too long". pytest cleans those trees itself (tmp_path_retention_policy in
    pyproject.toml), so nothing is left behind there either."""
    env.setdefault('PYTEST_DEBUG_TEMPROOT', root)


def enter(prefix='pct-'):
    """Point this process and everything it starts at a private temp dir; remove it at exit."""
    _keep_pytest_short(os.environ, _root())
    outer = os.environ.get(ENV_MARK)
    if outer and os.path.isdir(outer):
        path = outer
    else:
        path = make(prefix)
        os.environ[ENV_MARK] = path
        atexit.register(remove, path)
    os.environ['TMPDIR'] = path
    tempfile.tempdir = None              # tempfile caches its answer; make it read TMPDIR again
    return path


def child_env(env, path):
    """`env` with TMPDIR (and the ownership mark) pointing at `path` -- and NO session D-Bus.

    Every headless Chrome a test starts registers with the desktop's xdg-desktop-portal over the session bus
    (`org.freedesktop.host.portal.Registry.Register`), and the portal never lets go: measured on server1 2026-10-25,
    the portal grew only while a gate ran (12 -> 42 MB over four gates, flat in between) and had reached 29 GB after
    five days, which tripped the deploy watchdog. No test needs the user's bus -- the Electron tests drop it and the
    shell tests bring their own -- so the children get `disabled:` (Chrome then registers nothing: 1 -> 0, measured).
    """
    out = dict(env)
    _keep_pytest_short(out, _root())
    out.update(TMPDIR=path, DBUS_SESSION_BUS_ADDRESS='disabled:', **{ENV_MARK: path})
    return out
