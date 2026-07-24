#!/usr/bin/env python3
"""Self-contained Nostr publish-and-confirm — ZERO external dependencies (stdlib only).

Baked into the per-user agent sandbox (Dockerfile.sandbox) as `/usr/local/bin/nostr-post` so an
agent NEVER has to (re)implement the two things it kept getting wrong: bech32-decoding the nsec
(an nsec is NOT hex) and speaking the WebSocket relay protocol. Pure stdlib — bech32 decode +
BIP340 Schnorr sign + a minimal RFC6455 WebSocket client — so it works with no venv, no pip, and
no `bech32`/`coincurve`/`websockets` packages.

    nostr-post --nsec nsec1... --content "hello" --relay wss://relay.poster.place
    nostr-post --nsec <64-hex> --content "hi" --self-check      # sign + verify only, no network
    nostr-decode nsec1...                                       # just print the 32-byte hex key
"""
import argparse, base64, hashlib, json, os, socket, ssl, struct, sys, time
from urllib.parse import urlparse

# ─────────────────────────── bech32 / NIP-19 (BIP173) ───────────────────────────
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk

def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

def _bech32_decode(bech):
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return (None, None)
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return (None, None)
    if not all(x in _CHARSET for x in bech[pos + 1:]):
        return (None, None)
    hrp = bech[:pos]
    data = [_CHARSET.find(x) for x in bech[pos + 1:]]
    if _polymod(_hrp_expand(hrp) + data) != 1:   # checksum
        return (None, None)
    return (hrp, data[:-6])

def _convertbits(data, frombits, tobits, pad=True):
    acc = bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret

def _b32_encode(hrp, payload):
    data = _convertbits(list(payload), 8, 5)
    combined = data + [(_polymod(_hrp_expand(hrp) + data + [0]*6) ^ 1) >> 5*(5-i) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in combined)

def decode_seckey(s):
    """nsec1... or bare 64-hex -> 32 raw bytes."""
    s = s.strip()
    if len(s) == 64:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    if s.lower().startswith("nsec1"):
        hrp, data = _bech32_decode(s)
        if hrp == "nsec" and data is not None:
            raw = _convertbits(data, 5, 8, False)
            if raw and len(raw) == 32:
                return bytes(raw)
    raise ValueError("could not decode nsec (not a valid nsec1... or 64-char hex secret key)")

# ─────────────────────────── secp256k1 + BIP340 Schnorr ───────────────────────────
_p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

def _th(tag, msg):
    h = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(h + h + msg).digest()

def _add(P, Q):
    if P is None: return Q
    if Q is None: return P
    if P[0] == Q[0] and P[1] != Q[1]: return None
    if P == Q:
        lam = (3 * P[0] * P[0] * pow(2 * P[1], _p - 2, _p)) % _p
    else:
        lam = ((Q[1] - P[1]) * pow(Q[0] - P[0], _p - 2, _p)) % _p
    x3 = (lam * lam - P[0] - Q[0]) % _p
    return (x3, (lam * (P[0] - x3) - P[1]) % _p)

def _mul(P, k):
    R = None
    while k:
        if k & 1: R = _add(R, P)
        P = _add(P, P); k >>= 1
    return R

def _i2b(x): return x.to_bytes(32, "big")

def _lift_x(x):
    if x >= _p: return None
    y_sq = (pow(x, 3, _p) + 7) % _p
    y = pow(y_sq, (_p + 1) // 4, _p)
    if pow(y, 2, _p) != y_sq: return None
    return (x, y if y % 2 == 0 else _p - y)

def pubkey_xonly(seckey):
    d = int.from_bytes(seckey, "big")
    if not (1 <= d <= _n - 1): raise ValueError("secret key out of range")
    return _i2b(_mul(_G, d)[0])

def schnorr_verify(msg, pub, sig):
    if len(pub) != 32 or len(sig) != 64 or len(msg) != 32: return False
    P = _lift_x(int.from_bytes(pub, "big"))
    if P is None: return False
    r = int.from_bytes(sig[:32], "big"); s = int.from_bytes(sig[32:], "big")
    if r >= _p or s >= _n: return False
    e = int.from_bytes(_th("BIP0340/challenge", sig[:32] + pub + msg), "big") % _n
    R = _add(_mul(_G, s), _mul(P, _n - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r

def schnorr_sign(msg, seckey, aux=None):
    if aux is None: aux = os.urandom(32)
    d0 = int.from_bytes(seckey, "big")
    if not (1 <= d0 <= _n - 1): raise ValueError("secret key out of range")
    P = _mul(_G, d0)
    d = d0 if P[1] % 2 == 0 else _n - d0
    t = (d ^ int.from_bytes(_th("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(_th("BIP0340/nonce", t + _i2b(P[0]) + msg), "big") % _n
    if k0 == 0: raise RuntimeError("nonce is zero")
    R = _mul(_G, k0)
    k = k0 if R[1] % 2 == 0 else _n - k0
    e = int.from_bytes(_th("BIP0340/challenge", _i2b(R[0]) + _i2b(P[0]) + msg), "big") % _n
    sig = _i2b(R[0]) + _i2b((k + e * d) % _n)
    if not schnorr_verify(msg, _i2b(P[0]), sig): raise RuntimeError("produced invalid signature")
    return sig

# ─────────────────────────── event build ───────────────────────────
def build_event(seckey, kind, content, tags=None, created_at=None):
    tags = tags or []
    created_at = int(created_at if created_at is not None else time.time())
    pub = pubkey_xonly(seckey).hex()
    ser = json.dumps([0, pub, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False).encode()
    eid = hashlib.sha256(ser).hexdigest()
    sig = schnorr_sign(bytes.fromhex(eid), seckey)
    return {"id": eid, "pubkey": pub, "created_at": created_at, "kind": kind,
            "tags": tags, "content": content, "sig": sig.hex()}

# ─────────────────────────── minimal RFC6455 WebSocket client ───────────────────────────
class WS:
    def __init__(self, url, timeout=20):
        u = urlparse(url)
        secure = u.scheme == "wss"
        host = u.hostname
        port = u.port or (443 if secure else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(timeout)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        raw.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk: raise ConnectionError("relay closed during handshake")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError("relay refused websocket upgrade: " + resp.split(b"\r\n", 1)[0].decode("latin1"))
        self._buf = resp.split(b"\r\n\r\n", 1)[1]

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk: raise ConnectionError("relay closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask = os.urandom(4)
        ln = len(payload)
        if ln < 126: header.append(0x80 | ln)
        elif ln < 65536: header += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else: header += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        while True:
            b0, b1 = self._recv_exact(2)
            opcode = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126: ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", self._recv_exact(8))[0]
            data = self._recv_exact(ln) if ln else b""
            if opcode == 0x8: raise ConnectionError("relay sent close")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4)); continue
            if opcode == 0xA:  # pong
                continue
            return data.decode("utf-8", "replace")

    def close(self):
        try: self.sock.sendall(b"\x88\x80" + os.urandom(4))
        except Exception: pass
        try: self.sock.close()
        except Exception: pass

# ─────────────────────────── publish + confirm ───────────────────────────
def publish_and_confirm(relay, ev, timeout=20):
    ws = WS(relay, timeout=timeout)
    result = {"ok": None, "ok_message": "", "retrieved": False}
    try:
        ws.send(json.dumps(["EVENT", ev]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:            # wait for OK
            msg = json.loads(ws.recv())
            if msg[0] == "OK" and msg[1] == ev["id"]:
                result["ok"] = bool(msg[2])
                result["ok_message"] = msg[3] if len(msg) > 3 else ""
                break
        sub = "confirm-" + ev["id"][:8]
        ws.send(json.dumps(["REQ", sub, {"ids": [ev["id"]]}]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:            # wait for the event back or EOSE
            msg = json.loads(ws.recv())
            if msg[0] == "EVENT" and msg[1] == sub and msg[2].get("id") == ev["id"]:
                result["retrieved"] = True
                result["retrieved_event"] = msg[2]
                break
            if msg[0] == "EOSE" and msg[1] == sub:
                break
        ws.send(json.dumps(["CLOSE", sub]))
    finally:
        ws.close()
    return result

def _decode_main():
    """`nostr-decode <nsec|hex>` -> print the 32-byte secret key hex (nothing else)."""
    if len(sys.argv) != 2:
        print("usage: nostr-decode <nsec1...|64-hex>", file=sys.stderr)
        return 2
    print(decode_seckey(sys.argv[1]).hex())
    return 0

def main():
    if os.path.basename(sys.argv[0]).replace(".py", "") == "nostr-decode":
        return _decode_main()
    ap = argparse.ArgumentParser()
    ap.add_argument("--nsec", required=True, help="nsec1... or 64-char hex secret key")
    ap.add_argument("--content", required=True)
    ap.add_argument("--relay", default="wss://relay.poster.place")
    ap.add_argument("--kind", type=int, default=1)
    ap.add_argument("--self-check", action="store_true", help="sign + verify locally, do NOT publish")
    args = ap.parse_args()

    seckey = decode_seckey(args.nsec)
    ev = build_event(seckey, args.kind, args.content)
    assert schnorr_verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"]))
    print("pubkey (hex):", ev["pubkey"])
    print("npub        :", _b32_encode("npub", bytes.fromhex(ev["pubkey"])))
    print("event id    :", ev["id"])
    print("content     :", ev["content"])

    if args.self_check:
        print("\n[self-check] signature valid ✔  (not published)")
        return 0

    print(f"\nPublishing to {args.relay} ...")
    res = publish_and_confirm(args.relay, ev)
    print("relay OK    :", res["ok"], f"({res['ok_message']})" if res["ok_message"] else "")
    print("retrieved   :", res["retrieved"])
    if res["ok"] and res["retrieved"]:
        print("\n✅ SUCCESS")
        print("   event id:", ev["id"])
        print("   content :", ev["content"])
        return 0
    print("\n❌ FAILED — OK accepted:", res["ok"], "| retrieved:", res["retrieved"])
    return 1

if __name__ == "__main__":
    sys.exit(main())
