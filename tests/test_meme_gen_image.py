"""The Meme Builder's "🎨 Generate one with AI" endpoint (/client/meme/generate-image).

Run: venv-unified/bin/python -m unittest tests.test_meme_gen_image

This is the one place in the client where an unauthenticated-by-account caller can start a GPU
generation: a self-proof is any keypair, and a generation holds this node's GPUResourceLock for its
whole run (stalling chat, image, music and video with it). So the gate is the point of the test —
`geni` is deliberately kept OUT of the /meme/apply-effect allowlist for exactly this reason
(tests/test_meme_layer_tools.py), and this endpoint must not be the softer way in.

The stored EXTENSION is the other thing pinned here: the URL's suffix is what the browser preview
and ffmpeg both read the type from, so labelling a JPEG `.png` (or the reverse, when compression is
skipped because it made the file bigger) produces a layer that renders wrong rather than an error.
"""
import asyncio
import base64
import unittest
from unittest import mock

from fastapi import HTTPException

from app.routers import client as client_router


PK = "a" * 64


class _Req:
    """Just enough of MemeGenImageReq for the endpoint (it only reads attributes)."""

    def __init__(self, prompt="a neon city street in the rain", pubkey=PK, auth="x"):
        self.pubkey, self.auth, self.prompt = pubkey, auth, prompt


def _call(*, prompt="a neon city street in the rain", pubkey=PK, image=b"\x89PNG-original-bytes",
          compressed=b"tiny", auth_ok=True, blossom=True):
    """Drive the endpoint with every heavy dependency stubbed. Returns the JSONResponse."""
    saved = {}

    async def _save_blob(db, pk, data, mime, **kw):
        saved.update(pubkey=pk, data=data, mime=mime)
        return {"sha256": "f" * 64}

    async def _gen(db=None, user=None, prompt=""):
        saved["prompt"] = prompt
        return base64.b64encode(image).decode() if image else None

    with mock.patch.object(client_router, "_verify_self_auth", lambda a, p: auth_ok), \
         mock.patch.object(client_router, "_blossom_url", lambda req, db: "https://media.example/blossom"), \
         mock.patch("app.services.blossom_service.is_enabled", lambda db: blossom), \
         mock.patch("app.services.blossom_service.save_blob", _save_blob), \
         mock.patch("app.services.media_service.compress_image", lambda raw: compressed), \
         mock.patch("app.services.image_factory.generate_image_for_user", _gen):
        resp = asyncio.run(client_router.meme_generate_image(_Req(prompt, pubkey), None, None))
    return resp, saved


def _body(resp):
    import json
    return json.loads(resp.body)


class TestMemeGenerateImage(unittest.TestCase):
    def setUp(self):
        client_router._genimg_cooldown.clear()
        client_router._genimg_busy.clear()

    def test_a_bad_self_proof_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            _call(auth_ok=False)
        self.assertEqual(cm.exception.status_code, 401)

    def test_an_empty_prompt_is_refused_before_the_gpu(self):
        for blank in ("", "   ", "\n"):
            with self.assertRaises(HTTPException) as cm:
                _call(prompt=blank)
            self.assertEqual(cm.exception.status_code, 400, repr(blank))
        # …and nothing was generated, so a spammed empty prompt can't cost anything.
        self.assertEqual(client_router._genimg_cooldown, {})

    def test_no_media_store_is_a_clear_503(self):
        with self.assertRaises(HTTPException) as cm:
            _call(blossom=False)
        self.assertEqual(cm.exception.status_code, 503)

    def test_second_request_inside_the_cooldown_is_429(self):
        _call()
        with self.assertRaises(HTTPException) as cm:
            _call()
        self.assertEqual(cm.exception.status_code, 429)
        # A DIFFERENT user is unaffected — the gate is per-pubkey fairness, not a global mutex.
        resp, _ = _call(pubkey="b" * 64)
        self.assertTrue(_body(resp)["ok"])

    def test_a_second_request_while_one_is_in_flight_is_429(self):
        # The half the cooldown CANNOT cover: the timestamp is stamped when a job starts, and a
        # generation routinely outlives the cooldown, so without the in-flight set a second tap
        # would queue a second GPU job behind the first one.
        started, release = asyncio.Event(), asyncio.Event()

        async def _slow_gen(db=None, user=None, prompt=""):
            started.set()
            await release.wait()
            return base64.b64encode(b"png").decode()

        async def _save_blob(db, pk, data, mime, **kw):
            return {"sha256": "f" * 64}

        async def _drive():
            with mock.patch.object(client_router, "_verify_self_auth", lambda a, p: True), \
                 mock.patch.object(client_router, "_blossom_url", lambda req, db: "https://media.example/b"), \
                 mock.patch("app.services.blossom_service.is_enabled", lambda db: True), \
                 mock.patch("app.services.blossom_service.save_blob", _save_blob), \
                 mock.patch("app.services.media_service.compress_image", lambda raw: b""), \
                 mock.patch("app.services.image_factory.generate_image_for_user", _slow_gen):
                first = asyncio.create_task(client_router.meme_generate_image(_Req(), None, None))
                await started.wait()
                with self.assertRaises(HTTPException) as cm:
                    await client_router.meme_generate_image(_Req(), None, None)
                release.set()
                await first
                return cm.exception.status_code

        self.assertEqual(asyncio.run(_drive()), 429)
        # …and the in-flight marker is cleared afterwards, so the user isn't locked out forever.
        self.assertNotIn(PK, client_router._genimg_busy)

    def test_a_smaller_compression_wins_and_is_labelled_jpg(self):
        resp, saved = _call(image=b"x" * 500, compressed=b"y" * 20)
        self.assertEqual(saved["mime"], "image/jpeg")
        self.assertTrue(_body(resp)["url"].endswith(".jpg"))

    def test_a_bigger_compression_keeps_the_original_png(self):
        # compress_image can make a flat/graphic image BIGGER; shipping that (labelled jpg) would be
        # worse than doing nothing, so the original bytes and the png extension must survive.
        raw = b"x" * 20
        resp, saved = _call(image=raw, compressed=b"y" * 500)
        self.assertEqual(saved["data"], raw)
        self.assertEqual(saved["mime"], "image/png")
        self.assertTrue(_body(resp)["url"].endswith(".png"))

    def test_no_backend_answered_is_a_503_not_a_broken_layer(self):
        with self.assertRaises(HTTPException) as cm:
            _call(image=None)
        self.assertEqual(cm.exception.status_code, 503)

    def test_the_blob_is_owned_by_the_caller(self):
        _, saved = _call(pubkey=PK)
        self.assertEqual(saved["pubkey"], PK)

    def test_the_prompt_is_length_capped(self):
        _, saved = _call(prompt="z" * 5000)
        self.assertEqual(len(saved["prompt"]), 1500)


if __name__ == "__main__":
    unittest.main()
