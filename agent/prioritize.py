"""
Ranks processed claims into a recommended finalization order.
Heuristic (documented, not a black box):
  0. complete                                            -> finalize now
  1. a customer reply already narrowed the gap
     (fastest remaining path to close)
  2. needs_review with an official-document conflict (police report vs.
     finance agreement etc.) -- needs an adjuster, not just paperwork,
     regardless of whether the claim is ALSO missing documents
  3. incomplete, no reply yet, no conflict -- a single outbound message
     likely resolves it
  4. needs_review from low-confidence/noisy scans alone, nothing structurally
     wrong -- lowest urgency, likely resolves with a manual re-read of the
     source scan
Ties broken by: fewer outstanding issues first.
"""
from __future__ import annotations
def _bucket(claim: dict) -> int:
    status = claim["status"]
    has_reply_progress = claim.get("customer_replied", False)
    has_official_conflict = any(i["type"] == "inconsistency" for i in claim["issues"])
    if status == "complete":
        return 0
    if has_official_conflict:
        return 2
    if has_reply_progress:
        return 1
    if status == "incomplete":
        return 3
    return 4
_REASONS = {
    0: "All required documents present, fields extracted cleanly, and no conflicts -- ready to finalize.",
    1: "Customer has already replied and narrowed the gap; one more short exchange likely closes this.",
    2: "Official documents disagree on a key field -- needs adjuster judgment, not customer follow-up "
       "(even if a document is also still missing).",
    3: "Missing documents/fields but nothing in conflict -- a single outbound request should resolve it.",
    4: "Only low-confidence extractions from noisy scans, no missing pieces or hard conflicts -- lowest urgency.",
}
def prioritize(claims: list[dict]) -> dict:
    scored = sorted(claims, key=lambda c: (_bucket(c), len(c["issues"])))
    order = [{"claim_id": c["claim_id"], "reason": _REASONS[_bucket(c)]} for c in scored]
    return {"processing_order": order}