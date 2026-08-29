"""HOW MUCH OF THIS ACCOUNT'S PICTURE HISTORY IS ACTUALLY IN THE ARCHIVE — opened, not counted.

Run it on a node: `venv-unified/bin/python scripts/sms_media_census.py [N]` (N = documents to
sample; the default is the newest 400, `99999` reads the lot from both ends).

THIS IS THE INSTRUMENT THAT ENDED A WEEK OF WRONG HUNTS. Every other measurement available —
`scripts/sms_phone_report.py`, the client's own counters, the relay's document count — was perfectly
happy with an archive in which 1,775 of 2,676 messages were flagged `mms:true` and NOT ONE carried
an `att` key. Events were published, sweeps reported success, and the fixing went into the upload.
There was never an attachment to upload.

It can do this because the node holds `User.nostr_nsec`: a `pcai:sms:` body is NIP-44 sealed to the
user's own key, and the body itself is a Blossom pointer sealed under the drive master key, which
`/client/files-index` hands back wrapped to that same key. So the whole chain opens server-side with
no browser and no phone.

Read `with_real_media` against `mms_flagged`. `attachments_refused` with a reason is a DIFFERENT and
much better state than silence — it means the phone looked and said why.
"""
import base64, collections, json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text as sqltext
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.database import SessionLocal
from app.models import User
from app.services.nostr import nip44
from app.services.nostr.bip340 import pubkey_from_seckey, sign as schnorr_sign
from app.services.nostr.nostr_service import decode_seckey
import hashlib

db = SessionLocal()
u = db.query(User).filter(User.nostr_nsec.isnot(None)).first()
sk = decode_seckey(u.nostr_nsec); pk = pubkey_from_seckey(sk).hex()

def signed(kind, content, tags):
    ev = {"pubkey": pk, "created_at": int(time.time()), "kind": kind, "tags": tags, "content": content}
    ser = json.dumps([0, pk, ev["created_at"], kind, tags, content], separators=(',', ':'), ensure_ascii=False)
    ev["id"] = hashlib.sha256(ser.encode()).hexdigest()
    ev["sig"] = schnorr_sign(bytes.fromhex(ev["id"]), sk).hex()
    return ev

body = json.dumps({"pubkey": pk, "auth": base64.b64encode(
    json.dumps(signed(27235, "files-index", [["p", pk]])).encode()).decode()}).encode()
req = urllib.request.Request("http://127.0.0.1:3051/client/files-index", body,
                             {"content-type": "application/json"})
j = json.loads(urllib.request.urlopen(req, timeout=60).read())
wrapped = (j.get("index") or {}).get("mk", "")
mk = base64.b64decode(json.loads(nip44.decrypt_self(sk, wrapped))["k"])
print("drive key:", len(mk), "bytes")

rows = db.execute(sqltext(
  "SELECT e.content FROM events e JOIN event_tags t ON t.event_id=e.id AND t.tag='d' "
  "WHERE e.kind=30078 AND e.pubkey=:pk AND t.value LIKE 'pcai:sms:%' "
  "ORDER BY e.created_at DESC"), {"pk": pk}).fetchall()

N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
aes = AESGCM(mk)
tot=mms=withatt=withsha=witherr=fetchfail=0
errs = collections.Counter()
sel = rows if N >= len(rows) else (rows[:N//2] + rows[-(N//2):])
for (content,) in sel:
    try: env = json.loads(nip44.decrypt_self(sk, content))
    except Exception: continue
    blob = env.get("blob") if isinstance(env, dict) else None
    if blob:
        try:
            raw = urllib.request.urlopen("http://127.0.0.1:3051/blossom/" + blob, timeout=30).read()
            body = json.loads(aes.decrypt(raw[:12], raw[12:], None))
        except Exception as e:
            fetchfail += 1; continue
    else:
        body = env
    tot += 1
    if body.get("mms"): mms += 1
    att = body.get("att") or []
    if att:
        withatt += 1
        if any(a.get("sha") for a in att): withsha += 1
        for a in att:
            if a.get("err"): witherr += 1; errs[a["err"][:70]] += 1
print(json.dumps({"sampled": tot, "of_docs": len(rows), "blob_fetch_failed": fetchfail,
                  "mms_flagged": mms, "with_attachment_list": withatt,
                  "with_real_media": withsha, "attachments_refused": witherr,
                  "reasons": errs.most_common(6)}, indent=2))
