"""Meme Builder 🔄 Face swap: swap two faces in a picture, or put a face from another picture on it.

Asked for: "Meme Builder -> If you can add a face swapping feature that would be cool". These run the
REAL engine on a REAL group photo (InsightFace's own t1.jpg sample, six faces, shipped inside the
insightface package this feature depends on), because every way a swap goes wrong is still a valid PNG:

  * nothing changed                         -> "it did nothing"
  * the wrong place changed                 -> somebody's shoulder repainted
  * the face is not the other person's      -> checked with a face-RECOGNITION model, not pixels
  * the face is washed out                   -> the first version's seamless clone did exactly this
  * a cut-out loses its transparency        -> a background-removed layer grows a box
"""
import asyncio
import base64
import contextlib
import io
import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

insightface = pytest.importorskip("insightface", reason="insightface not installed — face swap needs it")
cv2 = pytest.importorskip("cv2")

from app.services import faceswap_service as fs  # noqa: E402

T1 = Path(insightface.__file__).parent / "data" / "images" / "t1.jpg"
pytestmark = pytest.mark.skipif(not T1.exists(), reason="insightface sample image missing")


@pytest.fixture(scope="module")
def photo():
    return T1.read_bytes()


@pytest.fixture(scope="module")
def faces(photo):
    img, _ = fs._decode(photo)
    return img, fs._faces(img)


def _arr(png):
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int32)


def _box(face, pad=0.35):
    x1, y1, x2, y2 = face["box"]
    w, h = x2 - x1, y2 - y1
    return int(x1 - w * pad), int(y1 - h * pad), int(x2 + w * pad), int(y2 + h * pad)


def _inside(xs, ys, boxes):
    ok = np.zeros(len(xs), bool)
    for x1, y1, x2, y2 in boxes:
        ok |= (xs >= x1) & (xs <= x2) & (ys >= y1) & (ys <= y2)
    return ok


def test_detect_finds_every_face_left_to_right_in_normalised_coordinates(photo):
    d = fs.detect(photo)
    assert (d["width"], d["height"]) == (1280, 886)
    assert len(d["faces"]) == 6
    xs = [f["x"] for f in d["faces"]]
    assert xs == sorted(xs), "numbered left to right, the order the picker shows"
    for f in d["faces"]:
        assert 0 <= f["x"] < 1 and 0 <= f["y"] < 1 and 0 < f["w"] < 0.2 and 0 < f["h"] < 0.3, f


def test_swapping_two_faces_changes_those_two_faces_and_nothing_else(photo, faces):
    img, F = faces
    before = _arr(photo)
    after = _arr(fs.swap(photo, 0, 1))
    assert after.shape == before.shape
    changed = np.abs(after - before).sum(axis=2) > 24
    ys, xs = np.nonzero(changed)
    assert changed.sum() > 2000, "the faces did not change"
    assert _inside(xs, ys, [_box(F[0]), _box(F[1])]).all(), "pixels outside the two faces changed"
    for other in F[2:]:                                         # the other four faces are untouched
        x1, y1, x2, y2 = _box(other, 0)
        assert not changed[y1:y2, x1:x2].any()


def test_the_swapped_face_is_the_other_persons_face(photo):
    """IDENTITY, measured by a face-recognition model (ArcFace, w600k_r50 -- in the same buffalo_l pack
    the swap already uses): after the swap, face 1 is recognised as closer to person 2 than to person 1.
    Pixel correlation cannot say this across two differently posed heads; a recogniser can."""
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition"], providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))

    def embeddings(data):
        img, _ = fs._decode(data)
        found = sorted(app.get(img), key=lambda f: (f.bbox[0] + f.bbox[2]) / 2)
        return [f.normed_embedding for f in found]

    before, after = embeddings(photo), embeddings(fs.swap(photo, 0, 1))
    assert len(before) == len(after) == 6
    sim = lambda a, b: float(np.dot(a, b))
    assert sim(after[0], before[1]) > sim(after[0], before[0]), "face 1 is still recognised as person 1"
    assert sim(after[1], before[0]) > sim(after[1], before[1]), "face 2 is still recognised as person 2"
    for i in range(2, 6):                                   # everyone else is still themselves
        assert sim(after[i], before[i]) > 0.95


def test_no_washout(photo, faces):
    """The colour is matched to the target's skin: each face's average brightness stays close to the
    original's (seamless cloning in the first version washed faces out to near-white).

    NOT measured here, deliberately: the glowing blob beside the nose that the rejected blur-ratio
    colour step produced. A correct colour transfer also brightens a face overall when the target is
    lit brighter than the source, and on this photo the two separate by a few luminance levels only --
    any threshold would be tuned to one picture. That fix was verified by eye on this photo."""
    img, F = faces
    before = _arr(photo)
    after = _arr(fs.paste(photo, photo, 2, None))
    lum = lambda a: 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    for i, f in enumerate(F):
        pts = f["pts"][fs._INNER]
        (x1, y1), (x2, y2) = pts.min(axis=0).astype(int), pts.max(axis=0).astype(int)
        b, a = lum(before[y1:y2, x1:x2]), lum(after[y1:y2, x1:x2])
        assert abs(a.mean() - b.mean()) < 0.2 * b.mean() + 12, ("washed out or darkened", i, a.mean(), b.mean())


def test_paste_goes_only_on_the_chosen_faces(photo, faces):
    img, F = faces
    before = _arr(photo)
    after = _arr(fs.paste(photo, photo, 2, [4]))
    changed = np.abs(after - before).sum(axis=2) > 24
    ys, xs = np.nonzero(changed)
    assert changed.sum() > 1000 and _inside(xs, ys, [_box(F[4])]).all()


def _png(arr, mode="RGB"):
    out = io.BytesIO()
    Image.fromarray(arr, mode).save(out, "PNG")
    return out.getvalue()


def test_every_refusal_is_a_sentence(photo, faces):
    img, F = faces
    x1, y1, x2, y2 = [int(v) for v in _box(F[0], 0.6)]
    one = _png(np.ascontiguousarray(img[max(0, y1):y2, max(0, x1):x2][:, :, ::-1]))
    blank = _png(np.full((200, 200, 3), 128, np.uint8))
    for call, words in ((lambda: fs.swap(one, 0, 1), "one"), (lambda: fs.swap(blank, 0, 1), "none"),
                        (lambda: fs.swap(photo, 1, 1), "two different"), (lambda: fs.swap(photo, 0, 9), "two different"),
                        (lambda: fs.paste(photo, blank, 0), "picture to take the face from"),
                        (lambda: fs.paste(blank, photo, 0), "No face"),
                        (lambda: fs.paste(photo, photo, 0, [99]), "which face"),
                        (lambda: fs.swap(b"not an image"), "could not be read")):
        with pytest.raises(ValueError) as e:
            call()
        assert words in str(e.value), (words, str(e.value))


def test_a_cut_out_stays_cut_out(photo):
    rgba = np.asarray(Image.open(io.BytesIO(photo)).convert("RGBA")).copy()
    rgba[:, :40, 3] = 0                                  # a transparent strip, as a cut-out has
    out = np.asarray(Image.open(io.BytesIO(fs.swap(_png(rgba, "RGBA"), 0, 1))))
    assert out.shape[2] == 4 and (out[..., 3] == rgba[..., 3]).all()


# ---- the endpoints ----------------------------------------------------------------------------------

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.database import get_db  # noqa: E402
from app.routers import client as C  # noqa: E402
from app.services import blossom_service, instance_membership  # noqa: E402
from app.services.nostr.event import build_event  # noqa: E402


def _auth():
    ev = build_event(os.urandom(32), 27235, "")
    return ev["pubkey"], base64.b64encode(json.dumps(ev).encode()).decode()


@pytest.fixture
def api(monkeypatch, photo):
    state = {"saved": [], "slots": 0, "fetched": [], "images": {}}

    async def member(pk):
        return None

    async def fetch(url, own, **k):
        state["fetched"].append(url)
        return state["images"].get(url, photo), "image/jpeg"

    async def save(db, pk, data, mime, **k):
        state["saved"].append((data, mime))
        return {"sha256": "cd" * 32}

    @contextlib.asynccontextmanager
    async def slot():
        state["slots"] += 1
        yield

    async def no_fwd(*a, **k):
        return None
    monkeypatch.setattr(instance_membership, "require_pubkey", member)
    monkeypatch.setattr(C, "_fetch_media_guarded", fetch)
    monkeypatch.setattr(C, "_own_media_hosts", lambda db: set())
    monkeypatch.setattr(C, "_blossom_url", lambda request, db: "https://media.example/blossom")
    monkeypatch.setattr(C, "_meme_slot", slot)
    monkeypatch.setattr(C, "_effect_cooldown", {})
    monkeypatch.setattr(C, "_meme_lb_forward", no_fwd)
    monkeypatch.setattr(blossom_service, "is_enabled", lambda db: True)
    monkeypatch.setattr(blossom_service, "save_blob", save)
    app = FastAPI()
    app.include_router(C.router)
    app.dependency_overrides[get_db] = lambda: None

    def post(path, body):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as c:
                return await c.post(path, json=body)
        return asyncio.run(go())
    state["post"] = post
    return state


def test_faces_endpoint_answers_numbered_boxes_without_a_render_slot(api):
    pk, auth = _auth()
    r = api["post"]("/client/meme/faces", {"pubkey": pk, "auth": auth, "url": "https://media.example/blossom/p.jpg"})
    assert r.status_code == 200, r.text
    assert len(r.json()["faces"]) == 6 and api["slots"] == 0


def test_faceswap_endpoint_swaps_and_stores_a_png_in_the_render_slot(api):
    pk, auth = _auth()
    r = api["post"]("/client/meme/faceswap", {"pubkey": pk, "auth": auth, "url": "https://media.example/blossom/p.jpg",
                                               "mode": "swap", "a": 0, "b": 1})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["url"] == "https://media.example/blossom/" + "cd" * 32 + ".png" and j["is_video"] is False
    assert api["slots"] == 1 and api["saved"][0][1] == "image/png"


def test_paste_reads_the_source_picture_and_the_chosen_faces(api):
    pk, auth = _auth()
    r = api["post"]("/client/meme/faceswap", {"pubkey": pk, "auth": auth, "url": "https://media.example/blossom/p.jpg",
                                               "mode": "paste", "source": "https://media.example/blossom/src.jpg",
                                               "source_face": 3, "targets": [0, 5]})
    assert r.status_code == 200, r.text
    assert api["fetched"] == ["https://media.example/blossom/p.jpg", "https://media.example/blossom/src.jpg"]


@pytest.mark.parametrize("body,code,words", [
    ({"mode": "spin"}, 400, "unknown face swap mode"),
    ({"mode": "paste"}, 400, "pick the picture"),
    ({"mode": "swap", "a": 2, "b": 2}, 400, "two different"),
])
def test_bad_requests_are_sentences_and_cost_nothing(api, body, code, words):
    pk, auth = _auth()
    r = api["post"]("/client/meme/faceswap", {"pubkey": pk, "auth": auth, "url": "https://media.example/blossom/p.jpg", **body})
    assert r.status_code == code and words in r.json()["detail"], r.text
    assert api["saved"] == []


def test_bad_auth_is_refused(api):
    pk, _ = _auth()
    _, other = _auth()
    for path in ("/client/meme/faces", "/client/meme/faceswap"):
        r = api["post"](path, {"pubkey": pk, "auth": other, "url": "https://media.example/blossom/p.jpg"})
        assert r.status_code == 401
    assert api["fetched"] == [] and api["saved"] == []


# ---- a puppet, a cartoon, a drawing ------------------------------------------------------------------
# "face swap did not actually swap": pasted onto a Muppet, the face covered under half of the puppet's
# face and was turned purple. Measured on that picture: the landmarks spanned 46% of the detected face
# box (a real face spans ~78%), and the colour match took the puppet's purple as "skin".

def _fake_face(box, span_frac, score=0.62):
    """A detected face whose inner landmarks span `span_frac` of its box (106 points), detected with
    `score` confidence -- 0.62 is what the Muppet measured."""
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    rng = np.random.default_rng(3)
    pts = np.zeros((106, 2), np.float32)
    cx, cy = x1 + bw / 2, y1 + bh * 0.55
    pts[:33] = [(x1 + bw * t, y1 + bh * 0.9) for t in np.linspace(0.1, 0.9, 33)]           # jaw
    inner = rng.uniform(-0.5, 0.5, (73, 2)) * [bw * span_frac, bh * span_frac] + [cx, cy]
    inner[0] = [cx - bw * span_frac / 2, cy]; inner[1] = [cx + bw * span_frac / 2, cy]     # exact span
    inner[2] = [cx, cy - bh * span_frac / 2]; inner[3] = [cx, cy + bh * span_frac / 2]
    pts[33:] = inner
    return {"box": box, "pts": pts, "score": score}


def test_every_real_face_in_the_photo_is_trusted_as_a_real_face(faces):
    """Including the two heads turned sideways, whose landmarks are as narrow as the Muppet's."""
    img, F = faces
    assert not [i for i, f in enumerate(F) if fs._stylised(f)], "a real face was treated as a puppet"
    # Both signals are needed: bunched landmarks on a CONFIDENT detection are a real face...
    assert not fs._stylised(_fake_face((100, 80, 300, 320), 0.46, score=0.88))
    # ...and an unsure detection whose landmarks fill the face is one too.
    assert not fs._stylised(_fake_face((100, 80, 300, 320), 0.75, score=0.62))
    assert fs._stylised(_fake_face((100, 80, 300, 320), 0.46, score=0.62))


def test_a_puppet_face_gets_the_swap_over_its_whole_face(photo, faces):
    """The swapped area must cover most of a stylised face's box, not the patch its bunched landmarks
    make -- the first version covered 46%x44% of it."""
    img, F = faces
    target = np.full((400, 400, 3), (160, 60, 170), np.uint8)             # a purple face-ish block
    puppet = _fake_face((100, 80, 300, 320), 0.46)
    out = fs._put(img, F[0], target, puppet)
    changed = np.abs(out.astype(int) - target.astype(int)).sum(axis=2) > 30
    ys, xs = np.nonzero(changed)
    assert xs.max() - xs.min() > 0.6 * 200 and ys.max() - ys.min() > 0.55 * 240, \
        ("the swap covered only a patch of the puppet's face", xs.min(), xs.max(), ys.min(), ys.max())


def test_a_person_on_a_purple_puppet_is_not_turned_purple(faces):
    """Brightness follows the target; colour follows it only where the target is skin."""
    img, F = faces
    target = np.full((400, 400, 3), (170, 60, 150), np.uint8)             # BGR purple
    puppet = _fake_face((100, 80, 300, 320), 0.46)
    out = fs._put(img, F[0], target, puppet)
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(float)
    tlab = cv2.cvtColor(target, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(float)
    changed = np.abs(out.astype(int) - target.astype(int)).sum(axis=2).reshape(-1) > 30
    face_b, puppet_b = lab[changed, 2].mean(), tlab[0, 2]
    assert face_b > puppet_b + 12, ("the pasted face took the puppet's purple", face_b, puppet_b)
    assert fs._skin_like(cv2.cvtColor(img[int(F[0]['box'][1]):int(F[0]['box'][3]), int(F[0]['box'][0]):int(F[0]['box'][2])],
                                      cv2.COLOR_BGR2LAB).reshape(-1, 3).mean(0))
    assert not fs._skin_like(tlab[0])
