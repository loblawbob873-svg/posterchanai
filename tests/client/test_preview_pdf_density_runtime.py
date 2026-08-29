"""Mobile PDF Preview uses a dense backing canvas without enlarging page layout."""

import json
from pathlib import Path
import subprocess


SRC = (Path(__file__).parents[2] / "static/js/client/preview.js").read_text(encoding="utf-8")


def _render_pdf():
    start = SRC.index("async function renderPdf(")
    end = SRC.index("\n\n  var _open", start)
    return SRC[start:end]


def test_phone_density_scales_backing_pixels_but_not_css_page_size():
    script = f"""
let rendered=null,appended=null;
const root={{devicePixelRatio:3}};
const page={{getViewport:({{scale}})=>({{width:600*scale,height:800*scale}}),
  render:o=>{{rendered=o;return {{promise:Promise.resolve()}};}}}};
const pdf={{numPages:1,getPage:async()=>page}};
const loadPdfJs=async()=>({{getDocument:()=>({{promise:Promise.resolve(pdf)}})}});
const box={{clientWidth:320,innerHTML:'spinner',appendChild:c=>{{appended=c}},querySelector:()=>null}};
const host={{querySelector:()=>box}},document={{createElement:()=>({{style:{{}},setAttribute(){{}},getContext:()=>({{}})}})}};
const H=String,openElsewhere=()=>{{}};
{_render_pdf()}
(async()=>{{await renderPdf(host,{{arrayBuffer:async()=>new ArrayBuffer(1)}},'manual.pdf');
process.stdout.write(JSON.stringify({{width:appended.width,height:appended.height,
 cssWidth:appended.style.width,cssHeight:appended.style.height,transform:rendered.transform}}));}})();
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == {"width": 600, "height": 800, "cssWidth": "300px",
                   "cssHeight": "400px", "transform": [2, 0, 0, 2, 0, 0]}


def test_pdf_density_is_capped_for_mobile_memory_safety():
    body = _render_pdf()
    assert "Math.min(2, Number(root.devicePixelRatio)" in body
    assert "viewport.width * outputScale" in body
    assert "canvas.style.width" in body
