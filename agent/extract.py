"""
Tool: field_extractor
Pulls the four required fields (VIN, date_of_loss, insurance_payout,
loan_balance) out of a document's raw text using layout-aware regexes, and
assigns each extraction a confidence + reason.

This is deliberately NOT an LLM call. These documents have a small, fairly
consistent set of layouts (label -> value), and regex extraction is faster,
cheaper, and fully deterministic/auditable for structured fields like this.
The LLM is reserved for the parts of the pipeline that need actual judgment:
resolving conflicts, reading free-text customer replies, and writing the
customer message (see llm_client.py / reply_processor.py).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional

VIN_RE = r"[A-HJ-NPR-Z0-9]{17}"          # standard VIN alphabet (no I/O/Q)
VIN_LOOSE_RE = r"[A-Z0-9]{10,19}"          # fallback for OCR-mangled VINs
DATE_RE = r"\d{1,2}/\d{1,2}/\d{2,4}"
MONEY_RE = r"\$?\s?[\d,]+(?:\.\d{2})?"      # cents are optional -- "$35,000" is valid too


def _confidence_for_quality(ocr_quality: str) -> str:
    """Map document read quality directly to field-extraction confidence.
    Previously this collapsed "medium" (i.e. came from OCR) into "high",
    which meant an OCR'd document could outrank its actual reliability."""
    return {"high": "high", "medium": "medium", "low": "low"}.get(ocr_quality, "low")


@dataclass
class FieldValue:
    value: Optional[str]
    confidence: str            # high / medium / low
    source: str                # filename
    reason: Optional[str] = field(default=None)
    raw_match: Optional[str] = field(default=None)


def _find_near_label(text: str, labels: list[str], value_pattern: str, window: int = 60):
    lowered = text.lower()
    for label in labels:
        for m in re.finditer(re.escape(label.lower()), lowered):
            snippet = text[m.end(): m.end() + window]
            vm = re.search(value_pattern, snippet)
            if vm:
                return vm.group(0)
    return None


def extract_vin(text: str, source: str, ocr_quality: str) -> FieldValue:
    conf = _confidence_for_quality(ocr_quality)

    # Prefer a VIN found near an explicit "VIN" label -- a blind scan for any
    # 17-char alphanumeric string can just as easily match a claim number or
    # policy number, so label-anchored matches get priority and full confidence.
    near = _find_near_label(text, ["VIN:", "VIN"], VIN_RE, window=40)
    if near:
        reason = None if conf == "high" else "Extracted from a lower-quality scan; verify against another document."
        return FieldValue(near, conf, source, reason, near)

    # Blind scan anywhere in the document -- weaker evidence, since there's no
    # label confirming this 17-char string is actually the VIN.
    strict = re.search(VIN_RE, text)
    if strict:
        downgraded = "medium" if conf == "high" else "low"
        return FieldValue(
            strict.group(0), downgraded, source,
            "Found a 17-character VIN-format string but not next to a 'VIN' label -- confirm it isn't another identifier.",
            strict.group(0),
        )

    # Loose fallback near a label for OCR-mangled VINs (wrong length after cleanup).
    near_loose = _find_near_label(text, ["VIN:", "VIN"], VIN_LOOSE_RE, window=40)
    if near_loose:
        cleaned = re.sub(r"[^A-Z0-9]", "", near_loose.upper())
        return FieldValue(
            cleaned, "low", source,
            f"Found near a VIN label but is {len(cleaned)} characters (expected 17) -- likely OCR error.",
            near_loose,
        )
    return FieldValue(None, "low", source, "No VIN pattern found in document.")


def extract_date_of_loss(text: str, source: str, ocr_quality: str) -> FieldValue:
    labels = ["DATE OF LOSS", "DATE OF INCIDENT", "DATE OF INCLDENT", "Date Prepared"]
    val = _find_near_label(text, labels, DATE_RE, window=30)
    if val:
        conf = _confidence_for_quality(ocr_quality)
        reason = None if conf == "high" else "Label matched on a lower-quality read; date digits may be misread."
        return FieldValue(val, conf, source, reason, val)

    # Fallback: first date-like token anywhere (e.g. narrative "On 02/14/2026...")
    m = re.search(DATE_RE, text)
    if m:
        return FieldValue(m.group(0), "medium", source,
                           "No explicit 'date of loss' label found; used first date mentioned in the document.",
                           m.group(0))
    return FieldValue(None, "low", source, "No date found in document.")


def _money_near(text: str, labels: list[str], window: int = 40) -> Optional[str]:
    val = _find_near_label(text, labels, MONEY_RE, window=window)
    if val:
        return val
    return None


def extract_insurance_payout(text: str, source: str, ocr_quality: str) -> FieldValue:
    labels = ["Net Insurance Payout"]
    val = _money_near(text, labels)
    if val:
        conf = _confidence_for_quality(ocr_quality)
        return FieldValue(_clean_money(val), conf, source, None if conf == "high" else "From a lower-quality read.", val)
    return FieldValue(None, "low", source, "No 'Net Insurance Payout' figure found (may not apply to this doc type).")


def extract_loan_balance(text: str, source: str, ocr_quality: str) -> FieldValue:
    labels = ["Outstanding Loan Balance", "CURRENT OUTSTANDING BALANCE", "Current Outstanding Balance"]
    val = _money_near(text, labels, window=60)
    if val:
        conf = _confidence_for_quality(ocr_quality)
        return FieldValue(_clean_money(val), conf, source, None if conf == "high" else "From a lower-quality read.", val)
    return FieldValue(None, "low", source, "No outstanding loan balance figure found (may not apply to this doc type).")


def _clean_money(raw: str) -> str:
    return re.sub(r"[^\d.]", "", raw)


def extract_all_fields(text: str, source: str, ocr_quality: str) -> dict[str, FieldValue]:
    return {
        "vin": extract_vin(text, source, ocr_quality),
        "date_of_loss": extract_date_of_loss(text, source, ocr_quality),
        "insurance_payout": extract_insurance_payout(text, source, ocr_quality),
        "loan_balance": extract_loan_balance(text, source, ocr_quality),
    }