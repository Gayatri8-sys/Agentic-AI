"""
tools/pdf_extractor.py

Extracts text from PDFs — including handwritten/scanned ones.
Strategy:
  1. Try pdfplumber for digital text (fast, free).
  2. If a page yields < MIN_CHARS, rasterize it and send to Gemini Vision.
  3. Returns per-page text with source metadata for attribution.

Handles BOTH:
  A) Multiple small PDFs (one per document type) in a patient folder
  B) Single large combined PDF (all page types in one file) — real-world case

Requires:
  GOOGLE_API_KEY env var  (Gemini Vision — for handwritten/scanned pages)
  pip install pdfplumber pymupdf pillow google-generativeai
"""

from __future__ import annotations
import base64
import os
import time
from pathlib import Path

import fitz        # PyMuPDF
import pdfplumber

MIN_CHARS = 80     # pages with fewer chars trigger Gemini OCR
DPI       = 200    # rasterization resolution


# ---------------------------------------------------------------------------
# Gemini Vision OCR
# ---------------------------------------------------------------------------

def _ocr_page_with_gemini(image_bytes: bytes, page_num: int, doc_path: str) -> str:
    try:
        import google.generativeai as genai
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            raise EnvironmentError("GOOGLE_API_KEY not set — cannot OCR handwritten pages.")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        image_part = {
            "mime_type": "image/jpeg",
        
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        }
        prompt = (
            "This is a page from a medical document (possibly handwritten). "
            "Transcribe ALL text you can see, exactly as written. "
            "Preserve structure: use blank lines between sections, "
            "keep bullet points and numbered lists. "
            "If you cannot read a word, write [illegible]. "
            "Do NOT add any commentary or preamble — only the transcribed text."
        )
        response = model.generate_content([prompt, image_part])
        return response.text.strip()
    except Exception as exc:
        return f"[OCR_FAILED page={page_num} doc={doc_path} error={exc}]"


# ---------------------------------------------------------------------------
# Rasterize one PDF page to JPEG bytes
# ---------------------------------------------------------------------------

def _rasterize_page(pdf_path: str, page_index: int, dpi: int = DPI) -> bytes:
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    img_bytes = pix.tobytes("jpeg")
    doc.close()
    return img_bytes


# ---------------------------------------------------------------------------
# Core extractor — one PDF → list of page dicts
# ---------------------------------------------------------------------------

def extract_pdf(pdf_path: str) -> list[dict]:
    """
    Extract text from every page of a PDF.
    Returns list of {page_num, text, method, doc_path}.
    """
    pdf_path = str(pdf_path)
    pages: list[dict] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_num = i + 1
                extracted = page.extract_text() or ""

                if len(extracted.strip()) >= MIN_CHARS:
                    pages.append({
                        "page_num": page_num,
                        "text":     extracted,
                        "method":   "pdfplumber",
                        "doc_path": pdf_path,
                    })
                else:
                    print(f"  [pdf_extractor] Page {page_num}/{total} sparse "
                          f"({len(extracted.strip())} chars) → Gemini Vision OCR")
                    try:
                        img_bytes = _rasterize_page(pdf_path, i)
                        ocr_text  = _ocr_page_with_gemini(img_bytes, page_num, pdf_path)
                        time.sleep(0.5)
                    except Exception as exc:
                        ocr_text = f"[EXTRACTION_FAILED page={page_num} error={exc}]"

                    pages.append({
                        "page_num": page_num,
                        "text":     ocr_text,
                        "method":   "gemini_vision",
                        "doc_path": pdf_path,
                    })

    except Exception as exc:
        pages.append({
            "page_num": 0,
            "text":     f"[PDF_READ_FAILED doc={pdf_path} error={exc}]",
            "method":   "error",
            "doc_path": pdf_path,
        })

    return pages


# ---------------------------------------------------------------------------
# Whole-document classifier (used for small PDFs ≤ 5 pages)
# ---------------------------------------------------------------------------

def classify_doc_type(pages: list[dict]) -> str:
    text = " ".join(p["text"] for p in pages[:2]).lower()
    keywords = {
        "admission_note":    ["admission note", "admitting diagnosis", "chief complaint",
                               "history of present illness", "hpi", "admission date"],
        "progress_note":     ["progress note", "assessment and plan", "soap note",
                               "subjective:", "objective:", "assessment:", "plan:"],
        "lab_result":        ["laboratory", "lab result", "reference range", "specimen",
                               "complete blood count", "cbc", "bmp", "cmp", "hemoglobin",
                               "glucose", "sodium", "potassium", "creatinine"],
        "medication_record": ["medication list", "medication administration", "mar",
                               "discharge medications", "admission medications",
                               "prescribed", "dosage", "sig:", "refills"],
    }
    scores: dict[str, int] = {k: 0 for k in keywords}
    for doc_type, kws in keywords.items():
        for kw in kws:
            if kw in text:
                scores[doc_type] += 1
    best_type = max(scores, key=lambda k: scores[k])
    return best_type if scores[best_type] > 0 else "unknown"


# ---------------------------------------------------------------------------
# Per-page classifier (used for large combined PDFs > 5 pages)
# ---------------------------------------------------------------------------

# Keywords that identify each page type
_PAGE_TYPE_KEYWORDS: dict[str, list[str]] = {
    "admission_note": [
        "chief complaint", "history of present illness", "hpi",
        "admission note", "admitting diagnosis", "past history",
        "case record", "admission record", "allergic history",
        "provisional diagnosis", "final diagnosis", "past medical history",
        "diagnosis:", "history:", "physical examination",
    ],
    "progress_note": [
        "progress note", "assessment and plan", "soap",
        "consultation sheet", "course in hospital", "hospital course",
        "nursing documentation", "nurses notes", "nursing assessment",
        "nurse notes", "condition at discharge", "advice on discharge",
        "follow-up", "follow up", "discharge condition",
    ],
    "lab_result": [
        "laboratory", "lab result", "reference range", "specimen",
        "complete blood count", "cbc", "haematology", "biochemistry",
        "hemoglobin", "glucose", "sodium", "potassium", "creatinine",
        "clinical pathology", "urine routine", "abg", "arterial blood",
        "widal", "crp", "c-reactive", "serum electrolytes",
        "investigation", "result value", "biological reference",
        "haematology report", "biochemistry report", "pathology report",
        "urine culture", "blood culture", "sensitivity",
    ],
    "medication_record": [
        "drug chart", "discharge medications", "admission medications",
        "advice on discharge", "tablet", "capsule",
        "dose", "route", "frequency", "inj.", "tab.",
        "medication name", "dosage", "duration",
        "regular prescription", "once only",
    ],
}

# Pages that are mostly drawings/charts — skip for clinical extraction
_SKIP_KEYWORDS = [
    "bed sore", "pulses + -", "bed sores stage",
    "cauti", "catheter associated urinary",
    "diabets hospital monitoring",
    "graphic(tpr)chart",
    "no bed sore",
]


def _classify_single_page(text: str) -> str:
    """Classify one page's text into a document type."""
    text_lower = text.lower().strip()

    # Skip non-clinical chart pages
    if len(text_lower) < 100:
        return "skip"
    for skip_kw in _SKIP_KEYWORDS:
        if skip_kw in text_lower:
            return "skip"

    scores: dict[str, int] = {k: 0 for k in _PAGE_TYPE_KEYWORDS}
    for doc_type, keywords in _PAGE_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[doc_type] += 1

    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "unknown"


# ---------------------------------------------------------------------------
# Main loader — handles both single large PDF and multiple small PDFs
# ---------------------------------------------------------------------------

def load_patient_documents(patient_folder: str) -> list[dict]:
    """
    Load all PDFs from a patient folder.

    Case A — multiple small PDFs (≤5 pages each):
        Classifies each PDF as a whole. Original behaviour.

    Case B — single large combined PDF (>5 pages):
        Classifies each page individually.
        Groups pages by type into virtual documents.
        This handles real-world records where one PDF contains
        admission notes, labs, nursing notes, drug charts, etc.

    Returns list of doc dicts:
        {doc_id, doc_type, pages: list[dict], full_text: str}
    """
    folder = Path(patient_folder)
    docs: list[dict] = []

    pdf_files = sorted(folder.glob("*.pdf"))
    if not pdf_files:
        print(f"  [pdf_extractor] WARNING: No PDFs found in {folder}")
        return docs

    for pdf_file in pdf_files:
        print(f"\n  [pdf_extractor] Loading: {pdf_file.name}")
        pages = extract_pdf(str(pdf_file))

        # ── Case A: small PDF — classify whole document ──────────────────
        if len(pages) <= 5:
            doc_type  = classify_doc_type(pages)
            full_text = "\n\n".join(
                f"[Page {p['page_num']}]\n{p['text']}" for p in pages
            )
            docs.append({
                "doc_id":    str(pdf_file),
                "doc_type":  doc_type,
                "pages":     pages,
                "full_text": full_text,
            })
            print(f"  [pdf_extractor] → classified as: {doc_type}  ({len(pages)} pages)")

        # ── Case B: large combined PDF — classify per page ────────────────
        else:
            print(f"  [pdf_extractor] Large PDF ({len(pages)} pages) — classifying per page...")

            # Bucket pages by detected type
            type_buckets: dict[str, list[dict]] = {}
            for page in pages:
                page_type = _classify_single_page(page["text"])
                if page_type in ("skip", "unknown"):
                    continue
                type_buckets.setdefault(page_type, []).append(page)

            # If nothing was classified, fall back to treating the whole PDF
            # as one unknown document so we don't silently drop data
            if not type_buckets:
                print(f"  [pdf_extractor] WARNING: No pages classified — using full PDF as 'unknown'")
                full_text = "\n\n".join(
                    f"[Page {p['page_num']}]\n{p['text']}" for p in pages
                )
                docs.append({
                    "doc_id":    str(pdf_file),
                    "doc_type":  "unknown",
                    "pages":     pages,
                    "full_text": full_text,
                })
            else:
                for doc_type, type_pages in type_buckets.items():
                    full_text = "\n\n".join(
                        f"[Page {p['page_num']}]\n{p['text']}"
                        for p in type_pages
                    )
                    virtual_doc_id = f"{pdf_file}::{doc_type}"
                    docs.append({
                        "doc_id":    virtual_doc_id,
                        "doc_type":  doc_type,
                        "pages":     type_pages,
                        "full_text": full_text,
                    })
                    print(f"  [pdf_extractor] → virtual doc '{doc_type}': "
                          f"{len(type_pages)} pages "
                          f"(pages {[p['page_num'] for p in type_pages]})")

    return docs