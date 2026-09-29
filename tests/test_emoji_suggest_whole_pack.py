"""The AI emoji suggester goes through ALL of the instance's custom emoji.

Asked for: "we reorganized DRC emojis, the custom emojis from akkoma and copied them. We need our AI
emoji suggestor to go through all custom emojis". Two things stood between the suggester and the pack:

  * a pack's pack.json was the whole truth, so images copied in beside it (and every sub-folder —
    Akkoma packs nest) were invisible: 94 of 3,404 DRC images on poster.place, measured;
  * the suggester took ONE emoji per keyword by name, so a 3,400-emoji pack was reduced to a guess per
    word — the model never saw anything else to choose from.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from app.services import emoji_service
from app.routers import client as client_router

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _put(root, rel):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(PNG)


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {"host": "poster.place", "x-forwarded-proto": "https"}
        self.url = mock.Mock(scheme="https", netloc="poster.place")
        self.client = mock.Mock(host="127.0.0.1")

    async def json(self):
        return self._body


class WholePack(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _put(self.root, "DRC/cat_laugh.png")
        _put(self.root, "DRC/copied_in_later.png")                 # beside pack.json, not listed in it
        _put(self.root, "DRC/animals/cat_smug.png")               # a sub-folder, as Akkoma packs have
        _put(self.root, "DRC/animals/cat_cope.png")
        _put(self.root, "DRC/animals/cat_seethe.png")
        _put(self.root, "DRC/animals/cat_blessed.png")
        with open(os.path.join(self.root, "DRC", "pack.json"), "w") as fh:
            json.dump({"files": {"catlaugh": "cat_laugh.png"}}, fh)
        p = mock.patch.object(emoji_service, "emoji_dir", return_value=self.root)
        p.start(); self.addCleanup(p.stop)
        emoji_service._invalidate(); self.addCleanup(emoji_service._invalidate)

    def codes(self):
        return {e["shortcode"] for e in emoji_service.index(force=True)}

    def test_every_image_is_indexed_listed_or_not_nested_or_not(self):
        got = self.codes()
        self.assertIn("catlaugh", got, "a pack.json entry keeps its listed shortcode")
        self.assertNotIn("cat_laugh", got, "a listed file must not ALSO appear under its file name")
        for sc in ("copied_in_later", "cat_smug", "cat_cope", "cat_seethe", "cat_blessed"):
            self.assertIn(sc, got, sc + " was not indexed")

    def test_a_file_copied_in_later_is_picked_up_without_a_restart(self):
        emoji_service.index(force=True)
        time.sleep(0.01)
        _put(self.root, "DRC/animals/new_arrival.png")
        emoji_service._cache["at"] = 0.0                         # past the 5s re-stat window
        self.assertIn("new_arrival", {e["shortcode"] for e in emoji_service.index()})

    def test_the_model_chooses_from_the_whole_pool_not_one_guess_per_word(self):
        """The keyword step says "cat"; ranking alone would answer ONE cat emoji. The pool carries
        several, and the model's choice — the fourth-best "cat" match — is what comes back."""
        calls = []

        class Svc:
            async def chat_completion(self, messages, **kw):
                calls.append(messages)
                if len(calls) == 1:
                    return {"choices": [{"message": {"content": "REACTION: cat, cope, blessed, smug\nSUBJECT: cat, pet, animal, fur"}}]}
                pool = messages[-1]["content"].split("EMOJI:", 1)[1]
                assert "cat_blessed" in pool and "cat_seethe" in pool, pool
                return {"choices": [{"message": {"content": "cat_seethe, cat_blessed, nonexistent_emoji"}}]}

        with mock.patch("app.services.inference_factory.get_inference_service", return_value=Svc()), \
             mock.patch.object(client_router.tor_service, "request_onion_host", return_value=None):
            res = asyncio.run(client_router.suggest_emoji(_Req({"text": "my cat did something", "limit": 3}), db=None))
        got = [e["s"] for e in json.loads(res.body)["emojis"]]
        self.assertEqual(len(calls), 2, "the model was never asked to choose")
        self.assertEqual(got[:2], ["cat_seethe", "cat_blessed"], got)
        self.assertNotIn("nonexistent_emoji", got, "a name the model invented must never be offered")
        self.assertEqual(len(got), 3)
