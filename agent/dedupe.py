"""
Tool: duplicate_detector
Handles two flavors of "duplicate" seen in this dataset:

1. True duplicates -- byte/near-identical copies of the same document
   (same doc_type, near-identical extracted text).
2. Superseding revisions -- a second file of the same required doc_type
   with different figures, explicitly marked as replacing the first
   (e.g. a "_v2" settlement breakdown with a "REVISED" / "supersedes" note).
   These are NOT thrown out as noise -- the newer one is treated as
   authoritative and the older one is kept for the audit trail.

A third case is now handled explicitly: multiple files of the same type with
DIFFERING content and NO revision marker. This is genuinely ambiguous --
rather than silently defaulting to the first file, it's flagged with
needs_review=True so the agent surfaces it as a real issue instead of a
comment that nothing downstream ever reads.
"""
from __future__ import annotations
import difflib
import re
from dataclasses import dataclass, field
from itertools import combinations


@dataclass
class DocGroup:
    doc_type: str
    files: list[str]
    authoritative_file: str
    relationship: str   # "single", "exact_duplicate", "superseding_revision", "conflicting_versions"
    note: str | None = None
    needs_review: bool = False


REVISION_MARKERS = re.compile(r"\brevised\b|\bsupersedes\b|\bv\d+\b", re.IGNORECASE)
VERSION_NUMBER = re.compile(r"\bv(\d+)\b", re.IGNORECASE)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _all_pairs_similar(items: list[dict], threshold: float = 0.9) -> bool:
    """True only if EVERY pair in the group is near-identical -- checking all
    pairs (not just the first two) so a group of 3+ files where one is
    genuinely different doesn't get swept into 'exact duplicate'."""
    for a, b in combinations(items, 2):
        if _similarity(a["text"], b["text"]) <= threshold:
            return False
    return True


def _version_number(item: dict) -> int:
    """Extract an actual version number from filename or text (e.g. 'v2' -> 2)
    rather than relying on alphabetical filename sort, which breaks on
    v2 vs v10."""
    m = VERSION_NUMBER.search(item["file"]) or VERSION_NUMBER.search(item["text"])
    return int(m.group(1)) if m else 0


def group_documents(docs: list[dict]) -> list[DocGroup]:
    """`docs` is a list of {"file", "doc_type", "text"} dicts for ALL files
    in a claim (already classified). Returns one DocGroup per distinct
    required/extra doc_type."""
    by_type: dict[str, list[dict]] = {}
    for d in docs:
        by_type.setdefault(d["doc_type"], []).append(d)

    groups = []
    for doc_type, items in by_type.items():
        if len(items) == 1:
            groups.append(DocGroup(doc_type, [items[0]["file"]], items[0]["file"], "single"))
            continue

        has_revision_marker = any(REVISION_MARKERS.search(i["text"]) for i in items)

        if has_revision_marker:
            marked = [i for i in items if REVISION_MARKERS.search(i["text"])]
            # Pick by actual parsed version number, not filename string order.
            authoritative = max(marked, key=_version_number)["file"]
            groups.append(DocGroup(
                doc_type, [i["file"] for i in items], authoritative, "superseding_revision",
                note=f"{len(items)} versions found; treated as revised statement, "
                     f"using the highest version number as authoritative."
            ))
        elif _all_pairs_similar(items):
            groups.append(DocGroup(
                doc_type, [i["file"] for i in items], items[0]["file"], "exact_duplicate",
                note=f"{len(items)} near-identical copies of the same {doc_type}; treated as one document."
            ))
        else:
            # Same doc_type, differing content, no revision marker -- genuinely
            # ambiguous. Flagged for real (needs_review=True) instead of a
            # comment nothing downstream reads.
            groups.append(DocGroup(
                doc_type, [i["file"] for i in items], items[0]["file"], "conflicting_versions",
                note=f"{len(items)} documents of type {doc_type} with differing content and no clear "
                     f"revision marker -- cannot automatically determine which is authoritative.",
                needs_review=True,
            ))
    return groups