#!/usr/bin/env python3
"""Measure what PosterChanDB would hold for THIS relay: compact record size vs JSON vs Postgres, and the
RAM of its in-memory indexes. Reads a 2% sample of the live events table (read-only). Needs zstandard,
pyroaring, psycopg2 (a scratch venv is fine). See docs/POSTERCHANDB.md."""
import re, json, struct, random, sys
import psycopg2, zstandard, pyroaring
url = next(re.search(r'(postgresql[^\s"\']+)', l).group(1) for l in open('data/secrets.env') if 'postgresql' in l)
url = url.replace('postgresql+psycopg2://', 'postgresql://')
con = psycopg2.connect(url); cur = con.cursor()
cur.execute("select count(*) from events"); total = cur.fetchone()[0]
cur.execute("select id,pubkey,created_at,kind,tags,content,sig from events tablesample system(2)")
rows = cur.fetchall(); n = len(rows)
HEX = re.compile(r'^[0-9a-f]{64}$')
def varint(x):
    out = bytearray()
    while True:
        b = x & 0x7f; x >>= 7
        out.append(b | (0x80 if x else 0))
        if not x: return bytes(out)
def tagbytes(tags):
    out = bytearray(varint(len(tags)))
    for t in tags:
        out += varint(len(t))
        for i, v in enumerate(t):
            v = str(v)
            if i > 0 and HEX.match(v): out += b'\x00' + bytes.fromhex(v)        # marker 0 + 32 raw bytes
            else:
                b = v.encode(); out += varint(len(b) + 1) + b
    return bytes(out)
contents = [r[5].encode() for r in rows]
samples = random.sample(contents, min(5000, len(contents)))
dic = zstandard.train_dictionary(65536, [s for s in samples if s] or [b'x'])
cz = zstandard.ZstdCompressor(level=6, dict_data=dic)
json_total = fixed_total = tag_total = content_raw = content_z = 0
for (eid, pk, ca, kind, tags, content, sig) in rows:
    tags = json.loads(tags) if tags else []
    ev = {"id": eid, "pubkey": pk, "created_at": ca, "kind": kind, "tags": tags, "content": content, "sig": sig}
    json_total += len(json.dumps(ev, separators=(',', ':'), ensure_ascii=False).encode())
    fixed_total += 32 + 32 + 64 + 4 + len(varint(kind))
    tag_total += len(tagbytes(tags))
    c = content.encode(); content_raw += len(c)
    if c:
        z = cz.compress(c); content_z += min(len(z), len(c)) + 1
# indexes over the sample
by_pk_kind, by_kind, by_tag = {}, {}, {}
for seq, (eid, pk, ca, kind, tags, content, sig) in enumerate(rows):
    by_pk_kind.setdefault((pk, kind), pyroaring.BitMap()).add(seq)
    by_kind.setdefault(kind, pyroaring.BitMap()).add(seq)
    for t in (json.loads(tags) if tags else []):
        if len(t) > 1 and len(str(t[0])) == 1:
            by_tag.setdefault((t[0], str(t[1])[:64]), pyroaring.BitMap()).add(seq)
bm_bytes = sum(len(b.serialize()) for d in (by_pk_kind, by_kind, by_tag) for b in d.values())
keys = len(by_pk_kind) + len(by_kind) + len(by_tag)
scale = total / n
rec = fixed_total + tag_total + content_z
print(json.dumps({
  "sample_events": n, "total_events": total,
  "json_bytes_per_event": round(json_total / n), "compact_bytes_per_event": round(rec / n),
  "parts_per_event": {"fixed": round(fixed_total / n), "tags": round(tag_total / n), "content_zstd": round(content_z / n), "content_raw": round(content_raw / n)},
  "full_json_GB": round(json_total * scale / 1e9, 2), "full_compact_GB": round(rec * scale / 1e9, 2),
  "index_keys_sample": keys,
  "note": "bitmaps over a 2% sample are sparser than over the full set; RAM below is an upper-ish estimate",
  "index_RAM_MB_est": round((bm_bytes + keys * 24) * scale / 1e6, 1),
  "id_map_RAM_MB": round(total * 24 / 1e6, 1),
  "time_array_RAM_MB": round(total * 4 / 1e6, 1),
}, indent=1))
