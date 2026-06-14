"""Flashcards study tool: turn an uploaded PDF / image / slide deck / doc into an interactive
MULTIPLE-CHOICE quiz deck via the LLM.

- `extract_source_text(attachments)` → pulls text from the upload (PyMuPDF for PDFs, tesseract
  OCR for images, python-pptx/docx/openpyxl for Office/slides — all via `document_service`).
- `generate_flashcards(text, chat_service)` → asks the LLM for a JSON array of questions, each with
  4 answer options, the correct index, and an explanation. For math, the explanation carries the
  worked steps (LaTeX in `$...$`).
- `render_card_png(...)` → a clean branded PNG of a card (question face, or revealed result face
  with the correct option + explanation) for the Telegram image-card UI. The web renders its own
  cards with clickable option buttons and KaTeX math.

Ephemeral by design: nothing is persisted. The web keeps the deck in the page; Telegram keeps it
in a per-chat cache.
"""
import base64
import io
import json
import logging
import os
import random
import re
from typing import List, Optional, Tuple

from app.services import document_service
from app.services.media_service import is_image, is_pdf, _outro_font

logger = logging.getLogger(__name__)

MAX_CARDS = 12
_MAX_SOURCE_CHARS = 40000
_OPTION_LETTERS = ["A", "B", "C", "D", "E"]


def extract_source_text(attachments: Optional[list]) -> Tuple[str, str, bool]:
    """Extract study text from uploaded attachments.

    `attachments` is a list of (filename, data_bytes, content_type). Returns
    (combined_text, source_label, was_image_only). `was_image_only` is True when EVERY source was
    an image (OCR) — the caller surfaces a low-confidence-OCR note, since OCR on dense
    screenshots/photos is unreliable.
    """
    if not attachments:
        return "", "", False
    parts: List[str] = []
    label = ""
    saw_text_source = False
    saw_image = False
    for fn, data, ct in attachments:
        if not data:
            continue
        if not label:
            label = os.path.splitext(os.path.basename(fn or "document"))[0][:60] or "document"
        b64 = base64.b64encode(data).decode("ascii")
        text = None
        try:
            if is_pdf(fn, ct):
                text = document_service.extract_pdf_text(b64, max_chars=_MAX_SOURCE_CHARS)
                saw_text_source = True
            elif is_image(fn, ct):
                text = document_service.extract_image_text(b64, max_chars=_MAX_SOURCE_CHARS)
                saw_image = True
            else:
                text = document_service.extract_document_text(b64, max_chars=_MAX_SOURCE_CHARS)
                saw_text_source = True
        except Exception as e:
            logger.warning(f"[FLASHCARDS] extract failed for {fn}: {e}")
        if text and text.strip():
            parts.append(text.strip())
    combined = "\n\n".join(parts)[:_MAX_SOURCE_CHARS]
    return combined, label or "document", (saw_image and not saw_text_source)


_SYSTEM_PROMPT = (
    "You are a study assistant that turns study material into multiple-choice quiz questions. "
    "You output ONLY a JSON array, nothing else."
)


def _build_user_prompt(text: str, max_cards: int) -> str:
    return (
        f"Create up to {max_cards} multiple-choice flashcards from the study material below.\n\n"
        "Return ONLY a JSON array. Each element is an object:\n"
        '  {"question": "...", "answer": "correct option", '
        '"distractors": ["wrong 1", "wrong 2", "wrong 3"], '
        '"explanation": "why the answer is correct", "math": true/false}\n\n'
        "Rules:\n"
        "- 3 plausible but clearly wrong distractors per question (same type/length as the answer).\n"
        "- General material: question on a key fact/term; the answer is the correct choice.\n"
        "- Math problems: the question is the problem; the answer is the final result; the "
        "explanation gives a short step-by-step solution. Set \"math\": true.\n"
        "- Use LaTeX delimited by $...$ for equations or math symbols.\n"
        "- Keep questions and options concise. Cover the most important points. No duplicates.\n"
        "- Output the JSON array only — no prose, no code fences.\n\n"
        f"Study material:\n\"\"\"\n{text}\n\"\"\""
    )


def _parse_cards_json(raw: str) -> List[dict]:
    """Tolerantly parse the model's reply into quiz cards. Handles code fences and surrounding
    prose; falls back to the first [...] block. Each card → {question, options[], correct,
    explanation, math} with options shuffled and `correct` the index of the right one."""
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    candidates = [s]
    m = re.search(r"\[.*\]", s, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        cards = []
        for item in data:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or item.get("front") or item.get("q") or "").strip()
            answer = str(item.get("answer") or item.get("correct") or "").strip()
            distractors = item.get("distractors") or item.get("options") or item.get("wrong") or []
            if not isinstance(distractors, list):
                distractors = []
            distractors = [str(x).strip() for x in distractors if str(x).strip() and str(x).strip() != answer]
            explanation = str(item.get("explanation") or item.get("back") or item.get("a") or "").strip()
            if not question or not answer or len(distractors) < 1:
                continue
            options = [answer] + distractors[:4]
            random.shuffle(options)
            cards.append({
                "question": question,
                "options": options,
                "correct": options.index(answer),
                "explanation": explanation or answer,
                "math": bool(item.get("math", False)),
            })
        if cards:
            return cards
    return []


async def generate_flashcards(text: str, chat_service, max_cards: int = MAX_CARDS) -> List[dict]:
    """Ask the LLM for multiple-choice flashcards from `text`. Returns a list of
    {question, options, correct, explanation, math} (possibly empty if the model didn't return
    parseable JSON)."""
    if not text or not text.strip():
        return []
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(text, max_cards)},
    ]
    try:
        reply = await chat_service.chat(messages)
    except Exception as e:
        logger.error(f"[FLASHCARDS] LLM call failed: {e}")
        return []
    return _parse_cards_json(reply or "")[:max_cards]


# --- Telegram PNG card rendering --------------------------------------------------------------

_CARD_W, _CARD_H = 900, 1200
_BG = (24, 26, 44)
_ACCENT = (255, 170, 60)
_Q_COLOR = (130, 180, 255)
_TEXT_COLOR = (238, 240, 250)
_OK = (130, 230, 160)
_BAD = (240, 120, 120)
_DIM = (150, 155, 180)


def _strip_latex(t: str) -> str:
    """The PNG can't typeset LaTeX, so make math readable. (Web renders real LaTeX via KaTeX.)"""
    t = t.replace("$$", " ").replace("$", "")
    t = re.sub(r"\\(times|cdot)\b", "×", t)
    t = re.sub(r"\\(div)\b", "÷", t)
    t = re.sub(r"\\frac\s*\{([^}]*)\}\s*\{([^}]*)\}", r"(\1)/(\2)", t)
    t = re.sub(r"\\sqrt\s*\{([^}]*)\}", r"√(\1)", t)
    t = re.sub(r"\\[a-zA-Z]+", "", t)
    return t.replace("{", "").replace("}", "")


def _wrap(draw, text: str, font, max_w: int) -> List[str]:
    """Greedy word-wrap to pixel width (handles explicit newlines)."""
    lines: List[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split():
            trial = (cur + " " + word).strip()
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def _draw_block(d, text, top, bottom, max_w, cx, start_sz, color, center=True, min_sz=20):
    """Auto-fit + wrap `text` into the vertical band [top, bottom]; returns the y after drawing."""
    body = _strip_latex(text or "")
    box_h = bottom - top
    fsz = start_sz
    while fsz > min_sz:
        font = _outro_font(fsz)
        lines = _wrap(d, body, font, max_w)
        lh = int(fsz * 1.3)
        if len(lines) * lh <= box_h:
            break
        fsz -= 3
    font = _outro_font(fsz)
    lines = _wrap(d, body, font, max_w)
    lh = int(fsz * 1.3)
    max_lines = max(1, box_h // lh)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = (lines[-1][:40] + "…") if lines[-1] else "…"
    y = top
    for ln in lines:
        x = (cx - d.textlength(ln, font=font) / 2) if center else (cx)
        d.text((x, y), ln, font=font, fill=color)
        y += lh
    return y


def render_card_png(deck_title: str, idx: int, total: int, card: dict,
                    reveal: bool = False, chosen: Optional[int] = None) -> bytes:
    """Render a quiz card to PNG bytes.
    - reveal=False: the question face (the options are shown as Telegram buttons, not on the card).
    - reveal=True: the result face — the correct option (✅) and the user's wrong pick (❌ if any),
      plus the explanation."""
    from PIL import Image, ImageDraw
    W, H = _CARD_W, _CARD_H
    img = Image.new("RGB", (W, H), _BG)
    d = ImageDraw.Draw(img)
    for y in range(H):
        f = y / H
        d.line([(0, y), (W, y)], fill=(int(_BG[0] + 8 * f), int(_BG[1] + 6 * f), int(_BG[2] + 14 * f)))
    margin = int(W * 0.08)
    cx = W // 2

    hf = _outro_font(34)
    d.text((margin, 40), (deck_title or "Flashcards")[:34], font=hf, fill=_DIM)
    prog = f"{idx + 1}/{total}"
    d.text((W - margin - d.textlength(prog, font=hf), 40), prog, font=hf, fill=_ACCENT)
    d.line([(margin, 92), (W - margin, 92)], fill=(60, 64, 96), width=2)

    max_w = W - 2 * margin
    options = card.get("options") or []
    correct = card.get("correct", 0)

    if not reveal:
        label = "QUESTION"
        lf = _outro_font(40)
        d.text((cx - d.textlength(label, font=lf) / 2, 140), label, font=lf, fill=_Q_COLOR)
        _draw_block(d, card.get("question", ""), 230, H - 110, max_w, cx, 60, _TEXT_COLOR)
        ff = _outro_font(28)
        hint = "choose an answer below"
        d.text((cx - d.textlength(hint, font=ff) / 2, H - 70), hint, font=ff, fill=_DIM)
    else:
        # Question (small, top), then options with marks, then explanation.
        y = _draw_block(d, card.get("question", ""), 120, 360, max_w, cx, 44, _TEXT_COLOR)
        y = max(y + 20, 380)
        of = _outro_font(34)
        for i, opt in enumerate(options):
            is_correct = (i == correct)
            is_wrong_pick = (chosen is not None and i == chosen and not is_correct)
            mark = "✓" if is_correct else ("✗" if is_wrong_pick else "  ")
            color = _OK if is_correct else (_BAD if is_wrong_pick else _DIM)
            line = f"{mark} {_OPTION_LETTERS[i]}. {_strip_latex(opt)}"
            for ln in _wrap(d, line, of, max_w):
                d.text((margin, y), ln, font=of, fill=color)
                y += int(34 * 1.3)
            y += 6
        if card.get("explanation"):
            y += 10
            d.line([(margin, y), (W - margin, y)], fill=(60, 64, 96), width=2)
            y += 16
            ef = _outro_font(30)
            d.text((margin, y), "Why:", font=ef, fill=_ACCENT)
            y += int(30 * 1.4)
            _draw_block(d, card["explanation"], y, H - 60, max_w, margin, 30, _TEXT_COLOR, center=False)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
