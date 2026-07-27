"""Instance custom emoji — Pleroma/Akkoma-style packs, served to the Nostr client.

Layout is deliberately THE SAME as Pleroma/Akkoma's so an existing pack can just be copied in:

    <custom_emoji_dir>/<pack>/<files…>        ← one directory per pack
    <custom_emoji_dir>/<pack>/pack.json       ← optional {"files": {shortcode: filename}}
    <custom_emoji_dir>/<file>                 ← loose images form the implicit "_" pack

A pack that carries `pack.json` is described BY it (the akkoma packs name their files with UUIDs,
so the map is the only place the shortcodes exist); one without takes every image in the directory
with the filename stem as the shortcode. Both are writable from Admin → Custom Emoji: whichever
form a pack already uses is the form its edits keep.

The index is cached — a pack can hold thousands of files and the picker asks for the whole list on
every open. Staleness is decided by the pack directories' mtimes (rechecked at most every few
seconds), so an emoji uploaded through the admin UI shows up without a restart.

Nothing here is Nostr-specific: the client turns each entry into a NIP-30 `["emoji", <shortcode>,
<url>]` tag when it publishes, which is what makes the emoji render in OTHER clients too.
"""
import io
import json
import logging
import os
import re
import shutil
import time
from typing import Dict, List, Optional, Tuple

from app.services import settings_store

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DIR = "assets/emoji"
ROOT_PACK = "_"                     # loose images sitting directly in the emoji dir
IMAGE_EXTS = {".png", ".gif", ".webp", ".jpg", ".jpeg", ".apng", ".avif"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # per file; these end up inline in other people's timelines
THUMB_PX = 72

# The shortcode charset is the one the CLIENT's :shortcode: regex accepts — anything else would be
# written into a note and never render, so it is sanitised on the way in rather than served broken.
_SC_RE = re.compile(r"^[A-Za-z0-9_+\-]+$")
_SC_BAD = re.compile(r"[^A-Za-z0-9_+\-]+")

_cache: Dict[str, object] = {"sig": None, "at": 0.0, "entries": [], "by_key": {}}
_SIG_TTL = 5.0                      # don't re-stat the pack dirs more than this often


def emoji_dir() -> str:
    """Configured emoji directory (absolute), or "" when the feature is switched off (blank setting).
    A relative setting is resolved against the repo root so the default works on every node."""
    raw = (settings_store.get("custom_emoji_dir") or DEFAULT_DIR).strip()
    if not raw:
        return ""
    return raw if os.path.isabs(raw) else os.path.join(_REPO_ROOT, raw)


def sanitize_shortcode(sc: str) -> str:
    """A usable shortcode, or "" if nothing usable is left."""
    sc = (sc or "").strip().strip(":")
    sc = _SC_BAD.sub("_", sc).strip("_")
    return sc if sc and _SC_RE.match(sc) else ""


def _pack_dirs(root: str) -> List[str]:
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
                      and not d.startswith("."))
    except OSError:
        return []


def _signature(root: str) -> Optional[Tuple]:
    """Cheap staleness key: the emoji dir's mtime plus each pack dir's. Adding, renaming or deleting
    a file changes its directory's mtime, which is exactly when the index must be rebuilt."""
    if not root or not os.path.isdir(root):
        return None
    try:
        sig = [os.stat(root).st_mtime_ns]
        for p in _pack_dirs(root):
            try:
                sig.append(os.stat(os.path.join(root, p)).st_mtime_ns)
            except OSError:
                sig.append(0)
        return (root, tuple(sig))
    except OSError:
        return None


def _pack_json_path(pack_dir: str) -> str:
    return os.path.join(pack_dir, "pack.json")


def read_pack_json(pack_dir: str) -> Optional[dict]:
    p = _pack_json_path(pack_dir)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) and isinstance(doc.get("files"), dict) else None
    except Exception as e:
        logger.warning("[emoji] unreadable pack.json in %s: %s", pack_dir, e)
        return None


def _write_pack_json(pack_dir: str, doc: dict) -> None:
    doc["files_count"] = len(doc.get("files") or {})
    tmp = _pack_json_path(pack_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _pack_json_path(pack_dir))


def _scan_pack(root: str, pack: str) -> List[Tuple[str, str]]:
    """[(shortcode, absolute path)] for one pack, pack.json first (see module docstring)."""
    pack_dir = root if pack == ROOT_PACK else os.path.join(root, pack)
    out: List[Tuple[str, str]] = []
    doc = read_pack_json(pack_dir)
    if doc is not None:
        for sc, rel in (doc.get("files") or {}).items():
            if not isinstance(rel, str) or not rel:
                continue
            path = os.path.normpath(os.path.join(pack_dir, rel))
            # A pack.json routinely outlives some of its files (16 of the 3352 in the pack this was
            # built against) — skip the dangling entries instead of serving 404s into the picker.
            # The prefix test is against `dir + sep` and not the bare directory: a `files` entry of
            # "../packs2/x.png" must not pass just because "/e/packs2" starts with "/e/pack".
            if not path.startswith(os.path.abspath(pack_dir) + os.sep) or not os.path.isfile(path):
                continue
            out.append((sc, path))
        return out
    try:
        names = sorted(os.listdir(pack_dir))
    except OSError:
        return out
    for name in names:
        path = os.path.join(pack_dir, name)
        if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            continue
        out.append((os.path.splitext(name)[0], path))
    return out


def index(force: bool = False) -> List[dict]:
    """Every instance emoji: [{shortcode, pack, path, ext}], shortcodes unique across all packs
    (a NIP-30 tag map is flat, so the first pack to claim a shortcode keeps it)."""
    root = emoji_dir()
    now = time.monotonic()
    if not force and _cache["entries"] and (now - float(_cache["at"] or 0)) < _SIG_TTL:
        return _cache["entries"]                                    # type: ignore[return-value]
    sig = _signature(root)
    if not force and sig is not None and sig == _cache["sig"]:
        _cache["at"] = now
        return _cache["entries"]                                    # type: ignore[return-value]
    entries: List[dict] = []
    seen: Dict[str, str] = {}
    if root and os.path.isdir(root):
        for pack in [ROOT_PACK] + _pack_dirs(root):
            for raw_sc, path in _scan_pack(root, pack):
                sc = sanitize_shortcode(raw_sc)
                if not sc or sc in seen:
                    continue
                seen[sc] = pack
                entries.append({"shortcode": sc, "pack": pack, "path": path,
                                "ext": os.path.splitext(path)[1].lower()})
    entries.sort(key=lambda e: (e["pack"], e["shortcode"].lower()))
    _cache.update({"sig": sig, "at": now, "entries": entries,
                   "by_key": {(e["pack"], e["shortcode"]): e for e in entries}})
    return entries


def lookup(pack: str, shortcode: str) -> Optional[dict]:
    """One entry by (pack, shortcode) — the index is the ONLY path from a URL to a file on disk, so
    a request can never name a path of its own."""
    index()
    return _cache["by_key"].get((pack, shortcode))                  # type: ignore[union-attr]


def packs() -> List[dict]:
    """[{name, count, bytes, managed}] for the admin UI. `managed` = described by a pack.json."""
    root = emoji_dir()
    by_pack: Dict[str, List[dict]] = {}
    for e in index():
        by_pack.setdefault(e["pack"], []).append(e)
    out = []
    for name, items in by_pack.items():
        total = 0
        for e in items:
            try:
                total += os.path.getsize(e["path"])
            except OSError:
                pass
        pack_dir = root if name == ROOT_PACK else os.path.join(root, name)
        out.append({"name": name, "count": len(items), "bytes": total,
                    "managed": read_pack_json(pack_dir) is not None})
    out.sort(key=lambda p: p["name"])
    return out


def stats() -> dict:
    root = emoji_dir()
    ps = packs()
    return {"dir": root, "exists": bool(root and os.path.isdir(root)),
            "count": sum(p["count"] for p in ps), "bytes": sum(p["bytes"] for p in ps),
            "packs": ps}


# ---------------------------------------------------------------------------------------------
# Thumbnails
#
# The picker shows the whole instance at once, and a real pack is not thumbnail-sized: the one this
# was built against is 308 MB across 3352 files (median 26 KB, largest 4 MB). Scrolling that at full
# size would be hundreds of megabytes on a phone, so the GRID gets a cached 72px still and only the
# emoji actually used in a note is ever served full size.
# ---------------------------------------------------------------------------------------------
def _thumb_path(pack: str, shortcode: str) -> str:
    return os.path.join(_REPO_ROOT, "data", "emoji-thumbs", pack, shortcode + ".webp")


def thumbnail(pack: str, shortcode: str) -> Optional[str]:
    """Path to a cached 72px still for this emoji, generating it on first use. Returns None (caller
    falls back to the original) if Pillow can't read it — an emoji is never worth a 500."""
    e = lookup(pack, shortcode)
    if not e:
        return None
    src, dst = e["path"], _thumb_path(pack, shortcode)
    try:
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
    except OSError:
        pass
    try:
        from PIL import Image
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with Image.open(src) as im:
            im.seek(0)                                  # animated source → first frame; a still grid
            im = im.convert("RGBA")
            im.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=82, method=4)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(buf.getvalue())
        os.replace(tmp, dst)
        return dst
    except Exception as e2:
        logger.debug("[emoji] thumbnail failed for %s:%s (%s)", pack, shortcode, e2)
        return None


# ---------------------------------------------------------------------------------------------
# Mutations (Admin → Custom Emoji). Every one of them ends with an index invalidation so the next
# picker open — and the next admin list — sees the change without a restart.
# ---------------------------------------------------------------------------------------------
def _invalidate() -> None:
    _cache.update({"sig": None, "at": 0.0})


def _require_dir() -> str:
    root = emoji_dir()
    if not root:
        raise ValueError("custom emoji directory is not configured")
    os.makedirs(root, exist_ok=True)
    return root


def _pack_dir(pack: str, create: bool = False) -> str:
    """Directory of an EXISTING pack, matched against the real listing — a request never gets to
    build a path. `create=True` (upload into a pack that isn't there yet) additionally accepts a
    brand-new name, but only a clean one: sanitising instead would turn "../../etc" into a silently
    created pack called "etc"."""
    root = _require_dir()
    if pack == ROOT_PACK:
        return root
    if pack in _pack_dirs(root):
        return os.path.join(root, pack)     # whatever the on-disk name is, spaces and all
    if not create:
        raise ValueError(f"no such pack: {pack}")
    if not _SC_RE.match(pack or ""):
        raise ValueError("invalid pack name")
    d = os.path.join(root, pack)
    os.makedirs(d, exist_ok=True)
    return d


def create_pack(name: str) -> str:
    """A new empty pack. The name must already be clean (letters, digits, _ + -) rather than being
    coerced into one — see _pack_dir."""
    name = (name or "").strip()
    if not _SC_RE.match(name):
        raise ValueError("invalid pack name — use letters, digits, _ - and + only")
    os.makedirs(os.path.join(_require_dir(), name), exist_ok=True)
    _invalidate()
    return name


def delete_pack(pack: str) -> int:
    """Delete a whole pack directory. Returns how many emoji went with it."""
    if pack == ROOT_PACK:
        raise ValueError("the loose-files pack cannot be deleted")
    d = _pack_dir(pack)
    n = len([e for e in index() if e["pack"] == pack])
    shutil.rmtree(d)
    shutil.rmtree(os.path.join(_REPO_ROOT, "data", "emoji-thumbs", pack), ignore_errors=True)
    _invalidate()
    return n


def add_emoji(pack: str, shortcode: str, filename: str, data: bytes, overwrite: bool = False) -> dict:
    """Store one uploaded image as `shortcode` in `pack`. A pack.json pack keeps the uploaded
    filename and gains a map entry; a plain pack is named by its shortcode (the filename IS the
    shortcode there, so anything else would rename the emoji behind the admin's back)."""
    sc = sanitize_shortcode(shortcode or os.path.splitext(filename or "")[0])
    if not sc:
        raise ValueError("invalid shortcode")
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in IMAGE_EXTS:
        raise ValueError(f"unsupported image type: {ext or '(none)'}")
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"too large ({len(data)/1048576:.1f} MB, max {MAX_UPLOAD_BYTES//1048576} MB)")
    existing = lookup(pack, sc)
    if existing and not overwrite:
        raise ValueError(f":{sc}: already exists in {pack}")
    d = _pack_dir(pack, create=True)
    doc = read_pack_json(d)
    if doc is not None:
        base = os.path.basename(filename or (sc + ext))
        base = re.sub(r"[^A-Za-z0-9_.\-]+", "_", base) or (sc + ext)
        target = os.path.join(d, base)
        i = 1
        while os.path.exists(target) and (not existing or os.path.abspath(target) != existing["path"]):
            stem, e2 = os.path.splitext(base)
            target = os.path.join(d, f"{stem}_{i}{e2}")
            i += 1
        with open(target, "wb") as fh:
            fh.write(data)
        doc.setdefault("files", {})[sc] = os.path.basename(target)
        _write_pack_json(d, doc)
    else:
        if existing:
            try:
                os.remove(existing["path"])                 # a re-upload may change the extension
            except OSError:
                pass
        with open(os.path.join(d, sc + ext), "wb") as fh:
            fh.write(data)
    _drop_thumb(pack, sc)
    _invalidate()
    return {"shortcode": sc, "pack": pack}


def rename_emoji(pack: str, shortcode: str, new_shortcode: str) -> dict:
    sc = sanitize_shortcode(new_shortcode)
    if not sc:
        raise ValueError("invalid shortcode")
    e = lookup(pack, shortcode)
    if not e:
        raise ValueError("no such emoji")
    if sc != shortcode and lookup(pack, sc):
        raise ValueError(f":{sc}: already exists in {pack}")
    d = _pack_dir(pack)
    doc = read_pack_json(d)
    if doc is not None:
        files = doc.setdefault("files", {})
        files[sc] = files.pop(shortcode, os.path.basename(e["path"]))
        _write_pack_json(d, doc)
    else:
        os.rename(e["path"], os.path.join(d, sc + e["ext"]))
    _drop_thumb(pack, shortcode)
    _invalidate()
    return {"shortcode": sc, "pack": pack}


def delete_emoji(pack: str, shortcode: str) -> None:
    e = lookup(pack, shortcode)
    if not e:
        raise ValueError("no such emoji")
    d = _pack_dir(pack)
    doc = read_pack_json(d)
    if doc is not None:
        (doc.get("files") or {}).pop(shortcode, None)
        _write_pack_json(d, doc)
    try:
        os.remove(e["path"])
    except OSError:
        pass
    _drop_thumb(pack, shortcode)
    _invalidate()


def _drop_thumb(pack: str, shortcode: str) -> None:
    try:
        os.remove(_thumb_path(pack, shortcode))
    except OSError:
        pass
