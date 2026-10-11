"""musicgeni: the song is made from what YOU typed, and the reply quotes you.

2026-10-10, a member asked for "video game style battle music ... drums in a 5 beat pattern, an orchestra
sounding trumpet with an electric guitar" and was answered "Generated song for: electronic orchestral hybrid,
high-energy battle rap, 140 BPM ..." -- read as somebody else's song. It was theirs: the lyric-writing LLM
also wrote a STYLE line, that line REPLACED their description as the music model's caption, and the reply
quoted the replacement. The LLM now writes the lyrics only; the caption and the reply are the user's words.
"""
import asyncio
import types

from app.services.command_service import gen
from app.services.command_service.gen import _GenMixin

ASKED = ("A video game style battle music, with a dreamy reverb synth that sounds epic with drums in a "
         "5 beat pattern. An orchestra sounding trumpet with an electric guitar.")


def _run(monkeypatch, arg, llm_reply):
    seen = {}

    async def chat(messages):
        seen["llm_in"] = messages[-1]["content"]
        return llm_reply

    async def make(db, prompt, lyrics):
        seen["caption"], seen["lyrics"] = prompt, lyrics
        return b"ID3fake", "mp3"
    monkeypatch.setattr(gen.music_factory, "generate_music_for_user", make)
    from app.services import media_service
    monkeypatch.setattr(media_service, "make_music_video", lambda *a, **k: b"mp4")
    cs = object.__new__(_GenMixin)
    cs.db = None
    cs.chat_service = types.SimpleNamespace(chat=chat, num_predict=512)
    res = asyncio.run(cs._musicgeni_command(arg))
    return res, seen


def test_the_caption_and_the_reply_are_what_the_user_typed(monkeypatch):
    res, seen = _run(monkeypatch, ASKED,
                     "STYLE: electronic orchestral hybrid, high-energy battle rap, 140 BPM\nLYRICS:\n[verse]\nwe fight")
    assert seen["caption"] == ASKED, "the music model was given the LLM's rewrite, not the request"
    assert "we fight" in seen["lyrics"] and "STYLE" not in seen["lyrics"]
    assert ASKED in res["content"] and "battle rap" not in res["content"], res["content"]
    assert res.get("prompt") == ASKED


def test_explicit_lyrics_and_instrumental_are_untouched(monkeypatch):
    res, seen = _run(monkeypatch, "lofi beat | la la la", "unused")
    assert seen["caption"] == "lofi beat" and seen["lyrics"] == "la la la" and "llm_in" not in seen
    res, seen = _run(monkeypatch, "calm piano instrumental", "unused")
    assert seen["caption"] == "calm piano instrumental" and seen["lyrics"] == "" and "llm_in" not in seen
