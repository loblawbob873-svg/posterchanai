"""Pose-guided images ride the ordinary image request -- and an ordinary request is unchanged.

Text alone could not hold a pose (four different dance poses asked for in words came back as the same
standing pose four times), so `/api/generate-image` takes an optional OpenPose skeleton + seed, applied
through ControlNet (app/services/image_pose.py). The load balancer is the thing this repo keeps breaking,
so these run the SHIPPED factory and generator with fakes for the GPU and the peers:

* a request with no pose takes exactly the old path -- nothing extra reaches the backend;
* a pose request never goes to a Nostr provider (that protocol has no pose image);
* a node that answers a pose request WITHOUT confirming it applied the pose is a failure, so the
  balancer moves on rather than handing back a picture of the wrong pose -- likewise a backend that
  cannot apply one;
* the generator passes the pose image and its strength to ControlNet, and a plain request passes neither.
"""
import asyncio
import base64
import io
import types

import pytest
from PIL import Image

from app.services import image_factory, image_pose, settings_store


def _png(w=64, h=96, color=(0, 0, 0)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


@pytest.fixture
def lb(monkeypatch):
    """The factory with two peers and no local GPU; records what each candidate was asked."""
    calls = []
    monkeypatch.setattr(settings_store, "all_settings", lambda: {"chat_server_urls": "10.0.0.1, 10.0.0.2",
                                                                 "vram_mode": "llm_only", "image_timeout": "5000"})
    import app.services.load_balancer as lbm
    monkeypatch.setattr(lbm, "parse_server_urls", lambda s, exclude_self=True: ["http://10.0.0.1:3051", "http://10.0.0.2:3051"])
    import app.services.nostr_dvm as dvm
    monkeypatch.setattr(dvm, "providers", lambda s: [{"pubkey": "p" * 64, "relay": "wss://x"}])

    async def run_remote(kind, payload, settings, worker_pubkey=None, relay=None, timeout=None):
        calls.append(("nostr", payload))
        return {"image": "NOSTR"}
    monkeypatch.setattr(dvm, "run_remote", run_remote)

    async def rotated(c):
        return list(c)
    monkeypatch.setattr(image_factory, "_rotated", rotated)
    return calls


class _Resp:
    def __init__(self, data):
        self.status_code = 200
        self._d = data

    def json(self):
        return self._d


def _nodes(monkeypatch, answers, calls):
    """Fake peers: answers[url] is what that node returns."""
    import httpx

    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            node = url.split("/api/")[0]
            calls.append((node, json))
            return _Resp(answers[node])
    monkeypatch.setattr(httpx, "AsyncClient", C)


def test_a_plain_request_is_unchanged(lb, monkeypatch):
    calls = lb
    _nodes(monkeypatch, {"http://10.0.0.1:3051": {"image": "A"}, "http://10.0.0.2:3051": {"image": "B"}}, calls)
    out = asyncio.run(image_factory.generate_image_with_load_balancing(None, "a cat"))
    assert out == "NOSTR"                                   # rotation unchanged: providers still first here
    payload = calls[0][1]
    assert "pose_image" not in payload and "seed" not in payload


def test_a_pose_request_skips_nostr_and_refuses_an_unposed_answer(lb, monkeypatch):
    calls = lb
    _nodes(monkeypatch, {"http://10.0.0.1:3051": {"image": "OLD-NODE-IGNORED-THE-POSE"},
                         "http://10.0.0.2:3051": {"image": "POSED", "pose": True}}, calls)
    out = asyncio.run(image_factory.generate_image_with_load_balancing(
        None, "dancing", seed=7, pose_image=_png(), pose_scale=0.9))
    assert out == "POSED", "a node that ignored the pose was taken as success"
    assert not [c for c in calls if c[0] == "nostr"], "a pose request went to a Nostr provider"
    sent = [c[1] for c in calls if c[0].startswith("http")]
    assert all(p["pose_image"] and p["seed"] == 7 and p["pose_scale"] == 0.9 for p in sent)


def test_a_backend_that_cannot_pose_declines(monkeypatch):
    class Backend:
        supports_pose = False
        async def generate_image(self, **kw):
            raise AssertionError("must not generate an unposed image for a pose request")
    monkeypatch.setattr(image_factory, "get_image_backend", lambda db: Backend())
    monkeypatch.setattr(image_factory, "prepare_vram_for_image", lambda db: None)
    out = asyncio.run(image_factory._generate_image_local(None, {}, "p", "", 64, 64, 2, 1.0, pose_image=_png()))
    assert out is None


def test_the_generator_hands_the_pose_to_controlnet(monkeypatch):
    from app.services import diffusers_service as ds
    seen = {}

    class Pipe:
        dtype = "bf16"
        def __call__(self, **kw):
            seen.update(kw)
            img = Image.effect_noise((64, 96), 60).convert("RGB")   # not blank: the generator rejects blanks
            return types.SimpleNamespace(images=[img])

    svc = ds.DiffusersService.__new__(ds.DiffusersService)
    svc.model_type, svc._device, svc._pipe = "sdxl", "cpu", Pipe()
    svc.default_width, svc.default_height, svc.default_steps, svc.default_cfg = 64, 96, 2, 5.0
    svc.default_negative, svc._last_used, svc.anime_model_path, svc.model_path = "", 0, "", "m"
    svc._model_path = "m"
    monkeypatch.setattr(svc, "_ensure_model_loaded", lambda m=None: None)
    monkeypatch.setattr(image_pose, "wrap_with_pose", lambda pipe, dtype, device, configured=None, offload=False: pipe)

    assert svc._generate_sync("dancing", width=64, height=96, pose_image=_png(10, 10), pose_scale=0.8)
    assert seen["image"].size == (64, 96) and seen["controlnet_conditioning_scale"] == 0.8
    seen.clear()
    assert svc._generate_sync("a plain picture", width=64, height=96)
    assert "image" not in seen and "controlnet_conditioning_scale" not in seen


def test_the_controlnet_is_found_on_disk_without_the_network(tmp_path, monkeypatch):
    for f in image_pose.FILES:
        (tmp_path / f).write_text("x")
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    assert image_pose.ensure_controlnet(str(tmp_path)) == str(tmp_path)


def test_a_pose_that_is_not_an_image_is_refused():
    with pytest.raises(ValueError):
        image_pose.decode_pose(base64.b64encode(b"not a png").decode(), 64, 64)
    assert image_pose.clamp_scale(9) == 2.0 and image_pose.clamp_scale("x") == 1.0
