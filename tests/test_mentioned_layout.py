"""The mentioned effect keeps its caption and its character apart, at every image size.

Reported 2026-10-08: "Mentioned effect had a bug on smaller images, character went behind the text and the text
was too big". The girl stood at full height with the caption drawn over her, and the caption's size floors were
absolute pixels (16px font, 10px margin), so the smaller the picture the bigger the share the caption took.
Measures the real caption layout (real font, real wrapping) on frames from tiny to large.
"""
from unittest import mock

import pytest

from app.services import media_service as ms
from app.services.effects_service import audio2
from app.services.effects_service._common import _meme_font_path

SIZES = [(160, 120), (240, 240), (320, 180), (360, 640), (480, 270), (640, 480), (1280, 720), (1080, 1920), (2160, 3840)]
WORDS = ["pizza", "michigan", "the federal reserve", "supercalifragilisticexpialidocious"]


@pytest.mark.parametrize("w,h", SIZES)
@pytest.mark.parametrize("word", WORDS)
def test_the_caption_fits_its_band_above_the_character(w, h, word):
    fs, lines, line_h, y0 = ms._caption_layout(audio2.mentioned_caption(word), w, h, _meme_font_path(),
                                               "top", audio2.MENTIONED_CAPTION_BAND)
    bottom = y0 + line_h * len(lines)
    girl_top = h * (1 - audio2.MENTIONED_GIRL_HEIGHT)
    assert bottom <= h * audio2.MENTIONED_CAPTION_BAND + 1, (w, h, word, fs, lines)
    assert bottom <= girl_top, ("the caption reaches into the character", w, h, word, bottom, girl_top)
    assert fs <= max(8, h / 8), ("the caption is out of proportion to the picture", w, h, fs)


def test_the_effect_asks_for_the_band_and_the_smaller_character():
    seen = {}
    with mock.patch.object(audio2, "_first_existing", return_value="/x/cheer.mov"), \
         mock.patch.object(ms, "image_gif_overlay_video", lambda *a, **k: seen.setdefault("girl", k) and b"clip"), \
         mock.patch.object(ms, "caption_video", lambda *a, **k: seen.setdefault("cap", (a, k)) and b"out"):
        audio2.add_mentioned(b"img", "a.png", "pizza")
    assert seen["girl"]["height_frac"] == audio2.MENTIONED_GIRL_HEIGHT
    assert seen["cap"][1] == {"position": "top", "max_height_frac": audio2.MENTIONED_CAPTION_BAND}
    assert audio2.MENTIONED_CAPTION_BAND + audio2.MENTIONED_GIRL_HEIGHT <= 1.0


def test_other_effects_keep_the_caption_at_the_bottom():
    fs, lines, line_h, y0 = ms._caption_layout("WHEN THE CODE WORKS", 1280, 720, _meme_font_path())
    assert y0 + line_h * len(lines) >= 720 * 0.9


@pytest.mark.parametrize("w,h", [(648, 1024), (360, 640), (240, 240), (1080, 1920)])
@pytest.mark.parametrize("word", ["malfoid", "michigan", "the federal reserve"])
def test_no_word_is_split_across_lines_when_a_smaller_size_keeps_it_whole(w, h, word):
    """The reported render (648x1024, `malfoid`) came back as "MALFOID / MENTION / ED"."""
    text = audio2.mentioned_caption(word)
    fs, lines, _lh, _y0 = ms._caption_layout(text, w, h, _meme_font_path(), "top", audio2.MENTIONED_CAPTION_BAND)
    assert " ".join(lines).split() == text.split(), (w, h, fs, lines)
