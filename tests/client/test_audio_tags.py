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


def wav(chunks, audio=b"\0\0" * 4000):
    """A WAV: fmt, the audio, then `chunks` (id, body) AFTER it -- where taggers write them."""
    def ck(cid, body):
        return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")
    body = b"WAVE" + ck(b"fmt ", struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)) + ck(b"data", audio)
    for cid, b in chunks:
        body += ck(cid, b)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def riff_info(**kw):
    out = b"INFO"
    for k, v in kw.items():
        b = v.encode() + b"\0"
        out += k.encode() + struct.pack("<I", len(b)) + b + (b"\0" if len(b) & 1 else b"")
    return out


READ_BLOB = r"""
const fs=require('fs');const src=fs.readFileSync(process.argv[1],'utf8');
const ctx={TextDecoder,Uint8Array,atob:s=>Buffer.from(s,'base64').toString('latin1'),String,Math,Object,Array,Promise,fetch:undefined};
ctx.globalThis=ctx;require('vm').runInNewContext(src,ctx);
const files=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{const out={};
for(const [k,b64] of Object.entries(files)){const bytes=Uint8Array.from(Buffer.from(b64,'base64'));
  // A Blob that COUNTS what is read: the player hands readBlob the whole decrypted track, and it must read the tags,
  // never a copy of the audio.
  const blob=new Blob([bytes]); let read=0; const sl=blob.slice.bind(blob);
  blob.slice=(a,b)=>{const s=sl(a,b);const ab=s.arrayBuffer.bind(s);s.arrayBuffer=async()=>{const r=await ab();read+=r.byteLength;return r;};return s;};
  const m=await ctx.PCAudioTags.readBlob(blob); const p=ctx.PCAudioTags.parse(bytes);
  out[k]={title:m.title||null,artist:m.artist||null,album:m.album||null,pic:m.picture?[m.picture.mime,m.picture.data.length]:null,
          read, size:bytes.length, parse:{title:p.title||null,artist:p.artist||null}};}
console.log(JSON.stringify(out));})();
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_wav_says_who_it_is_from_its_chunks_wherever_they_are():
    """The Music player reads the DECRYPTED track (MusicPlayer._readTags -> readBlob). A WAV keeps its tags in RIFF
    chunks -- an `id3 ` chunk (an ordinary ID3v2 tag inside) and/or LIST/INFO -- usually AFTER the audio, so a
    head-only read never sees them. readBlob walks the chunk headers and reads only the tag chunks."""
    big = b"\0\x01" * 3_000_000                      # 6 MB of audio between the header and the tags
    files = {"id3_chunk": wav([(b"id3 ", id3v23())]),
             "info": wav([(b"LIST", riff_info(INAM="Info Title", IART="Info Artist", IPRD="Info Album"))]),
             "both": wav([(b"LIST", riff_info(INAM="Info Title")), (b"ID3 ", id3v23())]),
             "big": wav([(b"id3 ", id3v23())], audio=big),
             "untagged": wav([])}
    payload = json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()})
    done = subprocess.run([NODE, "-e", READ_BLOB, str(ROOT / "static/js/client/audiotags.js")], input=payload,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    pic = ["image/jpeg", len(JPEG)]
    assert got["id3_chunk"]["title"] == "Playground" and got["id3_chunk"]["artist"] == "Bea Miller", got["id3_chunk"]
    assert got["id3_chunk"]["pic"] == pic and got["id3_chunk"]["parse"]["title"] == "Playground", got["id3_chunk"]
    assert (got["info"]["title"], got["info"]["artist"], got["info"]["album"], got["info"]["pic"]) == \
        ("Info Title", "Info Artist", "Info Album", None), got["info"]
    # Both: each field from whichever chunk comes first, the cover from the ID3 one.
    assert got["both"]["title"] == "Info Title" and got["both"]["artist"] == "Bea Miller" and got["both"]["pic"] == pic, got["both"]
    # 6 MB of audio, and the tags read without reading it.
    assert got["big"]["title"] == "Playground" and got["big"]["pic"] == pic, got["big"]
    assert got["big"]["read"] < 300_000, ("readBlob read the audio to find the tags", got["big"]["read"], got["big"]["size"])
    assert got["untagged"]["title"] is None and got["untagged"]["pic"] is None, got["untagged"]


# ---- where a track sits: track / disc / year / album artist (the Music app's Albums and Artists views) ----
POS = r"""
const fs=require('fs');const src=fs.readFileSync(process.argv[1],'utf8');
const ctx={TextDecoder,Uint8Array,atob:s=>Buffer.from(s,'base64').toString('latin1'),String,Math,Object,Array,Promise,fetch:undefined};
ctx.globalThis=ctx;require('vm').runInNewContext(src,ctx);
const files=JSON.parse(fs.readFileSync(0,'utf8'));const out={};
for(const [k,b64] of Object.entries(files)){let m;
  try{ m=ctx.PCAudioTags.parse(Uint8Array.from(Buffer.from(b64,'base64'))); }catch(e){ m={threw:String(e)}; }
  out[k]={title:m.title||null,track:m.track??null,trackTotal:m.trackTotal??null,disc:m.disc??null,year:m.year??null,
          albumArtist:m.albumArtist||null,threw:m.threw||null};}
console.log(JSON.stringify(out));
"""


def _id3(ver, frames):
    def frame(fid, text):
        body = b"\x03" + text.encode()
        size = syncsafe(len(body)) if ver == 4 else struct.pack(">I", len(body))
        return fid.encode() + size + b"\0\0" + body
    body = b"".join(frame(f, t) for f, t in frames) + b"\0" * 16
    return b"ID3" + bytes([ver]) + b"\x00\x00" + syncsafe(len(body)) + body


def _id3v22(frames):
    def frame(fid, text):
        body = b"\x00" + text.encode("latin1")
        return fid.encode() + struct.pack(">I", len(body))[1:] + body
    body = b"".join(frame(f, t) for f, t in frames)
    return b"ID3\x02\x00\x00" + syncsafe(len(body)) + body


def _flac_vc(items):
    vc = vorbis_comments(b"ref", items)
    blk = lambda t, last, data: bytes([t | (0x80 if last else 0)]) + struct.pack(">I", len(data))[1:] + data
    return b"fLaC" + blk(0, False, b"\0" * 34) + blk(4, True, vc)


def _ogg_vorbis(items):
    """An Ogg VORBIS file: identification packet, then the comment packet (type 3) -- one page each."""
    ident = b"\x01vorbis" + b"\0" * 23
    comment = b"\x03vorbis" + vorbis_comments(b"enc", items) + b"\x01"
    def page(pk):
        lac = [255] * (len(pk) // 255) + [len(pk) % 255]
        return b"OggS" + b"\0" * 22 + bytes([len(lac)]) + bytes(lac) + pk
    return page(ident) + page(comment)


def _run_pos(files):
    payload = json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()})
    done = subprocess.run([NODE, "-e", POS, str(ROOT / "static/js/client/audiotags.js")], input=payload,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_each_format_says_where_a_track_sits_on_its_album():
    """Albums list their songs in TRACK order and group a compilation by its ALBUM ARTIST, so the parser has to read
    TRCK/TPOS/TYER/TDRC/TPE2 (and ID3v2.2's three-letter spellings) and the Vorbis/FLAC comment names -- with "3/12"
    meaning track 3 of 12, and a full date meaning its year."""
    got = _run_pos({
        "v23": _id3(3, [("TIT2", "Song"), ("TRCK", "3/12"), ("TPOS", "2/2"), ("TYER", "1999"), ("TPE2", "Various Artists")]),
        "v24": _id3(4, [("TIT2", "Song"), ("TRCK", "07"), ("TPOS", "1"), ("TDRC", "2019-04-12"), ("TPE2", "Band")]),
        "v22": _id3v22([("TT2", "Old"), ("TRK", "4/9"), ("TPA", "1/1"), ("TYE", "1987"), ("TP2", "Old Band")]),
        "flac": _flac_vc(["TITLE=F", "TRACKNUMBER=5", "TRACKTOTAL=11", "DISCNUMBER=2", "DATE=2003-01-01",
                          "ALBUMARTIST=Flac Band"]),
        "vorbis": _ogg_vorbis(["TITLE=V", "TRACKNUMBER=9/10", "DATE=2011", "ALBUM ARTIST=Ogg Band"]),
    })
    assert got["v23"] == {"title": "Song", "track": 3, "trackTotal": 12, "disc": 2, "year": "1999",
                          "albumArtist": "Various Artists", "threw": None}, got["v23"]
    assert (got["v24"]["track"], got["v24"]["disc"], got["v24"]["year"], got["v24"]["albumArtist"]) == (7, 1, "2019", "Band"), got["v24"]
    assert (got["v22"]["track"], got["v22"]["trackTotal"], got["v22"]["year"], got["v22"]["albumArtist"]) == (4, 9, "1987", "Old Band"), got["v22"]
    assert (got["flac"]["track"], got["flac"]["trackTotal"], got["flac"]["disc"], got["flac"]["year"], got["flac"]["albumArtist"]) \
        == (5, 11, 2, "2003", "Flac Band"), got["flac"]
    assert (got["vorbis"]["title"], got["vorbis"]["track"], got["vorbis"]["trackTotal"], got["vorbis"]["year"], got["vorbis"]["albumArtist"]) \
        == ("V", 9, 10, "2011", "Ogg Band"), got["vorbis"]


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_malformed_position_is_dropped_never_thrown():
    """Taggers write anything: "A", "3 of 12", "-1", "1/", a year of "unknown". None of it may throw or invent a
    number -- the field is simply absent, and the title around it still reads."""
    got = _run_pos({
        "letters": _id3(3, [("TIT2", "Keep me"), ("TRCK", "A"), ("TPOS", "x/y"), ("TYER", "unknown")]),
        "prose": _id3(4, [("TIT2", "Keep me"), ("TRCK", "3 of 12"), ("TDRC", "19")]),
        "negative": _flac_vc(["TITLE=Keep me", "TRACKNUMBER=-1", "DISCNUMBER=", "DATE=0000"]),
        "zero": _id3(3, [("TIT2", "Keep me"), ("TRCK", "0"), ("TPOS", "0/2")]),
    })
    for k, v in got.items():
        assert v["threw"] is None and v["title"] == "Keep me", (k, v)
        assert v["track"] is None and v["disc"] is None and v["year"] is None, (k, v)
