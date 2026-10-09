"""Search tokens EXACTLY as the relay's Postgres makes them: `to_tsvector('simple', content)` and
`plainto_tsquery('simple', search)`.

Why a port and not `\\w+`: Postgres's default parser does not see words, it sees TOKENS — a URL is the whole
URL plus its host plus its path (so "image" inside https://x.com/image.jpg is NOT searchable), `poster.place` is
one host, `self-hosted` is the compound and each part, `v1.2.3` is a file, HTML tags and entities vanish, and
under the production locale (C.UTF8) an emoji is not a word at all. A word-splitter disagreed with Postgres on
~2/3 of real searches (scripts/posterchandb_parity.py, 2026-10-09).

So this is PostgreSQL's own state machine: the 77 state tables are GENERATED from src/backend/tsearch/
wparser_def.c (pg_parser_tables.py, by scripts/gen_pg_parser_tables.py), the driver below is TParserGet ported
line for line, and the character classes are the C library's own (`iswalpha_l` & co. in C.UTF-8 — what
Postgres calls), not Python's Unicode tables, which differ for Indic vowel signs, non-ASCII digits and `İ`.
tests/test_posterchandb_tsparser.py compares every token against a real Postgres.

Speed: outside tags/entities/comments the parser never carries state across whitespace, so text without
'<' or '&' is tokenised chunk by chunk with a memo (natural text repeats its words); anything else runs the
full machine over the whole string. Both paths are checked against each other by the tests.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import re
import unicodedata
from functools import lru_cache

from .pg_parser_tables import ACTIONS, FLAGS, STATES, STRANGE_LETTER, TOK_ALIAS

A_BINGO, A_POP, A_PUSH = FLAGS["A_BINGO"], FLAGS["A_POP"], FLAGS["A_PUSH"]
A_RERUN, A_CLEAR, A_MERGE, A_CLRALL = FLAGS["A_RERUN"], FLAGS["A_CLEAR"], FLAGS["A_MERGE"], FLAGS["A_CLRALL"]
TPS_BASE = STATES.index("TPS_Base")
TPS_URLPATHFIRST = STATES.index("TPS_InURLPathFirst")
HOST = TOK_ALIAS.index("host")
URLPATH = TOK_ALIAS.index("url_path")
# The `simple` configuration maps every token type except these to the simple dictionary (`\dF+ simple`).
UNMAPPED = frozenset(TOK_ALIAS.index(a) for a in ("blank", "tag", "protocol", "entity"))
_NONSPACE = re.compile(r"\S+")
MAXSTRLEN = (1 << 11) - 1        # ts_type.h: a lexeme of >= this many BYTES is skipped (IGNORE_LONGLEXEME)


# ---------------------------------------------------------------- the C library's character classes
class _LibC:
    def __init__(self):
        self.ok = False
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
            libc.newlocale.restype = ctypes.c_void_p
            libc.newlocale.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p]
            LC_CTYPE_MASK = 1 << 0          # glibc: 1 << __LC_CTYPE (0)
            loc = libc.newlocale(LC_CTYPE_MASK, b"C.UTF-8", None)
            if not loc:
                return
            self.loc = ctypes.c_void_p(loc)
            for name in ("iswalpha_l", "iswalnum_l", "iswdigit_l", "iswspace_l", "iswxdigit_l"):
                f = getattr(libc, name)
                f.restype = ctypes.c_int
                f.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
                setattr(self, name, f)
            libc.towlower_l.restype = ctypes.c_uint32
            libc.towlower_l.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            self.towlower_l = libc.towlower_l
            self.ok = bool(self.iswalpha_l(ord("é"), self.loc))
        except (OSError, AttributeError):
            self.ok = False


_C = _LibC()


@lru_cache(maxsize=1 << 16)
def _classes(ch: str) -> int:
    """Bit set: 1 alpha, 2 alnum, 4 digit, 8 space, 16 xdigit, 32 special (zero display width / strange letter)."""
    o = ord(ch)
    if _C.ok:
        loc = _C.loc
        b = ((1 if _C.iswalpha_l(o, loc) else 0) | (2 if _C.iswalnum_l(o, loc) else 0)
             | (4 if _C.iswdigit_l(o, loc) else 0) | (8 if _C.iswspace_l(o, loc) else 0)
             | (16 if _C.iswxdigit_l(o, loc) else 0))
    else:      # no glibc C.UTF-8 (musl): Unicode's view, which matches it for everything but rare marks
        cat = unicodedata.category(ch)
        alpha = cat[0] == "L" or (cat == "Nd" and not "0" <= ch <= "9")
        digit = "0" <= ch <= "9"
        b = ((1 if alpha else 0) | (2 if alpha or digit else 0) | (4 if digit else 0)
             | (8 if ch.isspace() and ch not in "   " else 0)
             | (16 if ch in "0123456789abcdefABCDEF" else 0))
    # pg_dsplen() == 0: NUL or a non-spacing/enclosing mark or format char (ucs_wcwidth's combining table)
    if o == 0 or (o > 0x7f and unicodedata.category(ch) in ("Mn", "Me", "Cf")) or o in STRANGE_LETTER:
        b |= 32
    return b


@lru_cache(maxsize=1 << 16)
def _lower(ch: str) -> str:
    if _C.ok:
        return chr(_C.towlower_l(ord(ch), _C.loc))
    lo = ch.lower()
    return lo if len(lo) == 1 else lo[0]


def lower(s: str) -> str:
    """str_tolower() under C.UTF8: one code point to one code point (towlower), never Python's
    multi-character mappings ('İ'.lower() is two characters; towlower gives 'i')."""
    return s.lower() if s.isascii() else "".join(_lower(c) for c in s)


# ---------------------------------------------------------------- the parser (TParserGet)
class _Pos:
    __slots__ = ("pos", "charlen", "lentoken", "state", "prev", "pushed")

    def __init__(self, prev=None):
        if prev is not None:
            self.pos, self.charlen, self.lentoken, self.state = prev.pos, prev.charlen, prev.lentoken, prev.state
        else:
            self.pos = self.charlen = self.lentoken = 0
            self.state = TPS_BASE
        self.prev = prev
        self.pushed = None        # (state, index) of the action that pushed — resume after it on POP


class _Parser:
    __slots__ = ("s", "n", "st", "ignore", "wanthost", "c", "token_start", "lentoken", "type")

    def __init__(self, s: str, start: int = 0):
        self.s = s[start:] if start else s
        self.n = len(self.s)
        self.st = _Pos()
        self.ignore = False
        self.wanthost = False
        self.c = ""

    # character tests (p_is* in wparser_def.c); `charlen` is the UTF-8 length of the current character
    def _cls(self, bit):
        st = self.st
        return st.pos < self.n and bool(_classes(self.s[st.pos]) & bit)

    def test(self, name):
        st = self.st
        if name == "p_isEOF":
            return st.pos == self.n or st.charlen == 0
        if name == "p_iseqC":
            return st.charlen == 1 and self.s[st.pos] == self.c
        if name == "p_isdigit":
            return self._cls(4)
        if name == "p_isasclet":
            return st.charlen == 1 and self._cls(1)
        if name == "p_isalpha":
            return self._cls(1)
        if name == "p_isalnum":
            return self._cls(2)
        if name == "p_isnotalnum":
            return not self._cls(2)
        if name == "p_isspecial":
            return self._cls(32)
        if name == "p_isspace":
            return self._cls(8)
        if name == "p_isxdigit":
            return self._cls(16)
        if name == "p_isurlchar":
            if st.charlen != 1:
                return False
            ch = self.s[st.pos]
            return " " < ch < "\x7f" and ch not in '"<>\\^`{|}'
        if name == "p_isignore":
            return self.ignore
        if name == "p_isstophost":
            if self.wanthost:
                self.wanthost = False
                return True
            return False
        if name == "p_ishost":
            return self._sub(True, None, HOST)
        if name == "p_isURLPath":
            return self._sub(False, TPS_URLPATHFIRST, URLPATH)
        raise ValueError(name)

    def _sub(self, wanthost, push_state, want_type):
        sub = _Parser(self.s, self.st.pos)
        sub.wanthost = wanthost
        if push_state is not None:
            sub.st = _Pos(sub.st)
            sub.st.state = push_state
        if sub.get() and sub.type == want_type:
            st = self.st
            st.pos += sub.lentoken
            st.lentoken += sub.lentoken
            st.charlen = sub.st.charlen
            return True
        return False

    def special(self, name):
        st = self.st
        if name == "SpecialTags":
            tok = self.s[self.token_start:self.token_start + st.lentoken].lower()
            n = st.lentoken
            if n == 8 and tok.startswith("</script"):
                self.ignore = False
            elif n == 7 and tok.startswith("</style"):
                self.ignore = False
            elif n == 7 and tok.startswith("<script"):
                self.ignore = True
            elif n == 6 and tok.startswith("<style"):
                self.ignore = True
        elif name == "SpecialFURL":
            self.wanthost = True
            st.pos -= st.lentoken
        elif name == "SpecialHyphen":
            st.pos -= st.lentoken
        elif name == "SpecialVerVersion":
            st.pos -= st.lentoken
            st.lentoken = 0
        else:
            raise ValueError(name)

    def get(self) -> bool:
        st = self.st
        if st.pos >= self.n:
            return False
        self.token_start = st.pos
        st.pushed = None
        item = None
        while self.st.pos <= self.n:
            st = self.st
            st.charlen = 0 if st.pos == self.n else len(self.s[st.pos].encode("utf-8"))
            if st.pushed is not None:
                state, idx = st.pushed
                idx += 1
                st.pushed = None
            else:
                state, idx = st.state, 0
            table = ACTIONS[state]
            while True:
                item = table[idx]
                if item[0] is None:
                    break
                self.c = item[1]
                if self.test(item[0]):
                    break
                idx += 1
            _cls, _c, flags, tostate, typ, special = item
            if special:
                self.special(special)
            st = self.st
            if flags & A_BINGO:
                self.lentoken = st.lentoken
                st.lentoken = 0
                self.type = typ
            if flags & A_POP:
                self.st = st.prev
            elif flags & A_PUSH:
                st.pushed = (state, idx)
                self.st = _Pos(st)
            elif flags & A_CLEAR:
                st.prev = st.prev.prev
            elif flags & A_CLRALL:
                st.prev = None
            elif flags & A_MERGE:
                prev = st.prev
                prev.pos, prev.charlen, prev.lentoken = st.pos, st.charlen, st.lentoken
                self.st = prev
            st = self.st
            if tostate != -1:
                st.state = tostate
            if (flags & A_BINGO) or (st.pos >= self.n and not (flags & A_RERUN)):
                break
            if flags & (A_RERUN | A_POP):
                continue
            if st.charlen:
                st.pos += 1
                st.lentoken += 1
        return bool(item and (item[2] & A_BINGO))

    def tokens(self):
        while self.get():
            yield self.type, self.s[self.token_start:self.token_start + self.lentoken]


def tokens(text: str):
    """(alias, token) pairs exactly as ts_debug('simple', text) lists them (including the ignored types)."""
    for t, tok in _Parser(text).tokens():
        yield TOK_ALIAS[t], tok


def _lexemes_full(text: str) -> list:
    out = []
    for t, tok in _Parser(text).tokens():
        if t in UNMAPPED:
            continue
        if len(tok.encode("utf-8")) >= MAXSTRLEN:
            continue
        lx = lower(tok)
        if lx:
            out.append(lx)
    return out


@lru_cache(maxsize=1 << 18)
def _chunk(chunk: str) -> tuple:
    return tuple(_lexemes_full(chunk))


def lexemes(text: str) -> list:
    """The lexemes to_tsvector('simple', text) would store, in order (duplicates kept)."""
    if not text:
        return []
    if "<" in text or "&" in text:
        return _lexemes_full(text)
    out = []
    for m in _NONSPACE.finditer(text):
        chunk = m.group()
        if m.start():
            # The full parser reaches this chunk in its BLANK state (TPS_InSpace), which keeps swallowing
            # characters that are not alphanumeric until one of < - + & / — so a chunk after whitespace
            # loses its leading '~', '.', '#', '@'… exactly as Postgres's does ("a ~/f" → blank " ~", file "/f").
            i = 0
            while i < len(chunk) and chunk[i] not in "<-+&/" and not (_classes(chunk[i]) & 2):
                i += 1
            chunk = chunk[i:]
            if not chunk:
                continue
        if chunk.isascii() and chunk.isalpha():
            if len(chunk) < MAXSTRLEN:         # a plain ASCII word: asciiword, lowercased — the common case
                out.append(chunk.lower())
        else:
            out.extend(_chunk(chunk))
    return out


def search_set(text: str) -> set:
    """What a search may match on: the distinct lexemes of to_tsvector('simple', text)."""
    return set(lexemes(text))


def query_lexemes(search: str) -> set:
    """plainto_tsquery('simple', search): every lexeme is required (AND). Empty = matches nothing."""
    return set(lexemes(search))
