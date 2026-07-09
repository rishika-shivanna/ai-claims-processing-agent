"""
Tool: consistency_checker
Compares a field's value across every document that reported it (using only
the authoritative file from each duplicate group) and decides:
  - all agree -> single value, confidence = weakest of the contributing confidences
  - disagreement -> flag issue, list the conflicting sources, and propose an
    educated guess

Educated-guess policy (documented here since it's a judgment call):
  1. If one value came from a doc explicitly marked as a superseding revision,
     trust it over the superseded one.
  2. Otherwise prefer the value with the higher extraction confidence.
  3. If confidences tie, prefer whichever value appears in more documents
     (majority vote).
  4. If still tied, no guess is offered -- surfaced as needs_review.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

CONF_RANK = {"high": 2, "medium": 1, "low": 0}


@dataclass
class FieldConsensus:
    value: str | None
    confidence: str
    contributors: list[dict]        # [{"file":..., "value":..., "confidence":...}]
    conflict: bool
    guess_reason: str | None = None


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def _normalize(field_name: str, value: str) -> str:
    if field_name in ("insurance_payout", "loan_balance"):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return value
    if field_name == "date_of_loss" and value:
        # Different documents may report the same date in different formats
        # (02/14/2026 vs 2026-02-14) -- normalize to one form before comparing,
        # or a formatting difference would look like a genuine conflict.
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return value.strip()
    return value.strip().upper() if value else value


def reconcile_field(field_name: str, contributors: list[dict], superseded_files: set[str]) -> FieldConsensus:
    """contributors: [{"file", "value", "confidence"}] already filtered to
    authoritative documents only (one entry per doc, may be None value)."""
    present = [c for c in contributors if c["value"]]
    if not present:
        return FieldConsensus(None, "low", contributors, conflict=False,
                               guess_reason=None)

    normalized_groups: dict[str, list[dict]] = {}
    for c in present:
        key = _normalize(field_name, c["value"])
        normalized_groups.setdefault(key, []).append(c)

    if len(normalized_groups) == 1:
        key = next(iter(normalized_groups))
        confs = [c["confidence"] for c in normalized_groups[key]]
        weakest = min(confs, key=lambda c: CONF_RANK[c])
        return FieldConsensus(present[0]["value"], weakest, contributors, conflict=False)

    # Disagreement -- pick a best guess per the documented policy.
    non_superseded = {k: v for k, v in normalized_groups.items()
                       if not all(c["file"] in superseded_files for c in v)}
    candidates = non_superseded or normalized_groups

    def score(item):
        key, group = item
        best_conf = max(CONF_RANK[c["confidence"]] for c in group)
        return (best_conf, len(group))

    best_key, best_group = max(candidates.items(), key=score)
    tied = [k for k, g in candidates.items() if score((k, g)) == score((best_key, best_group))]

    if len(tied) > 1:
        return FieldConsensus(None, "low", contributors, conflict=True,
                               guess_reason="Multiple conflicting values with equal confidence/support -- "
                                            "no reliable guess; needs human review.")

    reason_bits = []
    if non_superseded != normalized_groups:
        reason_bits.append("preferred the non-superseded document(s)")
    reason_bits.append(f"highest extraction confidence ({best_group[0]['confidence']})")
    if len(best_group) > 1:
        reason_bits.append(f"supported by {len(best_group)} documents")

    return FieldConsensus(
        value=best_group[0]["value"],
        confidence=best_group[0]["confidence"],
        contributors=contributors,
        conflict=True,
        guess_reason="Best guess based on " + ", ".join(reason_bits) + ".",
    )