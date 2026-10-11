"""The built-in terminal can draw btop/htop: braille graphs, rounded box corners, block bars.

2026-10-10: no font in the terminal's stack had braille (U+2800-28FF) or the rounded corners, so every btop graph
rendered as rows of empty "missing glyph" squares ("btop looks like ass on the servers" -- the servers were fine).
The fix ships static/fonts/term-symbols.woff2 (a DejaVu Sans Mono subset with the 256 braille patterns drawn on its
cell) and puts it FIRST in the terminal's font stack, scoped by unicode-range. This reads the shipped font's cmap
itself (WOFF2: brotli stream; cmap is never a transformed table), so it fails if the file loses a glyph.
"""
import os
import re
import struct

import brotli

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FONT = os.path.join(ROOT, "static", "fonts", "term-symbols.woff2")
_TAGS = ["cmap", "head", "hhea", "hmtx", "maxp", "name", "OS/2", "post", "cvt ", "fpgm", "glyf", "loca", "prep",
         "CFF ", "VORG", "EBDT", "EBLC", "gasp", "hdmx", "kern", "LTSH", "PCLT", "VDMX", "vhea", "vmtx", "BASE",
         "GDEF", "GPOS", "GSUB", "EBSC", "JSTF", "MATH", "CBDT", "CBLC", "COLR", "CPAL", "SVG ", "sbix", "acnt",
         "avar", "bdat", "bloc", "bsln", "cvar", "fdsc", "feat", "fmtx", "fvar", "gvar", "hsty", "just", "lcar",
         "mort", "morx", "opbd", "prop", "trak", "Zapf", "Silf", "Glat", "Gloc", "Feat", "Sill"]


def _b128(buf, i):
    v = 0
    for _ in range(5):
        b = buf[i]; i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i
    raise ValueError("bad UIntBase128")


def _cmap_codes() -> set:
    data = open(FONT, "rb").read()
    assert data[:4] == b"wOF2"
    num_tables = struct.unpack(">H", data[12:14])[0]
    i = 48
    tables = []
    for _ in range(num_tables):
        flags = data[i]; i += 1
        tag = _TAGS[flags & 0x3F] if (flags & 0x3F) != 63 else data[i:i + 4].decode("latin-1")
        if (flags & 0x3F) == 63:
            i += 4
        orig, i = _b128(data, i)
        version = (flags >> 6) & 3
        transformed = (version != 0) if tag not in ("glyf", "loca") else (version != 3)
        length = orig
        if transformed:
            length, i = _b128(data, i)
        tables.append((tag, length))
    compressed = struct.unpack(">I", data[20:24])[0]          # totalCompressedSize: the stream, no padding
    stream = brotli.decompress(data[i:i + compressed])
    off = 0
    for tag, length in tables:
        if tag == "cmap":
            cmap = stream[off:off + length]
            break
        off += length
    codes = set()
    n = struct.unpack(">H", cmap[2:4])[0]
    for k in range(n):
        _pid, _eid, sub = struct.unpack(">HHI", cmap[4 + 8 * k:12 + 8 * k])
        fmt = struct.unpack(">H", cmap[sub:sub + 2])[0]
        if fmt == 4:
            segx2 = struct.unpack(">H", cmap[sub + 6:sub + 8])[0]
            ends = struct.unpack(">%dH" % (segx2 // 2), cmap[sub + 14:sub + 14 + segx2])
            starts = struct.unpack(">%dH" % (segx2 // 2), cmap[sub + 16 + segx2:sub + 16 + 2 * segx2])
            for s, e in zip(starts, ends):
                if s != 0xFFFF:
                    codes.update(range(s, e + 1))
        elif fmt == 12:
            ng = struct.unpack(">I", cmap[sub + 12:sub + 16])[0]
            for g in range(ng):
                s, e, _ = struct.unpack(">III", cmap[sub + 16 + 12 * g:sub + 28 + 12 * g])
                codes.update(range(s, e + 1))
    return codes


def test_the_font_has_every_symbol_btop_draws():
    codes = _cmap_codes()
    missing = [hex(c) for c in list(range(0x2800, 0x2900)) + list(range(0x2500, 0x25A0)) + [0x25A0, 0x2191, 0x2193]
               if c not in codes]
    assert not missing, "term-symbols.woff2 lacks %d glyph(s): %s" % (len(missing), missing[:12])


def test_the_terminal_uses_it_first_and_only_for_symbols():
    css = open(os.path.join(ROOT, "static", "css", "client.css"), encoding="utf-8").read()
    face = re.search(r"@font-face\{font-family:'PC Term Symbols';[^}]*\}", css)
    assert face, "the symbol font is not declared"
    assert "/static/fonts/term-symbols.woff2" in face.group(0)
    assert "U+2800-28FF" in face.group(0) and "U+2500-25FF" in face.group(0), "not scoped to the symbol ranges"
    js = open(os.path.join(ROOT, "static", "js", "client", "term.js"), encoding="utf-8").read()
    stack = re.search(r"const TERM_FONT = '([^']*)'", js).group(1)
    assert stack.startswith('"PC Term Symbols"'), "the symbol font is not first in the terminal's stack: " + stack
