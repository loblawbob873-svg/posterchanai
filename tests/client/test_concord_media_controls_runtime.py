import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is unavailable")
def test_video_controls_remain_native_while_images_and_video_expansion_use_lightbox():
    function = "function wireRoomMedia(p)" + SOURCE.split("function wireRoomMedia(p)", 1)[1].split(
        "async function hydrateEncryptedAttachments", 1
    )[0]
    harness = f"""
const opens=[];
const make=(tag,src)=>({{
  tagName:tag,src,currentSrc:src,title:'',dataset:{{}},onclick:null,ondblclick:null,
  closest:selector=>null
}});
const image=make('IMG','https://files.example/picture.jpg');
const video=make('VIDEO','https://files.example/movie.mp4');
global.document={{querySelectorAll:()=>[image,video]}};
{function}
wireRoomMedia({{openLightbox:(...args)=>opens.push(args)}});
if(typeof image.onclick!=='function')throw new Error('image did not receive its lightbox action');
if(video.onclick!==null)throw new Error('video Play click was replaced by a lightbox action');
if(typeof video.ondblclick!=='function')throw new Error('video has no expansion gesture');
image.onclick({{preventDefault(){{}},stopPropagation(){{}}}});
video.ondblclick({{preventDefault(){{}},stopPropagation(){{}}}});
if(opens.length!==2||opens[0][0]!==image.src||opens[1][0]!==video.src||opens[1][1]!=='video')
  throw new Error('wrong viewer routing: '+JSON.stringify(opens));
console.log('ALL OK');
"""
    run = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "ALL OK" in run.stdout
