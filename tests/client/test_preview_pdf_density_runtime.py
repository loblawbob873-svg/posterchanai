"""Mobile PDF Preview uses a dense backing canvas without enlarging page layout."""

import json
from pathlib import Path
import subprocess


SRC = (Path(__file__).parents[2] / "static/js/client/preview.js").read_text(encoding="utf-8")


def _render_pdf():
    # renderPdf starts its asynchronous renderer internally and synchronously returns a cleanup
    # callback.  Keep this harness aligned with that public contract instead of awaiting the
    # callback as though renderPdf itself were async.
    start = SRC.index("function renderPdf(")
    end = SRC.index("\n\n  var _open", start)
    return SRC[start:end]


def test_phone_density_scales_backing_pixels_but_not_css_page_size():
    script = f"""
let rendered=null,appended=null;
let renderingReady;const rendering=new Promise(r=>renderingReady=r);
const root={{devicePixelRatio:3}};
const page={{getViewport:({{scale}})=>({{width:600*scale,height:800*scale}}),
  render:o=>{{rendered=o;renderingReady();return {{promise:Promise.resolve()}};}}}};
const pdf={{numPages:1,getPage:async()=>page}};
const loadPdfJs=async()=>({{getDocument:()=>({{promise:Promise.resolve(pdf)}})}});
const box={{clientWidth:320,innerHTML:'spinner',appendChild:c=>{{appended=c}},querySelector:()=>null}};
const host={{querySelector:()=>box}},document={{createElement:()=>({{style:{{}},setAttribute(){{}},getContext:()=>({{}})}})}};
const H=String,openElsewhere=()=>{{}};
{_render_pdf()}
(async()=>{{const cleanup=renderPdf(host,{{arrayBuffer:async()=>new ArrayBuffer(1)}},'manual.pdf');
await rendering;
process.stdout.write(JSON.stringify({{width:appended.width,height:appended.height,
 cssWidth:appended.style.width,cssHeight:appended.style.height,transform:rendered.transform,
 cleanupType:typeof cleanup}}));}})();
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == {"width": 600, "height": 800, "cssWidth": "300px",
                   "cssHeight": "400px", "transform": [2, 0, 0, 2, 0, 0],
                   "cleanupType": "function"}


def test_pdf_cleanup_cancels_active_render_and_destroys_document():
    script = f"""
let cancelCount=0,destroyCount=0;
let renderingReady;const rendering=new Promise(r=>renderingReady=r);
const root={{devicePixelRatio:1}};
const task={{cancel:()=>cancelCount++,promise:new Promise(()=>{{}})}};
const page={{getViewport:({{scale}})=>({{width:600*scale,height:800*scale}}),
  render:()=>{{renderingReady();return task;}}}};
const pdf={{numPages:1,getPage:async()=>page,destroy:()=>destroyCount++}};
const loadPdfJs=async()=>({{getDocument:()=>({{promise:Promise.resolve(pdf)}})}});
const box={{clientWidth:320,innerHTML:'spinner',appendChild:()=>{{}},querySelector:()=>null}};
const host={{querySelector:()=>box}},document={{createElement:()=>({{style:{{}},setAttribute(){{}},getContext:()=>({{}})}})}};
const H=String,openElsewhere=()=>{{}};
{_render_pdf()}
(async()=>{{const cleanup=renderPdf(host,{{arrayBuffer:async()=>new ArrayBuffer(1)}},'manual.pdf');
await rendering;cleanup();
process.stdout.write(JSON.stringify({{cancelCount,destroyCount}}));}})();
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == {"cancelCount": 1, "destroyCount": 1}


def test_pdf_density_is_capped_for_mobile_memory_safety():
    body = _render_pdf()
    assert "Math.min(2, Number(root.devicePixelRatio)" in body
    assert "viewport.width * outputScale" in body
    assert "canvas.style.width" in body
