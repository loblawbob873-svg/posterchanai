"""The fifty-odd "a sound over your picture" effects are table rows now (effects_service/_sound_on_still.py).

They were 52 hand-copied triples of functions. The rewrite was checked byte-for-byte against the old code
(every output name, summary, error message, ffmpeg call and the Meme Builder's sound list); this keeps the
parts a person meets honest for every row: the effect is reachable under its old names, it renders the FIRST
image with its own mp3 for its own length, the attachment and the summary carry the effect's name, a missing
mp3 says which one, and the Meme Builder still lists the sound.
"""
from unittest import mock

import pytest

from app.services import effects_service as es, media_service, meme_builder_service
from app.services.effects_service import audio1, audio2

ROWS = [(m, row) for m in (audio1, audio2) for row in m._SOUND_ON_STILL]


def test_the_table_is_the_whole_family():
    names = [row[0] for _, row in ROWS]
    assert len(names) >= 52 and len(set(names)) == len(names), "a sound effect is missing or listed twice"


@pytest.mark.parametrize("mod,row", ROWS, ids=[row[0] for _, row in ROWS])
def test_every_row_renders_its_own_sound_over_the_first_picture(mod, row):
    name, heading, lead, candidates, duration, missing = row
    seen = []

    def fake(image_data, source_filename, audio, duration=None):
        seen.append((image_data, source_filename, audio, duration))
        return b"V" * 2048
    assert getattr(es, f"add_{name}") is getattr(mod, f"add_{name}"), "not re-exported by the package"
    with mock.patch("os.path.exists", return_value=True), mock.patch.object(media_service, "image_audio_to_video", fake):
        outs, summary = getattr(es, f"{name}_attachments")([("my pic.png", b"img", "image/png"),
                                                             ("notes.txt", b"t", "text/plain")])
    first = next(p for p in candidates if p)
    assert seen == [(b"img", "my pic.png", first, duration)]
    assert outs == [{"filename": f"my pic_{name}.mp4", "data": b"V" * 2048, "content_type": "video/mp4"}]
    assert summary == f"## {heading}\n\n{lead} my pic.png: 2.0 KB"
    with mock.patch("os.path.exists", return_value=False):
        outs, summary = getattr(es, f"{name}_attachments")([("a.png", b"img", "image/png")])
    assert outs == [] and summary == f"❌ a.png: {missing}"
    assert getattr(es, f"{name}_attachments")([("a.txt", b"t", "text/plain")]) == ([], "No image — attach an image first.")


def test_the_meme_builder_still_lists_every_sound():
    listed = set(meme_builder_service.sound_names())
    assert {row[0] for _, row in ROWS} <= listed
