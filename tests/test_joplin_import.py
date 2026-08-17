"""The Joplin importer, tested against a REAL .jex archive.

Run: venv-unified/bin/python -m pytest tests/test_joplin_import.py

Python builds the fixture with `tarfile` — a genuine uncompressed tar laid out exactly as Joplin
writes one — and node runs the actual shipped parser (static/js/client/joplin.js) over those bytes.
Neither side is a mock, so a change to the parser is checked against the format rather than against
a second copy of my own assumptions.

This exists because the FIRST attempt at Joplin import went badly. That one read Joplin's live
`database.sqlite` (scripts/migrate_joplin.py, since dead): it only ran on the machine Joplin was
installed on, broke when Joplin migrated its schema, and silently read nothing when the user had
E2EE turned on. The .jex is the stable input, and these are the specific ways parsing one goes
wrong:

  * the metadata block can only be found by walking BACKWARDS from the end. A note whose body
    contains a line like "todo: call the bank" splits in the wrong place under any forward scan,
    and that is normal prose, not a corner case.
  * a note with no body, and a folder (which never has one), must not lose their titles.
  * `:/<32hex>` links point at both resources AND other notes, so rewriting has to be selective.
  * an export made with E2EE on contains ciphertext and EMPTY titles/bodies. Importing it yields
    hundreds of blank notes that look exactly like data loss. It must be detected and reported.
  * a resource record with no bytes (exported without resources) must not become a broken
    attachment, and bytes with no record must not be dropped.
"""
import io
import json
import os
import shutil
import subprocess
import tarfile
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSER = os.path.join(ROOT, "static", "js", "client", "joplin.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _id(n):
    """A Joplin id: 32 lowercase hex."""
    return f"{n:032x}"


def _item(title, body, **props):
    """Serialize an item the way Joplin does: title, blank, body, blank, then `key: value` lines."""
    head = title if body == "" else f"{title}\n\n{body}"
    meta = "\n".join(f"{k}: {v}" for k, v in props.items())
    return f"{head}\n\n{meta}\n"


def _jex(entries):
    """entries: {name: bytes|str} → the bytes of an uncompressed tar, like a real .jex."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries.items():
            if isinstance(data, str):
                data = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _parse(jex_bytes, path=None):
    """Run the shipped parser under node over these bytes; return its result as a dict."""
    harness = textwrap.dedent(f"""
        const fs = require('fs');
        const J = require({json.dumps(PARSER)});
        const buf = fs.readFileSync(process.argv[2]);
        J.parseJex(new Uint8Array(buf)).then(r => {{
          // Uint8Array doesn't survive JSON — send each resource's length and first bytes instead.
          r.resources = r.resources.map(x => ({{...x, data: undefined,
            bytes: x.data ? Array.from(x.data.slice(0, 8)) : null,
            len: x.data ? x.data.length : 0 }}));
          process.stdout.write(JSON.stringify(r));
        }}).catch(e => {{ process.stdout.write(JSON.stringify({{error: e.message}})); }});
    """)
    tmp = path or "/tmp/pcai-joplin-fixture.jex"
    with open(tmp, "wb") as f:
        f.write(jex_bytes)
    hpath = "/tmp/pcai-joplin-harness.js"
    with open(hpath, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", hpath, tmp], capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()[:2000]
    return json.loads(out.stdout.decode())


NOTE_PROPS = dict(id=_id(1), parent_id=_id(100), created_time="2021-03-04T05:06:07.000Z",
                  updated_time="2022-04-05T06:07:08.000Z", is_conflict=0, is_todo=0,
                  markup_language=1, type_=1)


def test_a_normal_export_round_trips():
    r = _parse(_jex({
        f"{_id(1)}.md": _item("Shopping list", "milk\neggs", **NOTE_PROPS),
        f"{_id(100)}.md": _item("Groceries", "", id=_id(100), parent_id="",
                                created_time="2020-01-01T00:00:00.000Z", type_=2),
    }))
    assert "error" not in r, r.get("error")
    assert r["counts"]["notes"] == 1 and r["counts"]["folders"] == 1
    note = r["notes"][0]
    assert note["title"] == "Shopping list"
    assert note["body"] == "milk\neggs"
    assert note["parent_id"] == _id(100)
    # ISO-8601 in, epoch SECONDS out (2021-03-04T05:06:07Z).
    assert note["created"] == 1614834367, note["created"]
    assert r["folders"][0]["title"] == "Groceries"


def test_a_body_containing_key_colon_value_lines_is_not_eaten():
    """THE parsing bug. "todo: call the bank" is prose, not metadata — but it is indistinguishable
    from metadata to anything that scans forwards for the property block."""
    body = "Monday:\n\ntodo: call the bank\nnote: bring the letter\ntype_: 9"
    r = _parse(_jex({f"{_id(1)}.md": _item("Reminders", body, **NOTE_PROPS)}))
    assert "error" not in r, r.get("error")
    assert len(r["notes"]) == 1
    assert r["notes"][0]["title"] == "Reminders"
    assert r["notes"][0]["body"] == body, "the body was truncated at a prose line that looks like metadata"


def test_empty_body_and_folders_keep_their_titles():
    r = _parse(_jex({
        f"{_id(1)}.md": _item("Just a title", "", **NOTE_PROPS),
        f"{_id(100)}.md": _item("Work", "", id=_id(100), parent_id="", type_=2),
    }))
    assert r["notes"][0]["title"] == "Just a title"
    assert r["notes"][0]["body"] == ""
    assert r["folders"][0]["title"] == "Work"


def test_tags_and_note_tag_joins_resolve():
    r = _parse(_jex({
        f"{_id(1)}.md": _item("Tagged", "x", **NOTE_PROPS),
        f"{_id(50)}.md": _item("urgent", "", id=_id(50), type_=5),
        f"{_id(51)}.md": _item("work", "", id=_id(51), type_=5),
        f"{_id(60)}.md": _item("", "", id=_id(60), note_id=_id(1), tag_id=_id(50), type_=6),
        f"{_id(61)}.md": _item("", "", id=_id(61), note_id=_id(1), tag_id=_id(51), type_=6),
    }))
    assert {t["title"] for t in r["tags"]} == {"urgent", "work"}
    assert len(r["noteTags"]) == 2


def test_resources_are_matched_to_their_bytes():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    rid = _id(900)
    r = _parse(_jex({
        f"{_id(1)}.md": _item("With picture", f"before\n\n![diagram](:/{rid})\n\nafter", **NOTE_PROPS),
        f"{rid}.md": _item("diagram.png", "", id=rid, mime="image/png", filename="diagram.png",
                           file_extension="png", type_=4),
        f"resources/{rid}.png": png,
    }))
    assert len(r["resources"]) == 1
    res = r["resources"][0]
    assert res["id"] == rid and res["mime"] == "image/png"
    assert res["len"] == len(png)
    assert res["bytes"] == list(png[:8]), "the wrong bytes were attached to the resource"


def test_a_resource_record_without_bytes_is_dropped_and_reported():
    """An export made without resources: the record is there, the file is not. A broken attachment
    is worse than a missing one, because it looks like the picture was lost in OUR app."""
    rid = _id(901)
    r = _parse(_jex({
        f"{_id(1)}.md": _item("n", "b", **NOTE_PROPS),
        f"{rid}.md": _item("photo.jpg", "", id=rid, mime="image/jpeg", file_extension="jpg", type_=4),
    }))
    assert r["resources"] == []
    assert any("no file in the export" in w for w in r["warnings"]), r["warnings"]


def test_bytes_without_a_record_are_still_imported():
    """The mirror case: losing a picture because its bookkeeping entry didn't export is worse."""
    rid = _id(902)
    r = _parse(_jex({
        f"{_id(1)}.md": _item("n", "b", **NOTE_PROPS),
        f"resources/{rid}.png": b"\x89PNG\r\n\x1a\n",
    }))
    assert len(r["resources"]) == 1 and r["resources"][0]["id"] == rid


def test_an_encrypted_export_is_refused_loudly():
    """E2EE exports carry ciphertext and EMPTY titles/bodies. Importing them silently produces a
    wall of blank notes that reads as data loss."""
    r = _parse(_jex({
        f"{_id(i)}.md": _item("", "", id=_id(i), parent_id="", type_=1,
                              encryption_applied=1, encryption_cipher_text="JEDsomeciphertext")
        for i in range(1, 4)
    }))
    assert "error" in r
    assert "encrypted" in r["error"].lower(), r["error"]


def test_conflicts_and_todos_survive():
    r = _parse(_jex({
        f"{_id(1)}.md": _item("Buy milk", "", id=_id(1), parent_id=_id(100), is_todo=1,
                              todo_completed=0, is_conflict=0, type_=1),
        f"{_id(2)}.md": _item("Conflicted", "two devices", id=_id(2), parent_id=_id(100),
                              is_conflict=1, type_=1),
    }))
    by = {n["title"]: n for n in r["notes"]}
    assert by["Buy milk"]["todo"] is True and by["Buy milk"]["done"] is False
    assert by["Conflicted"]["conflict"] is True


def test_junk_entries_are_ignored_not_fatal():
    """Real archives carry a README or an .DS_Store; one unreadable file must not abort 3000 notes."""
    r = _parse(_jex({
        "README.md": "just some text with no metadata block at all\n",
        ".DS_Store": b"\x00\x01\x02",
        f"{_id(1)}.md": _item("Real note", "kept", **NOTE_PROPS),
    }))
    assert len(r["notes"]) == 1 and r["notes"][0]["title"] == "Real note"


def test_missing_timestamps_do_not_become_now():
    """0, not `now` — a missing date must not make a ten-year-old note sort as today's."""
    r = _parse(_jex({f"{_id(1)}.md": _item("No dates", "b", id=_id(1), parent_id="", type_=1)}))
    assert r["notes"][0]["created"] == 0 and r["notes"][0]["updated"] == 0


def test_link_rewriting_is_selective():
    """`:/id` is used for BOTH resources and note-to-note links, and a `:/` in prose is neither."""
    harness = textwrap.dedent(f"""
        const J = require({json.dumps(PARSER)});
        const body = "![a](:/{_id(900)}) and [note](:/{_id(1)}) and http:/not-a-link and (:/short)";
        const out = J.rewriteLinks(body, id => id === "{_id(900)}" ? "pcres:abc" : null);
        const ids = Array.from(J.linkedIds(body));
        process.stdout.write(JSON.stringify({{out, ids}}));
    """)
    with open("/tmp/pcai-joplin-links.js", "w") as f:
        f.write(harness)
    res = subprocess.run(["node", "/tmp/pcai-joplin-links.js"], capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr.decode()[:2000]
    r = json.loads(res.stdout.decode())
    assert "![a](pcres:abc)" in r["out"], r["out"]
    assert f"[note](:/{_id(1)})" in r["out"], "an unmapped link must be left alone, not blanked"
    assert "http:/not-a-link" in r["out"]
    assert set(r["ids"]) == {_id(900), _id(1)}


def test_front_matter_export_is_supported():
    """The other export Joplin (and its mobile app) produces."""
    md = textwrap.dedent("""\
        ---
        title: From front matter
        updated: 2023-05-06T07:08:09.000Z
        tags:
          - work
          - urgent
        ---

        the body
        with: a colon line
        """)
    harness = textwrap.dedent(f"""
        const J = require({json.dumps(PARSER)});
        const r = J.parseMarkdownFiles([{{name:'Work/note.md', text:{json.dumps(md)}}}]);
        process.stdout.write(JSON.stringify(r));
    """)
    with open("/tmp/pcai-joplin-fm.js", "w") as f:
        f.write(harness)
    res = subprocess.run(["node", "/tmp/pcai-joplin-fm.js"], capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr.decode()[:2000]
    r = json.loads(res.stdout.decode())
    n = r["notes"][0]
    assert n["title"] == "From front matter"
    assert n["tagNames"] == ["work", "urgent"]
    assert "with: a colon line" in n["body"]
    assert r["folders"][0]["title"] == "Work"


def test_streaming_parse_matches_the_in_memory_parse():
    """The streaming reader is the ONLY path a .jex takes now, so it has to agree with the
    in-memory one exactly.

    It exists because `await file.arrayBuffer()` cannot read a real library: the export this was
    rebuilt against is 2.17 GB, and Chrome refuses to materialise a blob that size, throwing a
    NotReadableError whose message blames *permissions* — so a perfectly good export looked like a
    file the browser wasn't allowed to open. A tar is a flat sequence of headers, so the archive is
    indexed by reading headers and jumping over payloads, and attachments stay on disk as
    {offset,length} handles until each one is uploaded.
    """
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
    rid = _id(900)
    entries = {
        f"{_id(1)}.md": _item("With picture", f"see ![x](:/{rid})", **NOTE_PROPS),
        f"{_id(2)}.md": _item("Plain", "no attachments here", id=_id(2), parent_id=_id(100), type_=1),
        f"{_id(100)}.md": _item("Folder", "", id=_id(100), parent_id="", type_=2),
        f"{rid}.md": _item("pic.png", "", id=rid, mime="image/png", filename="pic.png",
                           file_extension="png", type_=4),
        f"resources/{rid}.png": png,
    }
    jex = _jex(entries)
    path = "/tmp/pcai-joplin-stream.jex"
    with open(path, "wb") as f:
        f.write(jex)

    harness = textwrap.dedent(f"""
        const {{ openAsBlob }} = require('fs');
        const J = require({json.dumps(PARSER)});
        (async () => {{
          // openAsBlob gives a lazy, file-backed Blob — the same shape a browser <input type=file>
          // hands over, and it never reads the file into memory.
          const blob = await openAsBlob({json.dumps(path)});
          const streamed = await J.parseJexFile(blob, {{}});
          const inMem = await J.parseJex(new Uint8Array(require('fs').readFileSync({json.dumps(path)})));
          const strip = r => ({{ notes: r.notes, folders: r.folders, tags: r.tags,
                                counts: r.counts, warnings: r.warnings }});
          // The one intended difference: streamed resources carry a lazy handle, not bytes.
          const res = streamed.resources[0];
          const bytes = await J.readResource(blob, res);
          process.stdout.write(JSON.stringify({{
            same: JSON.stringify(strip(streamed)) === JSON.stringify(strip(inMem)),
            streamedCounts: streamed.counts,
            lazy: !!(res.data && res.data.lazy),
            resLen: bytes.length,
            resHead: Array.from(bytes.slice(0, 8)),
            inMemHead: Array.from(inMem.resources[0].data.slice(0, 8)),
          }}));
        }})().catch(e => {{ process.stdout.write(JSON.stringify({{error: e.message}})); }});
    """)
    hpath = "/tmp/pcai-joplin-stream-harness.js"
    with open(hpath, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", hpath], capture_output=True, timeout=120)
    assert out.returncode == 0, out.stderr.decode()[:2000]
    r = json.loads(out.stdout.decode())
    assert "error" not in r, r.get("error")
    assert r["same"], "the streaming parse disagrees with the in-memory parse"
    assert r["streamedCounts"]["notes"] == 2
    assert r["lazy"], "a streamed resource must stay on disk as a handle, not be read into memory"
    assert r["resLen"] == len(png)
    assert r["resHead"] == list(png[:8]) == r["inMemHead"], "the lazy read returned the wrong bytes"


def test_streaming_reads_the_right_bytes_for_every_resource():
    """Offset arithmetic: one wrong length and every LATER resource reads shifted bytes — each file
    still has the right SIZE, so the import "succeeds" and quietly stores 1200 corrupt images."""
    entries = {}
    expect = {}
    for i in range(1, 61):
        rid = _id(500 + i)
        # Deliberately odd sizes so nothing lands neatly on tar's 512-byte block boundary.
        blob = bytes([i % 256]) * (i * 337 + 7)
        entries[f"{rid}.md"] = _item(f"r{i}.bin", "", id=rid, mime="application/octet-stream",
                                     filename=f"r{i}.bin", file_extension="bin", type_=4)
        entries[f"resources/{rid}.bin"] = blob
        expect[rid] = [blob[0], len(blob)]
    entries[f"{_id(1)}.md"] = _item("n", "b", **NOTE_PROPS)
    path = "/tmp/pcai-joplin-many.jex"
    with open(path, "wb") as f:
        f.write(_jex(entries))

    harness = textwrap.dedent(f"""
        const {{ openAsBlob }} = require('fs');
        const J = require({json.dumps(PARSER)});
        (async () => {{
          const blob = await openAsBlob({json.dumps(path)});
          const r = await J.parseJexFile(blob, {{}});
          const got = {{}};
          for(const res of r.resources){{
            const b = await J.readResource(blob, res);
            got[res.id] = [b[0], b.length];
          }}
          process.stdout.write(JSON.stringify({{got, n: r.resources.length}}));
        }})().catch(e => {{ process.stdout.write(JSON.stringify({{error: e.message}})); }});
    """)
    hpath = "/tmp/pcai-joplin-many-harness.js"
    with open(hpath, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", hpath], capture_output=True, timeout=120)
    assert out.returncode == 0, out.stderr.decode()[:2000]
    r = json.loads(out.stdout.decode())
    assert "error" not in r, r.get("error")
    assert r["n"] == 60, r["n"]
    assert r["got"] == expect, "a lazily-read resource returned the wrong bytes"


def test_backup_archive_round_trips_and_is_a_real_tar():
    """The backup export writes a .jex, and the importer must read it back losslessly.

    One format, one code path: the backup is not a private side-format only this app understands —
    a backup that depends on this app still existing fails at the one job a backup has. So it is a
    genuine Joplin archive, which means (a) it round-trips through the importer and (b) GNU tar can
    list it, which is the same thing Joplin's own reader needs.

    The body deliberately contains "todo: call the bank" — the line that breaks any parser scanning
    forwards for the metadata block.
    """
    harness = textwrap.dedent(f"""
        const J = require({json.dumps(PARSER)});
        const fs = require('fs');
        const enc8 = new TextEncoder();
        const hex32 = v => String(v||'').replace(/[^0-9a-f]/gi,'').toLowerCase().padEnd(32,'0').slice(0,32);
        const parts = [];
        const entry = (name, bytes) => {{ parts.push(J.tarHeader(name, bytes.length), bytes, J.tarPad(bytes.length)); }};
        const fid = hex32('aa11'), nid = hex32('bb22'), rid = hex32('cc33');
        entry(fid+'.md', enc8.encode(J.serializeItem('Family/Tax Returns','',
          {{id:fid, created_time:J._iso(1000), updated_time:J._iso(2000), parent_id:'', type_:2}})));
        // Built from an array rather than written with escapes: this harness lives inside a Python
        // f-string, where a backslash-n would become a real newline before node ever sees it.
        const NL = String.fromCharCode(10);
        const body = ["line one", "", "todo: call the bank", "", "![pic](:/"+rid+")"].join(NL);
        entry(nid+'.md', enc8.encode(J.serializeItem('Round trip', body,
          {{id:nid, parent_id:fid, created_time:J._iso(1614834367), updated_time:J._iso(1700000000),
            is_conflict:0, is_todo:0, type_:1}})));
        const png = new Uint8Array([0x89,0x50,0x4e,0x47,1,2,3,4,5,6,7,8,9]);
        entry(rid+'.md', enc8.encode(J.serializeItem('pic.png','',
          {{id:rid, mime:'image/png', filename:'pic.png', file_extension:'png', size:png.length, type_:4}})));
        entry('resources/'+rid+'.png', png);
        parts.push(J.tarEnd());
        const total = parts.reduce((n,p)=>n+p.length,0);
        const buf = new Uint8Array(total); let o=0; for(const p of parts){{ buf.set(p,o); o+=p.length; }}
        fs.writeFileSync('/tmp/pcai-backup-roundtrip.jex', buf);
        (async () => {{
          const r = await J.parseJex(buf);
          const n = r.notes[0];
          process.stdout.write(JSON.stringify({{
            notes:r.notes.length, folders:r.folders.length, resources:r.resources.length,
            title:n.title, bodyIntact:n.body===body, created:n.created, updated:n.updated,
            folderTitle:r.folders[0].title, parentLinked:n.parent_id===fid,
            resMime:r.resources[0].mime, resHead:Array.from(r.resources[0].data.slice(0,4)),
            warnings:r.warnings,
          }}));
        }})().catch(e => process.stdout.write(JSON.stringify({{error:e.message}})));
    """)
    hpath = "/tmp/pcai-backup-harness.js"
    with open(hpath, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", hpath], capture_output=True, timeout=120)
    assert out.returncode == 0, out.stderr.decode()[:2000]
    r = json.loads(out.stdout.decode())
    assert "error" not in r, r.get("error")
    assert (r["notes"], r["folders"], r["resources"]) == (1, 1, 1), r
    assert r["title"] == "Round trip"
    assert r["bodyIntact"], "the note body did not survive the round trip"
    assert r["created"] == 1614834367 and r["updated"] == 1700000000, "timestamps were lost"
    assert r["folderTitle"] == "Family/Tax Returns", "the folder path was lost"
    assert r["parentLinked"], "the note lost its folder"
    assert r["resMime"] == "image/png"
    assert r["resHead"] == [137, 80, 78, 71], "the attachment bytes are wrong"
    assert r["warnings"] == []

    # …and a real tar reader must accept it, which is what Joplin's own importer needs.
    with tarfile.open("/tmp/pcai-backup-roundtrip.jex") as t:
        names = sorted(m.name for m in t if m.isfile())
    assert len(names) == 4, names
    assert any(n.startswith("resources/") for n in names)
    with tarfile.open("/tmp/pcai-backup-roundtrip.jex") as t:
        png = t.extractfile([m for m in t if m.name.startswith("resources/")][0]).read()
    assert png[:4] == b"\x89PNG", "GNU tar read the attachment back wrong"


def test_a_large_export_parses_whole():
    """Tar reading is offset arithmetic: one bad length silently truncates the archive, and the
    result still looks like a successful import of fewer notes."""
    folder = _id(0x1000)   # outside the note id range — _id(100) would collide with note 100
    entries = {}
    for i in range(1, 501):
        entries[f"{_id(i)}.md"] = _item(f"Note {i}", "x" * (i * 7), id=_id(i),
                                        parent_id=folder, type_=1)
    entries[f"{folder}.md"] = _item("Big folder", "", id=folder, parent_id="", type_=2)
    r = _parse(_jex(entries))
    assert r["counts"]["notes"] == 500, r["counts"]
    assert {n["title"] for n in r["notes"]} == {f"Note {i}" for i in range(1, 501)}


# ---------------------------------------------------------------------------------------------
# THE SHAPES A REAL EXPORT HAS AND A HAND-BUILT FIXTURE DOES NOT.
#
# The parser tests above build tidy archives. A .jex written by Joplin itself is messier in ways that
# are individually boring and collectively how an import fails on somebody's real file: a directory
# entry before the items, CRLF, a BOM, a note whose body ends without a newline, an id in capitals,
# a resource whose record and bytes are in the opposite order, and a `resources/` prefix that may or
# may not be there.
#
# Each of these is a separate case because when one breaks, the failure has to name which.
# ---------------------------------------------------------------------------------------------


def test_a_directory_entry_in_the_tar_is_not_read_as_an_item():
    """Real archives carry `resources/` as its own zero-length entry with typeflag 5."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        d = tarfile.TarInfo("resources")
        d.type = tarfile.DIRTYPE
        d.size = 0
        tar.addfile(d)
        nid = _id(1)
        data = _item("Real note", "body", id=nid, type_=1, parent_id="",
                     created_time="2020-01-01T00:00:00.000Z",
                     updated_time="2020-01-01T00:00:00.000Z").encode()
        info = tarfile.TarInfo(nid + ".md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    got = _parse(buf.getvalue())
    assert got.get("error") is None, got
    assert [n["title"] for n in got["notes"]] == ["Real note"], got["notes"]


def test_crlf_line_endings_do_not_swallow_the_metadata():
    """A .jex made on Windows. The metadata block is found by walking backwards from the end, and a
    trailing \\r on every line is exactly the sort of thing that makes `key: value` stop matching."""
    nid = _id(2)
    body = "line one\r\n\r\ntodo: call the bank"
    raw = _item("CRLF note", body, id=nid, type_=1, parent_id="",
                created_time="2020-01-01T00:00:00.000Z",
                updated_time="2020-01-02T00:00:00.000Z").replace("\n", "\r\n")
    got = _parse(_jex({nid + ".md": raw}))
    assert got.get("error") is None, got
    assert len(got["notes"]) == 1, got
    n = got["notes"][0]
    assert n["title"] == "CRLF note", n
    assert "todo: call the bank" in n["body"], n["body"]
    assert n["id"] == nid, n


def test_a_byte_order_mark_does_not_become_part_of_the_title():
    nid = _id(3)
    raw = "﻿" + _item("BOM note", "body", id=nid, type_=1, parent_id="",
                           created_time="2020-01-01T00:00:00.000Z",
                           updated_time="2020-01-01T00:00:00.000Z")
    got = _parse(_jex({nid + ".md": raw}))
    assert got.get("error") is None, got
    assert got["notes"][0]["title"] == "BOM note", repr(got["notes"][0]["title"])


def test_an_item_with_no_trailing_newline_still_parses():
    nid = _id(4)
    raw = _item("No newline", "body", id=nid, type_=1, parent_id="",
                created_time="2020-01-01T00:00:00.000Z",
                updated_time="2020-01-01T00:00:00.000Z").rstrip("\n")
    got = _parse(_jex({nid + ".md": raw}))
    assert got.get("error") is None, got
    assert got["notes"][0]["title"] == "No newline", got["notes"]


def test_a_resource_whose_bytes_come_before_its_record_is_still_matched():
    """Tar order is whatever the exporter felt like. Matching must not depend on it."""
    rid = _id(5)
    nid = _id(6)
    entries = {}
    entries["resources/" + rid + ".bin"] = b"\x89PNG\r\n\x1a\nrest"
    entries[rid + ".md"] = _item("pic.png", "", id=rid, type_=4, mime="image/png",
                                 filename="pic.png", file_extension="png",
                                 created_time="2020-01-01T00:00:00.000Z",
                                 updated_time="2020-01-01T00:00:00.000Z")
    entries[nid + ".md"] = _item("Has a picture", "![pic](:/%s)" % rid, id=nid, type_=1,
                                 parent_id="", created_time="2020-01-01T00:00:00.000Z",
                                 updated_time="2020-01-01T00:00:00.000Z")
    got = _parse(_jex(entries))
    assert got.get("error") is None, got
    assert len(got["resources"]) == 1, got["resources"]
    r = got["resources"][0]
    assert r["len"] == 12, r
    assert r["bytes"][:4] == [0x89, 0x50, 0x4E, 0x47], r


def test_an_uppercase_id_still_links_to_its_resource():
    """Joplin ids are lowercase hex, and other tools that write .jex are not always so careful."""
    rid = _id(7).upper()
    nid = _id(8)
    entries = {
        rid + ".md": _item("shot.png", "", id=rid, type_=4, mime="image/png",
                           filename="shot.png", file_extension="png",
                           created_time="2020-01-01T00:00:00.000Z",
                           updated_time="2020-01-01T00:00:00.000Z"),
        "resources/" + rid + ".png": b"bytes-here",
        nid + ".md": _item("Note", "![shot](:/%s)" % rid, id=nid, type_=1, parent_id="",
                           created_time="2020-01-01T00:00:00.000Z",
                           updated_time="2020-01-01T00:00:00.000Z"),
    }
    got = _parse(_jex(entries))
    assert got.get("error") is None, got
    assert len(got["resources"]) == 1 and got["resources"][0]["len"] == 10, got["resources"]


def test_a_note_that_is_only_a_title_keeps_an_empty_body_not_the_metadata():
    """The one that eats an import silently: with no body, the metadata block is everything after
    the title, and a parser that takes 'the rest' as the body files the key/value lines as prose."""
    nid = _id(9)
    got = _parse(_jex({nid + ".md": _item("Title only", "", id=nid, type_=1, parent_id="",
                                          created_time="2020-01-01T00:00:00.000Z",
                                          updated_time="2020-01-01T00:00:00.000Z")}))
    assert got.get("error") is None, got
    n = got["notes"][0]
    assert n["title"] == "Title only", n
    assert n["body"].strip() == "", repr(n["body"])
    assert "created_time" not in n["body"], "the metadata block was filed as the note's text"
