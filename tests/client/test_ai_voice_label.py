"""A cloned-voice line stays a VOICE after a reload, so its Download files it under Voices on a phone.

The live payload carries `kind: "voice"` (command_service/gen.py); after a reload all the client has is
the persisted markdown, so app/routers/chat.py writes `!video[voice](...)` and the artifact row must read
that label: Download carries `data-kind="voice"` (→ saveBlobAs `{kind:'voice'}` → MediaSave hint →
Download/PosterChan/Voices) and Convert to MP3 is still offered, since the line has a speech track.
A song is unchanged. Runs the SHIPPED `_aiFileActions` under node.
"""
import json
import shutil
import subprocess

import pytest

from tests.client_source import module_path

NODE = shutil.which("node")


def _fn(src, opener):
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(opener)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_persisted_voice_line_downloads_as_a_voice_and_a_song_does_not():
    src = module_path("ai.js").read_bytes().decode("utf-8", "replace")
    marker = src[src.index("const _AI_LABEL_MARKER"):]
    marker = marker[:marker.index("\n")]
    js = "\n".join([
        "const enc=s=>String(s).replace(/&/g,'&amp;').replace(/\"/g,'&quot;').replace(/</g,'&lt;');",
        "const _ai={replyTo:null};",
        marker,
        _fn(src, "function _artName("),
        _fn(src, "function _aiFileActions("),
        "const u='/api/files/v/5/enc_'+'a'.repeat(64)+'.mp4';",
        "console.log(JSON.stringify({voice:_aiFileActions(u,'video','voice'), song:_aiFileActions(u,'video','song')}));",
    ])
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    voice_dl = got["voice"][got["voice"].index("ai-dlfile"):]
    voice_dl = voice_dl[:voice_dl.index(">")]
    assert 'data-kind="voice"' in voice_dl, voice_dl
    assert 'data-name=""' in voice_dl, "the marker label `voice` became the file's name"
    assert "ai-mp3file" in got["voice"], "a voice line lost Convert to MP3"
    song_dl = got["song"][got["song"].index("ai-dlfile"):]
    assert 'data-kind="voice"' not in song_dl[:song_dl.index(">")]
    assert "ai-mp3file" in got["song"]


def test_the_server_marks_voice_lines_and_persists_the_label():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    gen = (root / "app/services/command_service/gen.py").read_text()
    voice = gen[gen.index("wav, where = await voice_factory.generate_voice("):]
    voice = voice[:voice.index("finally:")]
    assert voice.count('"kind": "voice"') == 2, "both voice deliveries (MP4 and raw wav) must say voice"
    chat = (root / "app/routers/chat.py").read_text()
    assert 'result.get("kind") == "voice"' in chat and '_mlabel = "voice"' in chat
