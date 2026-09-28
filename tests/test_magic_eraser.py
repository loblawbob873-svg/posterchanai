"""Meme Builder ✨ Magic Eraser: brush over an object and it is filled in from its surroundings.

Run: venv-unified/bin/python -m pytest tests/test_magic_eraser.py

These run the REAL fillers on real pixels, because every way this can be wrong produces a perfectly
valid PNG that merely shows the wrong thing:

  * the hole left as the object's colour      -> "it did nothing" (a red square is still a red square)
  * the hole filled with black / transparent  -> Erase parts again, which is not what was asked for
  * pixels outside the brushed area changed   -> the rest of the photo quietly degraded
  * a cut-out's transparency destroyed        -> a background-removed layer grows a black box

The synthetic scene is a known background (a gradient, where a smooth filler must be near-exact;
stripes, where only a texture model can continue the pattern) with a saturated red square painted
over it. The mask covers the square, so the TRUE answer is the background that was there before.

The model test runs when LaMa's weights are on this machine (INPAINT_MODEL_PATH, or the assets
dirs) and SKIPS with a reason otherwise — the fallback tests always run, because the fallback is the
promise that the button is never dead.
"""
import asyncio
import base64
import contextlib
import io
import json
import os

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from PIL import Image

from app.services import inpaint_service as ip

H, W = 240, 320
SQ = (slice(90, 150), slice(130, 190))          # the "object": a 60x60 red square
RED = np.array([255, 0, 0])


def _scene(kind):
    yy, xx = np.mgrid[0:H, 0:W]
    if kind == "gradient":
        rgb = np.stack([xx * 255 // W, yy * 255 // H, np.full_like(xx, 128)], -1)
    else:  # vertical stripes, 12px period — a texture only a model can continue
        s = np.where(((xx // 6) % 2) == 0, 210, 50)
        rgb = np.stack([s, s // 2, 255 - s], -1)
    bg = np.concatenate([rgb, np.full((H, W, 1), 255)], -1).astype(np.uint8)
    img = bg.copy()
    img[SQ] = (255, 0, 0, 255)
    hole = np.zeros((H, W), bool)
    hole[SQ] = True
    return bg, img, hole


def _err(out, bg, hole):
    return float(np.abs(out[hole][:, :3].astype(int) - bg[hole][:, :3].astype(int)).mean())


def _redness(out, hole):
    """Mean distance of the filled pixels from the square's red. ~0 means the object is still there."""
    return float(np.abs(out[hole][:, :3].astype(int) - RED).mean())


def _ring(hole, r):
    """Everything within r px of the hole — the only pixels the filler may touch."""
    return ip._dilate(hole, r)


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(ip, "model_path", lambda: "")


# ---- the fallback fillers: always available ------------------------------------------------------

@pytest.mark.parametrize("method", ["opencv", "diffusion"])
def test_the_fallback_fills_the_hole_with_the_background(method, no_model):
    if method == "opencv":
        pytest.importorskip("cv2", reason="OpenCV not installed — the diffusion case covers this build")
    bg, img, hole = _scene("gradient")
    out, used = ip.inpaint(img, hole, method=method)
    assert used == method
    assert out.shape == img.shape and out.dtype == np.uint8
    # Close to the gradient that was really there…
    assert _err(out, bg, hole) < 10, f"{method}: fill is {_err(out, bg, hole):.1f} off the background"
    # …and nothing like the object that was removed.
    assert _redness(out, hole) > 100, f"{method}: the red square is still there"


@pytest.mark.parametrize("method", ["opencv", "diffusion"])
def test_nothing_outside_the_brushed_region_changes(method, no_model):
    if method == "opencv":
        pytest.importorskip("cv2")
    bg, img, hole = _scene("stripes")
    out, _ = ip.inpaint(img, hole, method=method)
    grow = max(2, int(round(max(H, W) / 256)))
    far = ~_ring(hole, grow + 1)
    assert np.array_equal(out[far], img[far]), "pixels far from the brush were altered"


def test_alpha_outside_the_mask_is_untouched_and_a_cutout_stays_cut_out(no_model):
    """A background-removed layer: transparent left third. Brushing an object in the opaque part must
    not touch a single alpha value outside the brush, and must not make the filled area transparent."""
    bg, img, hole = _scene("gradient")
    img[:, :90, 3] = 0
    img[:, 90:100, 3] = 128
    out, _ = ip.inpaint(img, hole)
    assert np.array_equal(out[..., 3][~hole], img[..., 3][~hole]), "alpha changed outside the brush"
    assert out[..., 3][hole].min() == 255, "the filled object turned see-through"


def test_the_auto_path_without_a_model_is_the_classical_filler(no_model):
    bg, img, hole = _scene("gradient")
    _, used = ip.inpaint(img, hole)
    assert used in ("opencv", "diffusion")


def test_the_pure_numpy_floor_works_with_opencv_gone(monkeypatch, no_model):
    """The nostr-only image has no OpenCV and no onnxruntime; this is what it runs."""
    monkeypatch.setattr(ip, "_fill_opencv", lambda *a: None)
    bg, img, hole = _scene("gradient")
    out, used = ip.inpaint(img, hole)
    assert used == "diffusion"
    assert _err(out, bg, hole) < 10 and _redness(out, hole) > 100


def test_an_empty_mask_is_a_sentence_not_a_crash():
    _, img, _ = _scene("gradient")
    with pytest.raises(ip.NothingToErase):
        ip.inpaint(img, np.zeros((H, W), bool))
    with pytest.raises(ip.NothingToErase):
        ip.inpaint(img, np.ones((H, W), bool))


def _png(arr, mode=None):
    b = io.BytesIO()
    Image.fromarray(arr, mode).save(b, "PNG")
    return b.getvalue()


def _brush_png(hole, size=None):
    """The mask exactly as the client sends it: white strokes on a TRANSPARENT canvas, at the
    client's own (smaller) resolution."""
    m = np.zeros(hole.shape + (4,), np.uint8)
    m[hole] = (255, 255, 255, 255)
    im = Image.fromarray(m, "RGBA")
    if size:
        im = im.resize(size, Image.BILINEAR)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_bytes_in_png_out_same_size_from_a_smaller_client_mask(no_model):
    bg, img, hole = _scene("gradient")
    png, used = ip.magic_erase(_png(img), _brush_png(hole, (W // 2, H // 2)))
    out = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert out.shape == img.shape
    assert _redness(out, hole) > 100 and _err(out, bg, hole) < 12


def test_a_white_on_black_mask_means_the_same_thing(no_model):
    bg, img, hole = _scene("gradient")
    m = np.zeros((H, W), np.uint8)
    m[hole] = 255
    png, _ = ip.magic_erase(_png(img), _png(m, "L"))
    out = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert _redness(out, hole) > 100


# ---- the model: when its weights are here ---------------------------------------------------------

def test_the_model_continues_a_texture_the_classical_filler_cannot():
    if not ip.model_path():
        pytest.skip("LaMa weights not on this machine (set INPAINT_MODEL_PATH to run this)")
    if ip.model_blocked():
        pytest.skip("model blocked on this build: " + ip.model_blocked())
    bg, img, hole = _scene("stripes")
    out, used = ip.inpaint(img, hole, method="lama")
    assert used == "lama"
    classic, _ = ip.inpaint(img, hole, method="diffusion")
    assert _redness(out, hole) > 100
    assert _err(out, bg, hole) < 30, f"model fill is {_err(out, bg, hole):.1f} off the stripes"
    assert _err(out, bg, hole) < _err(classic, bg, hole) * 0.75, \
        "the model is no better than a blur on texture — is it actually running?"
    grow = max(2, int(round(max(H, W) / 256)))
    far = ~_ring(hole, grow + 1)
    assert np.array_equal(out[far], img[far])


# ---- the model download: on demand, never on a build that cannot use it ---------------------------

def test_a_nostr_only_build_never_fetches_the_model(monkeypatch):
    started = []
    monkeypatch.setattr(ip, "model_path", lambda: "")
    monkeypatch.setattr(ip.threading, "Thread", lambda *a, **k: started.append(1) or pytest.fail("fetch"))
    monkeypatch.setenv("PC_ACCEL", "nostr")
    assert ip.ensure_model() == "unavailable"
    monkeypatch.delenv("PC_ACCEL")
    monkeypatch.setenv("POSTERCHANAI_NOSTR_ONLY", "1")
    assert ip.ensure_model() == "unavailable"
    monkeypatch.delenv("POSTERCHANAI_NOSTR_ONLY")
    monkeypatch.setenv("POSTERCHANAI_INPAINT_MODEL", "0")
    assert ip.ensure_model() == "unavailable"
    monkeypatch.delenv("POSTERCHANAI_INPAINT_MODEL")
    monkeypatch.setattr(ip, "_have_onnxruntime", lambda: False)
    assert ip.ensure_model() == "unavailable"
    assert started == []


def test_first_use_starts_one_background_download_and_does_not_wait(monkeypatch):
    started = []

    class T:
        def __init__(self, target=None, **k):
            self.target = target

        def start(self):
            started.append(self.target)

    for k in ("PC_ACCEL", "POSTERCHANAI_NOSTR_ONLY", "POSTERCHANAI_INPAINT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ip, "model_path", lambda: "")
    monkeypatch.setattr(ip, "_have_onnxruntime", lambda: True)
    monkeypatch.setattr(ip.threading, "Thread", T)
    monkeypatch.setitem(ip._dl_state, "running", False)
    monkeypatch.setitem(ip._dl_state, "failed_at", 0.0)
    assert ip.ensure_model() == "downloading"
    assert ip.ensure_model() == "downloading"        # a second request does not start a second fetch
    assert started == [ip._download]


def test_the_model_is_run_on_the_cpu_only():
    """CPU only is what keeps this out of GPUResourceLock and identical on CUDA/Arc/ROCm/no-GPU."""
    import inspect
    src = inspect.getsource(ip._session)
    assert 'providers=["CPUExecutionProvider"]' in src
    code = "\n".join(l for l in inspect.getsource(ip).splitlines() if not l.lstrip().startswith("#"))
    assert "services.locks" not in code and "GPUResourceLock(" not in code
    assert "services.locks" not in inspect.getsource(C.meme_magic_erase)


# ---- the endpoint ----------------------------------------------------------------------------------

from app.routers import client as C                                  # noqa: E402
from app.database import get_db                                      # noqa: E402
from app.services import blossom_service, instance_membership        # noqa: E402
from app.services.nostr.event import build_event                     # noqa: E402


def _auth():
    ev = build_event(os.urandom(32), 27235, "")
    return ev["pubkey"], base64.b64encode(json.dumps(ev).encode()).decode()


@pytest.fixture
def api(monkeypatch, no_model):
    bg, img, hole = _scene("gradient")
    state = {"saved": [], "slots": 0, "img": _png(img), "fetched": []}

    async def member(pk):
        return None

    async def fetch(url, own, **k):
        state["fetched"].append(url)
        return state["img"], "image/png"

    async def save(db, pk, data, mime, **k):
        state["saved"].append((data, mime))
        return {"sha256": "ab" * 32}

    @contextlib.asynccontextmanager
    async def slot():
        state["slots"] += 1
        yield

    monkeypatch.setattr(instance_membership, "require_pubkey", member)
    monkeypatch.setattr(C, "_fetch_media_guarded", fetch)
    monkeypatch.setattr(C, "_own_media_hosts", lambda db: set())
    monkeypatch.setattr(C, "_blossom_url", lambda request, db: "https://media.example/blossom")
    monkeypatch.setattr(C, "_meme_slot", slot)
    monkeypatch.setattr(C, "_effect_cooldown", {})
    monkeypatch.setattr(blossom_service, "is_enabled", lambda db: True)
    monkeypatch.setattr(blossom_service, "save_blob", save)
    monkeypatch.setattr(ip, "ensure_model", lambda: "unavailable")

    async def no_fwd(*a, **k):
        return None
    monkeypatch.setattr(C, "_meme_lb_forward", no_fwd)

    app = FastAPI()
    app.include_router(C.router)
    app.dependency_overrides[get_db] = lambda: None

    def post(body, headers=None):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as c:
                return await c.post("/client/meme/magic-erase", json=body, headers=headers or {})
        return asyncio.run(go())

    state.update(post=post, bg=bg, arr=img, hole=hole)
    return state


def _body(hole, **over):
    pk, auth = _auth()
    b = {"pubkey": pk, "auth": auth, "url": "https://media.example/blossom/x.png",
         "mask": "data:image/png;base64," + base64.b64encode(_brush_png(hole, (W // 2, H // 2))).decode()}
    b.update(over)
    return b


def test_the_endpoint_fills_the_layer_and_stores_a_png(api):
    r = api["post"](_body(api["hole"]))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["url"] == "https://media.example/blossom/" + "ab" * 32 + ".png"
    assert j["is_video"] is False and j["method"] in ("opencv", "diffusion")
    assert api["slots"] == 1, "the fill must run inside the meme render slot"
    data, mime = api["saved"][0]
    assert mime == "image/png"
    out = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))
    assert out.shape == api["arr"].shape
    assert _redness(out, api["hole"]) > 100 and _err(out, api["bg"], api["hole"]) < 12


def test_bad_auth_is_refused(api):
    pk, _ = _auth()
    _, other = _auth()
    r = api["post"](_body(api["hole"], pubkey=pk, auth=other))
    assert r.status_code == 401
    assert api["saved"] == [] and api["slots"] == 0


@pytest.mark.parametrize("mask,why", [
    ("", "nothing brushed"),
    ("!!!not-base64!!!", "not base64"),
    (base64.b64encode(b"GIF89a....").decode(), "not a PNG"),
    ("A" * (C._MAGIC_MASK_MAX_B64 + 4), "too large"),
])
def test_a_bad_mask_is_a_400_and_costs_no_cooldown(api, mask, why):
    body = _body(api["hole"], mask=mask)
    r = api["post"](body)
    assert r.status_code == 400, (why, r.status_code, r.text)
    assert api["slots"] == 0 and api["saved"] == []
    # The same user's corrected request goes straight through — a malformed one did not charge them.
    ok = api["post"](_body(api["hole"], pubkey=body["pubkey"], auth=body["auth"]))
    assert ok.status_code == 200, ok.text


def test_a_mask_with_nothing_brushed_says_so(api):
    r = api["post"](_body(np.zeros((H, W), bool)))
    assert r.status_code == 400 and "brush" in r.json()["detail"]


def test_an_image_over_the_cap_is_refused(api):
    api["img"] = b"\x89PNG" + b"\0" * (80 * 1024 * 1024 + 1)
    r = api["post"](_body(api["hole"]))
    assert r.status_code == 400 and "large" in r.json()["detail"]
    assert api["slots"] == 0


def test_an_unreadable_picture_is_a_400_not_a_500(api):
    api["img"] = b"this is not a picture"
    r = api["post"](_body(api["hole"]))
    assert r.status_code == 400


def test_one_at_a_time_per_user(api):
    body = _body(api["hole"])
    assert api["post"](body).status_code == 200
    assert api["post"](body).status_code == 429


def test_a_node_with_blossom_off_says_so(api, monkeypatch):
    monkeypatch.setattr(blossom_service, "is_enabled", lambda db: False)
    assert api["post"](_body(api["hole"])).status_code == 503


def test_a_forwarded_job_answers_with_raw_png_and_the_lb_knows_it(api, monkeypatch):
    """A peer needs the CPU, not a blob store: it answers with the PNG and the requesting node stores
    it. A subpath missing from _MEME_RAW_MEDIA_SUBPATHS would hand those bytes to the browser, which
    is waiting for {url} — and the layer would silently never change."""
    from app.utils import lb_auth
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: "s3cret")
    r = api["post"](_body(api["hole"]), headers=lb_auth.headers({"x-pcai-meme-fwd": "1"}))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-pcai-effect-method"] in ("opencv", "diffusion")
    assert api["saved"] == []
    assert "magic-erase" in C._MEME_RAW_MEDIA_SUBPATHS


def test_the_route_uses_the_fleet_membership_gate():
    import inspect
    src = inspect.getsource(C.meme_magic_erase)
    assert "_require_member_unless_fleet_forward(request, pk)" in src
    assert '_meme_lb_forward(request, "magic-erase"' in src
