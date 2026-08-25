"""PosterChan Code — the editor's own file API and its beautifiers.

WHY THIS IS NOT `/api/files`. That router is the USER STORAGE file manager: it proxies to a separate
storage server, and its write path raises outright ("Storage server not configured") on a node that
has none. An editor whose Save is a 500 on most deployments is not an editor. It also roots itself in
per-user storage, which is not where code being tested lives — the terminal beside it opens a shell
in the app's own directory, and an editor whose tree cannot reach what that shell can run is two
tools rather than one.

So this is small, self-contained and jailed to ONE root, and it shares the TERMINAL's gate rather
than inventing a second answer to "who may touch this node's files" — `node_service.user_allowed`,
which is admins plus the `node_exec_users` allowlist. Editing files on a node and running commands on
it are the same privilege wearing two hats; giving them two different gates is how one of them ends
up quietly wider than anybody meant.

THE PATH JAIL IS THE WHOLE SECURITY OF THIS FILE and it is enforced in ONE function, `_resolve`, on
the REAL path after symlinks. Checking a string prefix before resolving is the classic hole: `..` is
only the obvious half, and a symlink inside the workspace pointing at `/etc` is the half that gets
missed. Every endpoint here goes through it; none of them joins a path itself.
"""
import os
import logging
import importlib.util
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services import node_service
from app.services import settings_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/code", tags=["code"])

# A text file this editor will open. Bigger than this is not an editing problem, it is a different
# tool -- and a browser that is handed 40 MB of one line stops responding rather than failing.
MAX_BYTES = 2 * 1024 * 1024
# Directory listings are bounded for the same reason: node_modules is one directory.
MAX_ENTRIES = 2000

# Directories that are never worth walking into and are always enormous. Skipped in the LISTING
# only -- a file inside one still opens if something names it, because refusing to open a path we
# happily displayed is the kind of inconsistency that reads as a bug in the editor.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "venv",
             "venv-unified", ".venv", "dist", "build", ".gradle", ".idea"}

LANGS = {
    ".py": "python", ".pyw": "python",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".json": "json", ".html": "html", ".htm": "html", ".css": "css",
    ".md": "markdown", ".markdown": "markdown",
    ".java": "java", ".yml": "yaml", ".yaml": "yaml",
    ".sql": "sql", ".xml": "xml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
}


def _root() -> str:
    """The one directory this API can see.

    Defaults to the app's own checkout — the directory `run.py` lives in, which is also where the
    terminal's local shell starts, so the tree and the shell agree about what "here" means.
    """
    raw = (settings_store.get("code_workspace_root") or "").strip()
    base = raw or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.realpath(base)


def _resolve(rel: str, root: str, must_exist: bool = True) -> str:
    """A relative path from the client turned into an absolute one INSIDE the workspace, or 400.

    Resolved with `realpath` FIRST and containment checked SECOND. The other order -- reject `..`,
    then join -- passes a symlink in the workspace that points anywhere on the disk, which is the
    same file read the jail exists to prevent, arrived at by a route nobody looks at.

    `os.path.commonpath` rather than `startswith`: a root of `/srv/app` and a target of
    `/srv/app-secrets` share a string prefix and are different directories.
    """
    root = os.path.realpath(root)
    rel = (rel or "").replace("\\", "/").strip("/")
    if "\x00" in rel:
        raise HTTPException(status_code=400, detail="Invalid path")
    target = os.path.realpath(os.path.join(root, rel))
    try:
        if os.path.commonpath([root, target]) != root:
            raise ValueError
    except ValueError:
        # Different drives on Windows also land here, and the answer is the same one.
        raise HTTPException(status_code=403, detail="That path is outside the workspace")
    if must_exist and not os.path.exists(target):
        raise HTTPException(status_code=404, detail="No such file")
    return target


def _rel(abs_path: str, root: str) -> str:
    rel = os.path.relpath(abs_path, os.path.realpath(root))
    return "" if rel == "." else rel.replace("\\", "/")


def _lang(name: str) -> str:
    return LANGS.get(os.path.splitext(name)[1].lower(), "text")


def _user_root(user: User) -> str:
    """One person's own workspace, made on demand.

    Keyed on the numeric id, never the username: a name can be changed and a display name can
    contain a slash, and either would move somebody else's files or escape the base directory.
    """
    base = (settings_store.get("code_user_root") or "").strip()
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "data", "code-workspaces")
    path = os.path.realpath(os.path.join(os.path.realpath(base), str(int(getattr(user, "id", 0) or 0))))
    os.makedirs(path, exist_ok=True)
    return path


def _guard(db: Session, user: User) -> str:
    """WHICH TREE THIS PERSON MAY EDIT — the whole of the access decision, in one place.

    PosterChan Code is for everybody: it was gated to the terminal's allowlist, so an ordinary
    account got "limited to administrators" and no editor at all. That gate was not arbitrary,
    though, and removing it alone would have been the worst possible fix: `_root()` defaults to the
    app's OWN CHECKOUT, so write access there is write access to the code this node runs — handing
    every signed-up account remote code execution, on a public instance, in one line.

    So the gate became a ROUTE. An operator (admin, or the Admin → Nodes allowlist — the same people
    who may open a shell here) still edits the node's tree, which is what the feature was built for.
    Everybody else gets a directory of their own, created on first use, jailed exactly the same way.
    Both are real editors; they differ only in what they can see.
    """
    if node_service.user_allowed(db, user):
        return _root()
    return _user_root(user)


# --------------------------------------------------------------------------------------------
# Beautifiers
# --------------------------------------------------------------------------------------------

def _engines() -> dict:
    """WHICH FORMATTERS THIS NODE ACTUALLY HAS, asked rather than assumed.

    Reported to the client so the button can say what it will do. A Format button that silently
    does nothing on a node without the dependency is worse than one that is not offered: the person
    presses it, the code does not change, and there is no way to tell that from "already tidy"."""
    def has(mod):
        # find_spec, not a try/import: importing black costs ~200ms and this is asked on every
        # config read and every failed format, including on nodes that will never have it.
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            return False
    return {"python": "black" if has("black") else "",
            "bash": "beautysh" if has("beautysh") else ""}


def _format_python(src: str, indent: int) -> str:
    import black
    mode = black.Mode(line_length=100)
    # `format_file_contents(..., fast=False)` rather than `format_str`: fast=False runs black's own
    # round-trip check, re-parsing its output and comparing the AST with the input's. On somebody's
    # unsaved work those milliseconds are worth it -- a formatter that silently changes what code
    # MEANS is the one failure this feature cannot have.
    try:
        return black.format_file_contents(src, fast=False, mode=mode)
    except black.NothingChanged:
        return src


def _format_bash(src: str, indent: int) -> str:
    from beautysh import BashFormatter
    out, err = BashFormatter(indent_size=max(1, min(8, indent))).beautify_string(src)
    if err:
        # beautysh reports trouble as a FLAG beside a best-effort result, not as an exception. The
        # usual cause is unbalanced block keywords -- exactly what a half-typed script looks like --
        # and returning its guess would silently reindent a file around a block that is not there.
        raise ValueError("this script has an unbalanced if/do/case block, so it was left alone")
    return out


def _format_json(src: str, indent: int) -> str:
    import json
    # Ordinary load/dump, but `ensure_ascii=False`: re-encoding somebody's UTF-8 into \\u escapes is
    # a diff across every non-English string in the file for no reason anybody asked for.
    return json.dumps(json.loads(src), indent=max(1, min(8, indent)), ensure_ascii=False) + "\n"


class FormatBody(BaseModel):
    language: str = "text"
    source: str = ""
    indent: int = 4


@router.post("/format")
async def format_source(body: FormatBody, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Beautify a buffer. THE SOURCE COMES AND GOES IN THE REQUEST — nothing is read or written.

    Formatting an unsaved buffer is the whole point (you tidy, look, then save), so this must not
    touch the file. It also means a formatter that throws costs the person nothing."""
    _guard(db, current_user)          # everyone has an editor; this endpoint touches no file
    lang = (body.language or "text").lower()
    src = body.source or ""
    if len(src.encode("utf-8", "ignore")) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That file is too big to format")
    fns = {"python": _format_python, "bash": _format_bash, "json": _format_json}
    if lang not in fns:
        return {"ok": False, "error": "No formatter for " + lang, "engine": "", "source": src}
    try:
        out = fns[lang](src, int(body.indent or 4))
    except HTTPException:
        raise
    except Exception as e:
        # A SYNTAX ERROR IS THE ORDINARY CASE, not an incident: people format while mid-edit. It is
        # reported as a sentence and the buffer is returned untouched, never a 500.
        msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
        return {"ok": False, "error": msg, "engine": _engines().get(lang, ""), "source": src}
    engine = {"python": _engines().get("python", ""), "bash": _engines().get("bash", ""),
              "json": "json"}[lang]
    return {"ok": True, "source": out, "engine": engine, "changed": out != src}


# --------------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------------

@router.get("/config")
async def config(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    root = _guard(db, current_user)
    # `own` is what lets the editor say WHOSE tree this is. An operator editing the node and a
    # person editing their own directory are both real editors, and confusing the two is how
    # somebody edits a config file they think is theirs.
    return {"root": root, "own": root != _root(), "engines": _engines(), "maxBytes": MAX_BYTES}


@router.get("/tree")
async def tree(path: str = Query(""), db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    root = _guard(db, current_user)
    target = _resolve(path, root)
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail="Not a directory")
    entries, truncated = [], False
    try:
        names = sorted(os.listdir(target), key=lambda s: s.lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail="This directory cannot be read")
    for name in names:
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        full = os.path.join(target, name)
        try:
            is_dir = os.path.isdir(full)
            st = os.stat(full)
        except OSError:
            # A broken symlink or a file that vanished mid-listing is skipped, never fatal: one bad
            # entry must not cost the whole directory.
            continue
        if is_dir and name in SKIP_DIRS:
            continue
        entries.append({"name": name, "dir": is_dir, "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime), "lang": "" if is_dir else _lang(name)})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": _rel(target, root), "entries": entries, "truncated": truncated}


@router.get("/file")
async def read_file(path: str = Query(...), db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    root = _guard(db, current_user)
    target = _resolve(path, root)
    if os.path.isdir(target):
        raise HTTPException(status_code=400, detail="That is a directory")
    size = os.path.getsize(target)
    if size > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That file is too big to open here")
    with open(target, "rb") as fh:
        raw = fh.read()
    # A NUL BYTE MEANS BINARY, and opening one in a text editor is how a person destroys a file:
    # the bytes that do not survive the round trip through a textarea are gone on the next save.
    if b"\x00" in raw:
        raise HTTPException(status_code=415, detail="That looks like a binary file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="That file is not UTF-8 text")
    return {"path": _rel(target, root), "text": text, "lang": _lang(target), "size": size,
            "mtime": int(os.path.getmtime(target))}


class WriteBody(BaseModel):
    path: str
    text: str
    mtime: int = 0


class GitBody(BaseModel):
    action: str
    paths: list[str] = []
    message: str = ""


def _git(root: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run Git without a shell, prompts, hooks, pager, or an attacker-controlled executable path."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
           "GIT_CONFIG_NOSYSTEM": "1"}
    try:
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", root, *args],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(status_code=503, detail="Git is unavailable: " + str(e))


def _repo(root: str) -> str:
    p = _git(root, ["rev-parse", "--show-toplevel"])
    if p.returncode:
        raise HTTPException(status_code=404, detail="This workspace is not a Git repository")
    repo = os.path.realpath(p.stdout.strip())
    if os.path.commonpath([os.path.realpath(root), repo]) != os.path.realpath(root):
        raise HTTPException(status_code=403, detail="The repository is outside this workspace")
    return repo


@router.get("/git/status")
async def git_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    root = _guard(db, current_user); repo = _repo(root)
    p = _git(repo, ["status", "--porcelain=v1", "--branch", "-z", "--untracked-files=all"])
    if p.returncode:
        raise HTTPException(status_code=400, detail=(p.stderr or "git status failed").strip())
    rows = p.stdout.split("\0"); head = rows.pop(0) if rows else ""
    files = []
    for row in rows:
        if not row: continue
        files.append({"xy": row[:2], "path": row[3:]})
    remote = _git(repo, ["remote", "get-url", "origin"])
    return {"repo": _rel(repo, root), "branch": head[3:] if head.startswith("## ") else head,
            "files": files, "origin": remote.stdout.strip() if remote.returncode == 0 else "",
            "nostr": remote.returncode == 0 and remote.stdout.strip().startswith("nostr://")}


@router.get("/git/diff")
async def git_diff(path: str = Query(""), staged: bool = Query(False),
                   db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    root = _guard(db, current_user); repo = _repo(root)
    args = ["diff", "--no-ext-diff", "--no-color"] + (["--cached"] if staged else [])
    if path:
        target = _resolve(path, root, must_exist=False)
        args += ["--", _rel(target, repo)]
    p = _git(repo, args)
    if p.returncode:
        raise HTTPException(status_code=400, detail=(p.stderr or "git diff failed").strip())
    return {"diff": p.stdout[:500_000], "truncated": len(p.stdout) > 500_000}


@router.post("/git/action")
async def git_action(body: GitBody, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    root = _guard(db, current_user); repo = _repo(root); action = body.action.strip().lower()
    paths = []
    for path in body.paths[:500]:
        target = _resolve(path, root, must_exist=False)
        paths.append(_rel(target, repo))
    if action == "stage" and paths: args = ["add", "--", *paths]
    elif action == "unstage" and paths: args = ["restore", "--staged", "--", *paths]
    elif action == "restore" and paths:
        # VS Code's “Discard Changes” semantics. Tracked files are restored from HEAD (including a
        # staged copy); an untracked file has no HEAD copy, so discarding it means deleting that one
        # explicitly resolved file. Never pass an untracked path to `git clean`: its directory-level
        # behavior is broader than the item the person confirmed in the UI.
        for rel in paths:
            tracked = _git(repo, ["ls-files", "--error-unmatch", "--", rel])
            if tracked.returncode == 0:
                p = _git(repo, ["restore", "--staged", "--worktree", "--", rel])
                if p.returncode:
                    raise HTTPException(status_code=409, detail=(p.stderr or "Git restore failed").strip())
            else:
                target = _resolve(rel, repo, must_exist=False)
                if os.path.isfile(target) or os.path.islink(target):
                    os.unlink(target)
                elif os.path.exists(target):
                    raise HTTPException(status_code=409, detail="Refusing to discard a directory")
        return {"ok": True, "output": "Changes discarded"}
    elif action == "commit":
        msg = body.message.strip()
        if not msg or len(msg) > 5000: raise HTTPException(status_code=400, detail="Enter a commit message")
        args = ["commit", "--no-verify", "-m", msg]
    elif action in ("pull", "push"): args = [action, "--no-rebase"] if action == "pull" else ["push"]
    else: raise HTTPException(status_code=400, detail="Unsupported Git action")
    p = _git(repo, args, 120 if action in ("pull", "push") else 30)
    if p.returncode:
        raise HTTPException(status_code=409, detail=(p.stderr or p.stdout or "Git action failed").strip()[-4000:])
    return {"ok": True, "output": (p.stdout or p.stderr).strip()[-4000:]}


@router.post("/file")
async def write_file(body: WriteBody, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Save. Written to a TEMPORARY FILE BESIDE THE TARGET and renamed over it.

    `open(path,'w')` truncates first: a crash, a full disk or a killed worker between the truncate
    and the write leaves an EMPTY file where the person's code was. A rename within one directory is
    atomic, so the file is either the old one or the new one and never a half of either.

    `mtime` is a compare-and-swap, not decoration -- see below."""
    root = _guard(db, current_user)
    target = _resolve(body.path, root, must_exist=False)
    if os.path.isdir(target):
        raise HTTPException(status_code=400, detail="That is a directory")
    data = (body.text or "").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That file is too big to save here")
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise HTTPException(status_code=400, detail="That folder does not exist")
    # SOMEBODY ELSE CHANGED IT WHILE THIS TAB HELD IT OPEN. The terminal beside this editor is the
    # likeliest somebody -- a git pull, a formatter, another window on the same file. Refused rather
    # than merged: this endpoint cannot know which version is wanted, and overwriting silently is
    # the one outcome that loses work with nothing to say so.
    if body.mtime and os.path.exists(target):
        cur = int(os.path.getmtime(target))
        if cur != int(body.mtime):
            raise HTTPException(status_code=409,
                                detail="This file changed on disk since you opened it")
    tmp = os.path.join(parent, "." + os.path.basename(target) + ".pccode-tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Could not save: " + str(e))
    return {"ok": True, "path": _rel(target, root), "bytes": len(data),
            "mtime": int(os.path.getmtime(target))}
