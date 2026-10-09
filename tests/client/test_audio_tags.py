"""audiotags.js reads a music file's own title, artist, album and cover — as Ditto and Armada show them.

"try to get it to show the audio metadata like ditto/amethyst does it". The shipped parser runs under node
on REAL tag bytes built here byte by byte, one per format a posted track arrives in: ID3v2.3 (UTF-16 with a
BOM, the common one), ID3v2.4 (UTF-8, syncsafe frame sizes), ID3v2.2 (three-letter frames), FLAC
(VORBIS_COMMENT + PICTURE) and Ogg Opus whose comment packet — cover art included — spans several pages.
A file it does not understand must answer {} and never throw.
"""
import base64
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
JPEG = b"\xff\xd8\xff\xe0" + b"J" * 300 + b"\xff\xd9"


def syncsafe(n):
    return bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])


def id3v23():
    def frame(fid, body):
        return fid.encode() + struct.pack(">I", len(body)) + b"\0\0" + body
    u16 = lambda s: b"\x01\xff\xfe" + s.encode("utf-16-le")
    frames = (frame("TIT2", u16("Playground")) + frame("TPE1", u16("Bea Miller"))
              + frame("TALB", u16("Arcane: League of Legends")) + frame("APIC", b"\x00image/jpeg\x00\x03\x00" + JPEG))
    body = frames + b"\0" * 64
    return b"ID3\x03\x00\x00" + syncsafe(len(body)) + body + b"\xff\xfb" * 100


def id3v24():
    def frame(fid, body):
        return fid.encode() + syncsafe(len(body)) + b"\0\0" + body
    body = frame("TIT2", b"\x03" + "Ação Noturna".encode()) + frame("TPE1", b"\x03Baunilha") + b"\0" * 20
    return b"ID3\x04\x00\x00" + syncsafe(len(body)) + body


def id3v22():
    def frame(fid, body):
        return fid.encode() + struct.pack(">I", len(body))[1:] + body
    body = frame("TT2", b"\x00Old Song") + frame("TP1", b"\x00Old Band") + frame("PIC", b"\x00JPG\x03\x00" + JPEG)
    return b"ID3\x02\x00\x00" + syncsafe(len(body)) + body


def vorbis_comments(vendor, items):
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(items))
    for s in items:
        b = s.encode()
        out += struct.pack("<I", len(b)) + b
    return out


def flac_picture():
    mime = b"image/png"
    return (struct.pack(">I", 3) + struct.pack(">I", len(mime)) + mime + struct.pack(">I", 0)
            + struct.pack(">IIII", 1, 1, 24, 0) + struct.pack(">I", 8) + b"\x89PNGfake")


def flac():
    vc = vorbis_comments(b"ref", ["TITLE=Flac Title", "ARTIST=Flac Artist", "ALBUM=Flac Album"])
    pic = flac_picture()
    blk = lambda t, last, data: bytes([t | (0x80 if last else 0)]) + struct.pack(">I", len(data))[1:] + data
    streaminfo = b"\0" * 34
    return b"fLaC" + blk(0, False, streaminfo) + blk(4, False, vc) + blk(6, True, pic)


def ogg_opus():
    head = b"OpusHead" + bytes([1, 2]) + b"\0" * 9
    pic = base64.b64encode(flac_picture() + b"x" * 1500).decode()      # forces the packet across pages
    tags = b"OpusTags" + vorbis_comments(b"enc", ["title=Opus Title", "artist=Opus Artist", "METADATA_BLOCK_PICTURE=" + pic])

    def pages(packet, seq0):
        lacing, data = [], packet
        n = len(packet)
        while n >= 255:
            lacing.append(255)
            n -= 255
        lacing.append(n)
        out, i, seq, off = b"", 0, seq0, 0
        while i < len(lacing):
            chunk = lacing[i:i + 255]
            size = sum(chunk)
            out += b"OggS" + b"\0" * 22 + bytes([len(chunk)]) + bytes(chunk) + data[off:off + size]
            off += size
            i += 255
            seq += 1
        return out
    return pages(head, 0) + pages(tags, 1)


RUN = r"""
const fs=require('fs');const src=fs.readFileSync(process.argv[1],'utf8');
const ctx={TextDecoder,Uint8Array,atob:s=>Buffer.from(s,'base64').toString('latin1'),String,Math,Object,Array,Promise,fetch:undefined};
ctx.globalThis=ctx;require('vm').runInNewContext(src,ctx);
const files=JSON.parse(fs.readFileSync(0,'utf8'));const out={};
for(const [k,b64] of Object.entries(files)){const m=ctx.PCAudioTags.parse(Uint8Array.from(Buffer.from(b64,'base64')));
  out[k]={title:m.title||null,artist:m.artist||null,album:m.album||null,pic:m.picture?[m.picture.mime,m.picture.data.length]:null};}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_every_format_gives_its_title_artist_album_and_cover():
    files = {"id3v23": id3v23(), "id3v24": id3v24(), "id3v22": id3v22(), "flac": flac(), "opus": ogg_opus(),
             "garbage": b"\x00\x01not audio at all" * 10, "truncated_id3": id3v23()[:40]}
    payload = json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()})
    done = subprocess.run([NODE, "-e", RUN, str(ROOT / "static/js/client/audiotags.js")], input=payload,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-1500:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["id3v23"] == {"title": "Playground", "artist": "Bea Miller", "album": "Arcane: League of Legends",
                             "pic": ["image/jpeg", len(JPEG)]}, got["id3v23"]
    assert got["id3v24"]["title"] == "Ação Noturna" and got["id3v24"]["artist"] == "Baunilha", got["id3v24"]
    assert got["id3v22"]["title"] == "Old Song" and got["id3v22"]["pic"] == ["image/jpeg", len(JPEG)], got["id3v22"]
    assert got["flac"] == {"title": "Flac Title", "artist": "Flac Artist", "album": "Flac Album", "pic": ["image/png", 8]}, got["flac"]
    assert got["opus"]["title"] == "Opus Title" and got["opus"]["artist"] == "Opus Artist" and got["opus"]["pic"] == ["image/png", 8], got["opus"]
    assert got["garbage"] == {"title": None, "artist": None, "album": None, "pic": None}
    assert got["truncated_id3"]["pic"] is None      # a cut-off tag answers what it can, and never throws
