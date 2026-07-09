"""
ClaimsAgent -- the orchestrator.

This is the only piece of the system that decides WHICH tools to call and
in what order, and it does so conditionally per claim rather than running a
fixed pipeline:

  - OCR is only invoked for image/scanned files; clean PDFs skip straight to
    text extraction.
  - The duplicate/consistency tools are only invoked when there's actually
    more than one document to compare.
  - The customer-reply reconciliation step only runs if a customer_reply.txt
    is present in the claim folder.
  - The LLM is only called for the free-text reply parsing and the
    customer-message drafting -- never for the structured field extraction,
    which stays deterministic.
  - `escalate` vs `message_customer` is chosen based on whether the
    remaining problem is something the customer can actually resolve.

See README.md for the reasoning behind each of these choices.
"""
from __future__ import annotations
import os
from dataclasses import asdict

from . import ocr, classify, extract, validate, dedupe, reconcile, llm_client
REQUIRED_DOC_TYPES = sorted(classify.REQUIRED_TYPES)
FIELD_RELEVANT_DOC_TYPES = {
    "vin": ["police_report", "finance_agreement", "settlement_breakdown"],
    "date_of_loss": ["police_report", "settlement_breakdown"],
    "insurance_payout": ["settlement_breakdown"],
    "loan_balance": ["finance_agreement", "settlement_breakdown"],
}
FIELD_LABELS = {
    "vin": "VIN",
    "date_of_loss": "date of loss",
    "insurance_payout": "insurance payout amount",
    "loan_balance": "outstanding loan balance",
}


class ClaimsAgent:
    def __init__(self, claim_dir: str):
        self.claim_dir = claim_dir
        self.claim_id = os.path.basename(claim_dir.rstrip("/"))
        self.tools_used: list[dict] = []

    def _log(self, tool: str, input_, result):
        self.tools_used.append({"tool": tool, "input": input_, "result": result})

    # ------------------------------------------------------------------
    def _read_and_classify_all(self):
        docs = []
        for fname in sorted(os.listdir(self.claim_dir)):
            path = os.path.join(self.claim_dir, fname)
            if not os.path.isfile(path):
                continue
            raw = ocr.read_document(path)
            self._log("document_reader", fname, {"method": raw.method, "quality": raw.quality})
            doc_type, conf = classify.classify_document(path, raw.text)
            self._log("document_classifier", fname, {"doc_type": doc_type, "confidence": conf})
            docs.append({"file": fname, "doc_type": doc_type, "text": raw.text,
                         "quality": raw.quality, "classify_confidence": conf})
        return docs

    def _extract_fields_for(self, doc):
        fields = extract.extract_all_fields(doc["text"], doc["file"], doc["quality"])
        self._log("field_extractor", doc["file"],
                   {k: v.value for k, v in fields.items()})
        return fields

    # ------------------------------------------------------------------
    def process(self) -> dict:
        all_docs = self._read_and_classify_all()

        customer_reply_docs = [d for d in all_docs if d["doc_type"] == "customer_reply"]
        structural_docs = [d for d in all_docs if d["doc_type"] != "customer_reply"]

        groups = dedupe.group_documents(structural_docs) if structural_docs else []
        if any(g.relationship != "single" for g in groups):
            self._log("duplicate_detector", [g.doc_type for g in groups if g.relationship != "single"],
                       [{"doc_type": g.doc_type, "relationship": g.relationship, "note": g.note} for g in groups
                        if g.relationship != "single"])

        present_types = {g.doc_type for g in groups}
        missing_docs = [t for t in REQUIRED_DOC_TYPES if t not in present_types]

        authoritative_by_type = {g.doc_type: g.authoritative_file for g in groups}
        superseded_files = {f for g in groups for f in g.files if f != g.authoritative_file}
        doc_by_file = {d["file"]: d for d in structural_docs}

        extracted_by_file = {}
        for f in authoritative_by_type.values():
            extracted_by_file[f] = self._extract_fields_for(doc_by_file[f])
        # Also extract from superseded files, so a conflicting older version is
        # visible to the consistency checker (needed to explain e.g. the
        # CLM-003 VIN mismatch / settlement revision correctly).
        for f in superseded_files:
            extracted_by_file[f] = self._extract_fields_for(doc_by_file[f])

        # --- cross-document consistency ------------------------------------------------
        reconciled = {}
        issues = []

        # Ambiguous duplicates (same doc_type, differing content, no revision
        # marker) get surfaced as a real issue -- previously this was only a
        # note on the DocGroup that nothing downstream ever read, so a claim
        # could come back "complete" despite an unresolved document conflict.
        for g in groups:
            if g.needs_review:
                issues.append({
                    "type": "inconsistency",
                    "description": f"Multiple {g.doc_type} documents with differing content, "
                                    f"can't determine which is authoritative",
                    "details": g.note,
                })

        for field_name, relevant_types in FIELD_RELEVANT_DOC_TYPES.items():
            contributors = []
            for doc_type in relevant_types:
                group = next((g for g in groups if g.doc_type == doc_type), None)
                if not group:
                    continue
                for f in group.files:
                    fv = extracted_by_file[f][field_name]
                    contributors.append({"file": f, "value": fv.value, "confidence": fv.confidence,
                                          "reason": fv.reason})
            consensus = reconcile.reconcile_field(field_name, contributors, superseded_files)
            self._log("consistency_checker", field_name,
                       {"value": consensus.value, "conflict": consensus.conflict})
            reconciled[field_name] = consensus

            if consensus.conflict:
                disagreeing = [c for c in contributors if c["value"]]
                issues.append({
                    "type": "inconsistency",
                    "description": f"{FIELD_LABELS[field_name]} disagrees across documents",
                    "details": "; ".join(f"{c['file']}: {c['value']}" for c in disagreeing),
                    "guess": consensus.value,
                    "guess_reason": consensus.guess_reason,
                })

        # --- validation ------------------------------------------------
        missing_fields = []
        for field_name, consensus in reconciled.items():
            relevant_types = FIELD_RELEVANT_DOC_TYPES[field_name]
            applicable = any(t in present_types for t in relevant_types)
            if not applicable:
                continue  # source doc for this field isn't even present -- covered by missing_docs
            if not consensus.value:
                missing_fields.append(field_name)
                continue

            if field_name == "vin":
                ok, reason = validate.validate_vin(consensus.value)
            elif field_name == "date_of_loss":
                ok, reason = validate.validate_date(consensus.value)
            else:
                ok, reason = validate.validate_numeric(consensus.value, FIELD_LABELS[field_name])
            self._log("field_validator", {field_name: consensus.value}, {"valid": ok, "reason": reason})
            if not ok:
                issues.append({"type": "invalid", "description": f"{FIELD_LABELS[field_name]} failed validation",
                                "details": reason})
            if consensus.confidence == "low" and ok:
                issues.append({"type": "low_confidence",
                                "description": f"{FIELD_LABELS[field_name]} extracted with low confidence",
                                "details": consensus.contributors})

        for doc_type in missing_docs:
            issues.append({"type": "missing", "description": f"Missing required document: {doc_type}",
                            "details": None})

        # --- multi-turn: customer reply -------------------------------
        reply_notes = None
        llm_backend_used = None
        if customer_reply_docs:
            conflicted_fields = [f for f, c in reconciled.items() if c.conflict]
            open_questions = (list(missing_fields) + list(conflicted_fields) +
                               [f"document:{d}" for d in missing_docs])
            reply_text = "\n\n".join(d["text"] for d in customer_reply_docs)
            result, backend = llm_client.parse_customer_reply(self.claim_id, reply_text, open_questions)
            llm_backend_used = backend
            self._log("reply_parser", {"open_questions": open_questions}, {"backend": backend, "result": result})

            resolved = result.get("resolved", {})
            for q, val in resolved.items():
                field_name = q.split(":")[-1] if q.startswith("document:") else q
                if q.startswith("document:"):
                    # Customer reply substitutes for a missing doc only partially --
                    # the doc is still absent, but note what info they supplied instead.
                    continue
                elif field_name in missing_fields:
                    # Field was entirely unknown -- accept the customer's figure, but
                    # only at medium/low confidence since it's self-reported.
                    missing_fields.remove(field_name)
                    reconciled[field_name] = reconcile.FieldConsensus(
                        value=val, confidence="medium", contributors=[{"file": "customer_reply.txt",
                                                                         "value": val, "confidence": "medium"}],
                        conflict=False, guess_reason="Provided by customer via reply email; not independently verified.")
                    issues.append({"type": "low_confidence",
                                    "description": f"{FIELD_LABELS.get(field_name, field_name)} sourced from customer statement, not an official document",
                                    "details": val})
                elif field_name in conflicted_fields:
                    # Field already had a documented value in conflict -- the customer is
                    # weighing in on (or disputing) it. Don't silently overwrite an official
                    # record from an email; surface it as a flagged correction request instead.
                    issues.append({"type": "inconsistency",
                                    "description": f"Customer disputes the recorded {FIELD_LABELS.get(field_name, field_name)}",
                                    "details": f"Customer states {val}; current best guess is "
                                                f"{reconciled[field_name].value}. Needs manual confirmation "
                                                f"before correcting the record."})
            reply_notes = result.get("notes") or None
            if reply_notes:
                issues.append({"type": "needs_review", "description": "Customer reply requires manual follow-up",
                                "details": reply_notes})
            still_open = result.get("still_open", [])
            missing_fields = [f for f in missing_fields if f in still_open or f not in resolved]

        # --- status decision --------------------------------------------------
        if missing_docs or missing_fields:
            status = "incomplete"
        elif any(i["type"] in ("inconsistency", "invalid") for i in issues) or reply_notes:
            status = "needs_review"
        elif any(i["type"] == "low_confidence" for i in issues):
            status = "needs_review"
        else:
            status = "complete"

        # --- next action --------------------------------------------------
        conflicted_field_names = {
            f for f, c in reconciled.items() if c.conflict
        }
        # Only fields/docs that DON'T involve an active official-document
        # conflict are things the customer can actually help with -- a
        # conflicted field should never be phrased as a question to the
        # customer (they can't referee two of the company's own documents).
        customer_fixable_fields = [f for f in missing_fields if f not in conflicted_field_names]
        customer_fixable = bool(missing_docs or customer_fixable_fields or reply_notes)
        official_conflict = any(i["type"] == "inconsistency" for i in issues)

        if status == "complete":
            next_action = {"type": "finalize", "message": None}
        elif official_conflict:
            # Conflicts always escalate -- but if there's ALSO a genuinely
            # customer-fixable gap that doesn't involve the conflict (e.g. a
            # separately missing field), that doesn't just get silently
            # dropped: it's called out for the adjuster so nothing falls
            # through the cracks while the conflict is being sorted out.
            escalate_msg = ("Conflicting values across official documents that customer input can't "
                             "resolve -- route to an adjuster to confirm the correct figure.")
            if missing_docs or customer_fixable_fields:
                extra = []
                if missing_docs:
                    extra.append(f"missing document(s): {', '.join(missing_docs)}")
                if customer_fixable_fields:
                    extra.append(f"still needs from customer: {', '.join(customer_fixable_fields)}")
                escalate_msg += " Separately, while this is under adjuster review: " + "; ".join(extra) + "."
            next_action = {"type": "escalate", "message": escalate_msg}
        elif customer_fixable:
            msg, backend = llm_client.draft_customer_message(
                self.claim_id, missing_docs, customer_fixable_fields, issues)
            llm_backend_used = llm_backend_used or backend
            self._log("message_drafter", {"missing_docs": missing_docs, "missing_fields": customer_fixable_fields}, backend)
            next_action = {"type": "message_customer", "message": msg}
        else:
            next_action = {"type": "escalate",
                            "message": "Low-confidence extraction with no missing info or clear conflict to ask "
                                       "the customer about -- needs a human to eyeball the source scans."}

        # --- assemble output --------------------------------------------------
        identified = []
        for g in groups:
            for f in g.files:
                label = g.doc_type if g.doc_type != "unknown" else f"unknown -- {doc_by_file[f]['text'][:40].strip() or 'unclassified'}"
                identified.append({"file": f, "type": label})

        duplicates = [{"doc_type": g.doc_type, "files": g.files, "relationship": g.relationship, "note": g.note}
                       for g in groups if g.relationship != "single"]

        extracted_fields_out = {}
        for field_name, consensus in reconciled.items():
            src = consensus.contributors[0]["file"] if consensus.contributors else None
            extracted_fields_out[field_name] = {
                "value": consensus.value,
                "confidence": consensus.confidence,
                "source": src,
                "reason": consensus.guess_reason if consensus.conflict else (
                    None if consensus.confidence == "high" else "Low-confidence extraction; see tools_used log."),
            }

        return {
            "claim_id": self.claim_id,
            "status": status,
            "extracted_fields": extracted_fields_out,
            "documents": {
                "identified": identified,
                "missing": missing_docs,
                "duplicates": duplicates,
            },
            "issues": issues,
            "next_action": next_action,
            "llm_backend": llm_backend_used,
            "customer_replied": bool(customer_reply_docs),
            "tools_used": self.tools_used,
        }