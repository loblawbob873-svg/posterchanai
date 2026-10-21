"""AI Assistant: what you can do with attached files -- Compress and Convert for a PDF, Combine for several.

Reported: "When adding a file, compress is not an option. For example, I wanted to compress a PDF." /
"When adding multiple files, combine is not an option. For example, I wanted to combine multiple PDF
files." / "Convert Images/PDF -> Only supports 1 file."

The real client's AI view, files attached through its own file input:

  * one PDF offers Compress and Convert (they only ever appeared for images);
  * two PDFs lead with "Combine PDFs", which sends `combine` with BOTH files (the server merges them);
  * two images lead with "Combine into one PDF", which sends `convert pdf` with both;
  * the ✨ Convert and Compress sheets let you pick more than one file.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_effects_studio_mentioned_full_app as base

ATTACH = r"""(async kinds=>{const dt=new DataTransfer();
  kinds.forEach((k,i)=>dt.items.add(k==='pdf'?new File(['%PDF-1.4 '+i],'doc'+i+'.pdf',{type:'application/pdf'})
                                            :new File([new Uint8Array([137,80,78,71,i])],'pic'+i+'.png',{type:'image/png'})));
  const inp=document.getElementById('ai-file');inp.files=dt.files;inp.dispatchEvent(new Event('change',{bubbles:true}));})"""
ACTS = "[...document.querySelectorAll('#ai-attachbar .fx-act')].map(x=>x.textContent)"
CLEAR = "(()=>{while(document.querySelector('#ai-attachbar .ai-chip-x'))document.querySelector('#ai-attachbar .ai-chip-x').click();})()"


async def attach(b, kinds):
    await b.js(CLEAR)
    await b.js(ATTACH + "(" + str(kinds).replace("'", '"') + ")")
    await b.until(f"document.querySelectorAll('#ai-attachbar .ai-chip').length==={len(kinds)}")
    return await b.js(ACTS)


async def click(b, label):
    await b.js("[...document.querySelectorAll('#ai-attachbar .fx-act')].find(x=>x.textContent===%r).click()" % label)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome is required")
def test_pdfs_can_be_compressed_converted_and_combined():
    got = {}

    async def check(b):
        await b.js("__PC.switchView('ai')")
        await b.until("!!document.getElementById('ai-file')")
        got["one_pdf"] = await attach(b, ["pdf"])
        got["two_pdfs"] = await attach(b, ["pdf", "pdf"])
        n = await b.js("__chatRequests.length")
        await click(b, "📚 Combine PDFs")
        await b.until(f"__chatRequests.length>{n} && __chatRequests.some(m=>m.type==='message')")
        got["combine_sent"] = await b.js("(()=>{const m=__chatRequests.filter(m=>m.type==='message').pop();"
                                         "return {content:m.content,pdfs:(m.pdfs||[]).map(p=>p.filename)};})()")
        got["two_images"] = await attach(b, ["img", "img"])
        await click(b, "📚 Combine into one PDF")
        await b.until("__chatRequests.filter(m=>m.type==='message').length>=2")
        got["images_sent"] = await b.js("(()=>{const m=__chatRequests.filter(m=>m.type==='message').pop();"
                                        "return {content:m.content,images:(m.images||[]).map(p=>p.filename)};})()")

    asyncio.run(base.run(check))
    assert "🗜 Compress" in got["one_pdf"] and "🔄 Convert" in got["one_pdf"], ("a PDF offers no Compress/Convert", got["one_pdf"])
    assert "📚 Combine PDFs" not in got["one_pdf"], got["one_pdf"]
    assert got["two_pdfs"][0] == "📚 Combine PDFs", ("two PDFs offer no Combine", got["two_pdfs"])
    assert got["combine_sent"] == {"content": "combine", "pdfs": ["doc0.pdf", "doc1.pdf"]}, got["combine_sent"]
    assert got["two_images"][0] == "📚 Combine into one PDF", got["two_images"]
    assert got["images_sent"] == {"content": "convert pdf", "images": ["pic0.png", "pic1.png"]}, got["images_sent"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome is required")
def test_the_convert_and_compress_sheets_take_several_files():
    got = {}

    async def check(b):
        await b.js("__PC.switchView('ai')")
        await b.until("!!document.getElementById('ai-file')")
        for gen in ("convert", "compress"):
            await b.until("!!document.querySelector('[data-gen=%s]')" % gen)
            await b.js("document.querySelector('[data-gen=%s]').click()" % gen)
            await b.until("!!document.getElementById('gen-file')")
            got[gen] = await b.js("document.getElementById('gen-file').multiple")
            await b.js("document.querySelector('.modal-bg')&&document.querySelector('.modal-bg').click();"
                       "try{__PC.closeModal&&__PC.closeModal()}catch(_){}")
            await b.until("!document.getElementById('gen-file')")

    asyncio.run(base.run(check))
    assert got == {"convert": True, "compress": True}, ("a sheet still takes only one file", got)
