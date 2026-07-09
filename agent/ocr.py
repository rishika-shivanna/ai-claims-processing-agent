"""
Tool: document_reader
Turns a file on disk (PDF, PNG/JPG image, or plain text) into raw text.

This is the lowest-level tool in the system. It never makes judgments about
content -- it just gets text out of a file as reliably as it can, and reports
*how* it got that text (clean PDF layer vs OCR) so downstream extraction can
weight its confidence accordingly.
"""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass

import cv2
import numpy as np
import pdfplumber
import pytesseract
from PIL import Image


@dataclass
class RawText:
    text: str
    method: str          # "pdf_text", "ocr", "plain_text", "pdf_ocr_fallback"
    quality: str          # "high", "medium", "low" -- rough signal for downstream confidence


def _preprocess_for_ocr(path: str) -> np.ndarray:
    """Deskew + denoise + adaptive-threshold a scanned image.

    Scanned insurance docs in this dataset come in noisy and slightly rotated.
    A naive tesseract call on the raw image loses most of the text (verified
    empirically on this dataset). This pipeline recovers the bulk of it.
    """
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"Could not read image file (missing, corrupt, or unsupported format): {path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.fastNlMeansDenoising(gray, h=15)

    th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(th > 0))
    if len(coords) > 0:
        angle = cv2.minAreaRect(coords)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
    else:
        angle = 0.0

    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    rotated = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)

    out = cv2.adaptiveThreshold(rotated, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 31, 11)
    return out


def _ocr_image(path: str) -> str:
    processed = _preprocess_for_ocr(path)
    # --psm 6 assumes a single uniform block of text, which fits typed
    # insurance forms/letters better than Tesseract's default page-segmentation mode.
    return pytesseract.image_to_string(processed, config="--psm 6")


def _quality_from_text(text: str) -> str:
    """Cheap heuristic: OCR garbage tends to be short and symbol-heavy."""
    letters = sum(c.isalnum() for c in text)
    if letters < 40:
        return "low"
    junk = sum(1 for c in text if c in "@#$%^&*_~|\\")
    ratio = junk / max(letters, 1)
    if ratio > 0.03:
        return "low"
    return "medium"


def read_document(path: str) -> RawText:
    ext = os.path.splitext(path)[1].lower()

    if ext == ".txt":
        with open(path, "r", errors="replace") as f:
            return RawText(text=f.read(), method="plain_text", quality="high")

    if ext == ".pdf":
        text_parts = []
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    text_parts.append(t)
        except Exception:
            pass
        text = "\n".join(text_parts).strip()
        if len(text) > 30:
            return RawText(text=text, method="pdf_text", quality="high")

        # Scanned/image-only PDF -- fall back to rasterize + OCR.
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            ocr_chunks = []
            for page in doc:
                pix = page.get_pixmap(dpi=300)
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".png")
                os.close(tmp_fd)
                try:
                    pix.save(tmp_path)
                    ocr_chunks.append(_ocr_image(tmp_path))
                finally:
                    os.remove(tmp_path)
            text = "\n".join(ocr_chunks).strip()
            return RawText(text=text, method="pdf_ocr_fallback",
                            quality=_quality_from_text(text))
        except Exception:
            return RawText(text=text, method="pdf_text", quality="low")

    if ext in (".png", ".jpg", ".jpeg"):
        text = _ocr_image(path)
        return RawText(text=text, method="ocr", quality=_quality_from_text(text))

    # Unknown extension -- best effort as plain text.
    try:
        with open(path, "r", errors="replace") as f:
            return RawText(text=f.read(), method="plain_text", quality="medium")
    except Exception:
        return RawText(text="", method="unknown", quality="low")