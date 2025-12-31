import base64
import io
from typing import Optional


def extract_image_text(image_base64: str, max_chars: int = 50000) -> Optional[str]:
    """Extract text from an image using OCR (pytesseract)"""
    try:
        import pytesseract
        from PIL import Image

        image_bytes = base64.b64decode(image_base64)
        image = Image.open(io.BytesIO(image_bytes))

        # Run OCR
        text = pytesseract.image_to_string(image)

        if text and text.strip():
            result = text.strip()
            if len(result) > max_chars:
                result = result[:max_chars] + "\n\n[Text truncated...]"
            print(f"[OCR] Extracted {len(result)} characters from image")
            return result

        return None
    except Exception as e:
        print(f"[OCR] Extraction error: {e}")
        return None


def extract_pdf_text(pdf_base64: str, max_chars: int = 50000) -> Optional[str]:
    """Extract text from a base64-encoded PDF"""
    try:
        import fitz  # PyMuPDF
        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        text_parts = []
        for page_num, page in enumerate(doc, 1):
            text = page.get_text()
            if text.strip():
                text_parts.append(f"--- Page {page_num} ---\n{text}")

        doc.close()
        full_text = "\n\n".join(text_parts)

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[Document truncated...]"

        return full_text if full_text.strip() else None
    except Exception as e:
        print(f"[PDF] Extraction error: {e}")
        return None


def extract_docx_text(docx_base64: str, max_chars: int = 50000) -> Optional[str]:
    """Extract text from a base64-encoded DOCX file"""
    try:
        from docx import Document
        docx_bytes = base64.b64decode(docx_base64)
        doc = Document(io.BytesIO(docx_bytes))

        text_parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)

        # Also get text from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    text_parts.append(row_text)

        full_text = "\n".join(text_parts)

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[Document truncated...]"

        return full_text if full_text.strip() else None
    except Exception as e:
        print(f"[DOCX] Extraction error: {e}")
        return None


def extract_xlsx_text(xlsx_base64: str, max_chars: int = 50000) -> Optional[str]:
    """Extract text from a base64-encoded XLSX file"""
    try:
        from openpyxl import load_workbook
        xlsx_bytes = base64.b64decode(xlsx_base64)
        wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)

        text_parts = []
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            text_parts.append(f"--- Sheet: {sheet_name} ---")

            for row in sheet.iter_rows(values_only=True):
                row_values = [str(cell) if cell is not None else "" for cell in row]
                if any(v.strip() for v in row_values):
                    text_parts.append(" | ".join(row_values))

        wb.close()
        full_text = "\n".join(text_parts)

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[Document truncated...]"

        return full_text if full_text.strip() else None
    except Exception as e:
        print(f"[XLSX] Extraction error: {e}")
        return None


def extract_pptx_text(pptx_base64: str, max_chars: int = 50000) -> Optional[str]:
    """Extract text from a base64-encoded PPTX file"""
    try:
        from pptx import Presentation
        pptx_bytes = base64.b64decode(pptx_base64)
        prs = Presentation(io.BytesIO(pptx_bytes))

        text_parts = []
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_text = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_text.append(shape.text)

            if slide_text:
                text_parts.append(f"--- Slide {slide_num} ---\n" + "\n".join(slide_text))

        full_text = "\n\n".join(text_parts)

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[Document truncated...]"

        return full_text if full_text.strip() else None
    except Exception as e:
        print(f"[PPTX] Extraction error: {e}")
        return None


def extract_document_text(document_base64: str, max_chars: int = 50000) -> Optional[str]:
    """Try to extract text from an Office document (auto-detect type)"""
    # Try each format - the wrong format will fail quickly
    for extractor in [extract_docx_text, extract_xlsx_text, extract_pptx_text]:
        result = extractor(document_base64, max_chars)
        if result:
            return result
    return None
