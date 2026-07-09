"""
Tool: document_classifier
Decides what kind of document a file is, using filename first (cheap, usually
reliable in this dataset) and falling back to content keywords when the
filename doesn't tell us anything (e.g. a generic "scan001.png").

Anything that isn't one of the three required types is still classified
(so the agent can look at it / mention it) but tagged as an "extra" type
rather than forcing it into one of the required buckets.
"""
from __future__ import annotations
import os
import re

REQUIRED_TYPES = {"police_report", "finance_agreement", "settlement_breakdown"}

_FILENAME_HINTS = {
    "police_report": ["police", "incident_report", "accident_report"],
    "finance_agreement": ["finance_agreement", "loan", "installment", "retail_contract"],
    "settlement_breakdown": ["settlement"],
    "customer_reply": ["customer_reply", "reply", "email"],
    "adjuster_note": ["adjuster"],
    "tow_receipt": ["tow"],
}

_CONTENT_HINTS = {
    "police_report": [r"police department", r"incident report", r"reporting officer",
                       r"badge no"],
    "finance_agreement": [r"retail installment contract", r"auto finance agreement",
                           r"outstanding balance", r"lender"],
    "settlement_breakdown": [r"settlement breakdown", r"actual cash value",
                              r"net insurance payout", r"gap amount"],
    "customer_reply": [r"^from:.*\n?to:", r"subject:"],
    "adjuster_note": [r"adjuster notes?", r"field adjuster"],
    "tow_receipt": [r"towing", r"tow yard", r"recovery"],
}


def classify_document(path: str, text: str) -> tuple[str, float]:
    """Returns (doc_type, confidence 0-1). doc_type is one of the known
    labels above, or 'unknown'."""
    filename = os.path.basename(path).lower()

    for doc_type, hints in _FILENAME_HINTS.items():
        if any(h in filename for h in hints):
            return doc_type, 0.95

    lowered = text.lower()
    scores = {}
    for doc_type, patterns in _CONTENT_HINTS.items():
        hits = sum(1 for p in patterns if re.search(p, lowered, re.IGNORECASE | re.MULTILINE))
        if hits:
            scores[doc_type] = hits

    if scores:
        best = max(scores, key=lambda k: scores[k])
        confidence = min(0.55 + 0.15 * scores[best], 0.9)
        return best, confidence

    return "unknown", 0.2


def is_required(doc_type: str) -> bool:
    return doc_type in REQUIRED_TYPES
